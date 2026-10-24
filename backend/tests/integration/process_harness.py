"""Helpers for tests that run the real worker as a separate OS process and then break things.

Nothing here is mocked: the worker is ``python -m app.workers.main``, it talks to the real
PostgreSQL and Redis, and "killing" it is the operating system's hard kill (no ``finally``, no
signal handler, no goodbye), the same thing an out-of-memory kill or a lost node does.
"""

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]


def eventually(
    condition: Callable[[], object],
    *,
    timeout: float = 90.0,
    interval: float = 0.25,
    what: str = "",
):
    """Poll until ``condition()`` is truthy and return its value, or fail with ``what``."""
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        last = condition()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for: {what or condition}")


def still_true_for(
    condition: Callable[[], bool], seconds: float, *, interval: float = 0.25
) -> bool:
    """True if ``condition()`` held at every check for ``seconds``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not condition():
            return False
        time.sleep(interval)
    return True


class WorkerProcess:
    def __init__(self, env: dict[str, str], log_path: Path, name: str = "worker") -> None:
        self.env = env
        self.log_path = log_path
        self.name = name
        self.proc: subprocess.Popen[bytes] | None = None
        self._log = None

    def start(self) -> "WorkerProcess":
        self._log = open(self.log_path, "ab")  # noqa: SIM115 - closed in kill()/stop()
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "app.workers.main"],
            cwd=BACKEND_DIR,
            env=self.env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        return self

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def logs(self) -> str:
        return (
            self.log_path.read_text(encoding="utf-8", errors="replace")
            if self.log_path.exists()
            else ""
        )

    def wait_started(self, timeout: float = 60.0) -> None:
        eventually(
            lambda: "worker started" in self.logs() or not self.alive,
            timeout=timeout,
            what=f"{self.name} to start",
        )
        assert self.alive, f"{self.name} exited while starting:\n{self.logs()[-2000:]}"

    def kill(self) -> None:
        """Hard kill: TerminateProcess on Windows, SIGKILL elsewhere. No cleanup code runs."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=15)
        self._close_log()

    def _close_log(self) -> None:
        if self._log is not None and not self._log.closed:
            self._log.close()

    def wait_exit(self, timeout: float = 30.0) -> int:
        assert self.proc is not None
        code = self.proc.wait(timeout=timeout)
        self._close_log()
        return code


class TcpProxy:
    """A TCP forwarder that can be cut and restored, to simulate a database or Redis outage
    without touching the real server (other work may be using it)."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self.port = self._listener.getsockname()[1]
        self._connections: list[socket.socket] = []
        self._lock = threading.Lock()
        self._up = True
        self._closed = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if not self._up:
                client.close()  # refuse by hanging up at once
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=5)
            except OSError:
                client.close()
                continue
            with self._lock:
                self._connections.extend([client, upstream])
            for src, dst in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(src, dst), daemon=True).start()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()

    def cut(self) -> None:
        """The server becomes unreachable and every open connection is dropped."""
        self._up = False
        with self._lock:
            connections, self._connections = self._connections, []
        for s in connections:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def restore(self) -> None:
        self._up = True

    def close(self) -> None:
        self._closed = True
        self.cut()
        self._listener.close()


def base_worker_env(**overrides: str) -> dict[str, str]:
    """A clean environment: nothing from the developer's shell or .env leaks into the worker."""
    keep = {
        k: v
        for k, v in os.environ.items()
        if k in {"SYSTEMROOT", "PATH", "TEMP", "TMP", "USERPROFILE"}
    }
    env = {
        **keep,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "ENVIRONMENT": "development",
        "LOG_LEVEL": "INFO",
        "EMBEDDING_PROVIDER": "fake",
        "LLM_PROVIDER": "fake",
    }
    env.update(overrides)
    return env
