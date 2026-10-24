"""Liveness, readiness and worker health are different questions with different answers."""

import os
import time
from pathlib import Path

import pytest

from app.db.database import get_db
from app.main import app
from app.workers import healthcheck
from app.workers.heartbeat import WorkerHeartbeat, file_is_fresh
from app.workers.queue import InMemoryJobQueue, get_job_queue

SECRET = "postgresql://app:SuperSecretPassword@10.1.2.3:5432/bizflow"


class BrokenDb:
    """A session whose every statement fails the way an unreachable database does."""

    def execute(self, *args, **kwargs):
        raise RuntimeError(f"could not connect to {SECRET}")

    def rollback(self):
        pass


def with_broken_db():
    app.dependency_overrides[get_db] = lambda: BrokenDb()


def with_queue(queue):
    app.dependency_overrides[get_job_queue] = lambda: queue


class DeadRedis(InMemoryJobQueue):
    def ping(self):
        raise ConnectionError(f"redis at {SECRET} refused")

    def live_workers(self):
        raise ConnectionError("redis is down")


# --- liveness: the process is up, and nothing else is asked --------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/health/live"])
def test_liveness_stays_green_when_every_dependency_is_down(client, path):
    with_broken_db()
    with_queue(DeadRedis())
    r = client.get(path)
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# --- readiness: can this instance serve? ------------------------------------------------------------------------------


def test_readiness_is_green_when_postgres_and_redis_answer(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}
    assert r.headers["cache-control"] == "no-store"


def test_readiness_goes_red_when_the_database_is_down_and_names_only_that_component(client):
    with_broken_db()
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {
        "status": "unavailable",
        "checks": {"database": "unavailable", "redis": "ok"},
    }


def test_readiness_goes_red_when_redis_is_down(client):
    with_queue(DeadRedis())
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": "ok", "redis": "unavailable"}


def test_readiness_when_everything_is_down(client):
    with_broken_db()
    with_queue(DeadRedis())
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"] == {
        "database": "unavailable",
        "redis": "unavailable",
    }


@pytest.mark.parametrize("path", ["/health/ready", "/health/db", "/health/worker"])
def test_health_responses_never_leak_credentials_hosts_or_exception_text(client, path):
    with_broken_db()
    with_queue(DeadRedis())
    r = client.get(path)
    body = r.text.lower()
    for leaked in (
        "supersecret",
        "10.1.2.3",
        "postgresql://",
        "could not connect",
        "refused",
        "traceback",
    ):
        assert leaked not in body, (path, leaked)


def test_the_original_database_probe_still_answers(client):
    assert client.get("/health/db").json() == {"status": "ok", "database": "ok"}
    with_broken_db()
    r = client.get("/health/db")
    assert r.status_code == 503 and r.json()["database"] == "unavailable"


# --- worker health: is anybody running the background jobs? -------------------------------------------------------------


def test_no_worker_is_reported_as_unhealthy_but_does_not_fail_readiness(client):
    assert client.get("/health/ready").status_code == 200  # the API itself serves fine
    r = client.get("/health/worker")
    assert r.status_code == 503 and r.json() == {"status": "none", "workers": 0}


def test_a_beating_worker_makes_the_worker_probe_green(client):
    queue = InMemoryJobQueue()
    with_queue(queue)
    queue.beat("worker-a", 20)
    queue.beat("worker-b", 20)
    r = client.get("/health/worker")
    assert r.status_code == 200 and r.json() == {"status": "ok", "workers": 2}


def test_an_unreachable_redis_is_unknown_not_a_crash(client):
    with_queue(DeadRedis())
    r = client.get("/health/worker")
    assert r.status_code == 503 and r.json()["status"] == "unknown"


# --- the heartbeat itself ------------------------------------------------------------------------------------------------


def test_the_heartbeat_touches_its_file_and_tells_redis_and_removes_the_file_when_stopped(tmp_path):
    queue = InMemoryJobQueue()
    path = tmp_path / "hb"
    beat = WorkerHeartbeat(queue, path=path, interval=0.05, worker_id="w1")
    beat.start()
    try:
        assert path.exists() and queue.workers == {"w1": 20}
        first = path.stat().st_mtime
        time.sleep(0.2)
        assert path.stat().st_mtime >= first  # still being touched
    finally:
        beat.stop()
    assert not path.exists()  # a stopped worker must not look alive


def test_a_redis_outage_does_not_stop_the_heartbeat_thread(tmp_path):
    class Flaky(InMemoryJobQueue):
        def beat(self, worker_id, ttl_seconds):
            raise ConnectionError("redis down")

    beat = WorkerHeartbeat(Flaky(), path=tmp_path / "hb", interval=0.05)
    beat.start()
    time.sleep(0.15)
    assert beat._thread.is_alive()  # the file (the container's own signal) keeps being written
    beat.stop()


def test_file_freshness(tmp_path):
    path = tmp_path / "hb"
    assert file_is_fresh(path, 30) is False  # missing
    path.touch()
    assert file_is_fresh(path, 30) is True
    old = time.time() - 120
    os.utime(path, (old, old))
    assert file_is_fresh(path, 30) is False


def test_the_container_healthcheck_command_exits_zero_only_for_a_live_worker(
    tmp_path, monkeypatch, capsys
):
    path = Path(tmp_path / "hb")
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(path))
    assert healthcheck.main([]) == 1 and capsys.readouterr().out.strip() == "stale"
    path.touch()
    assert healthcheck.main([]) == 0 and capsys.readouterr().out.strip() == "ok"
    old = time.time() - 120
    os.utime(path, (old, old))
    assert healthcheck.main([]) == 1


def test_the_cluster_healthcheck_asks_redis_and_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(healthcheck, "RedisJobQueue", lambda *a, **k: InMemoryJobQueue())
    assert healthcheck.main(["--cluster"]) == 1

    class Alive(InMemoryJobQueue):
        def live_workers(self):
            return 1

    monkeypatch.setattr(healthcheck, "RedisJobQueue", lambda *a, **k: Alive())
    assert healthcheck.main(["--cluster"]) == 0
    monkeypatch.setattr(healthcheck, "RedisJobQueue", lambda *a, **k: DeadRedis())
    assert healthcheck.main(["--cluster"]) == 1  # cannot ask: not healthy
    assert "SuperSecret" not in capsys.readouterr().out
