"""Hand-rolled mutation testing: change one thing in production code, run the tests that should
notice, restore the file byte for byte. A mutant that is not caught ("SURVIVED") is either a gap in
the tests or an equivalent mutant (redundant defence in depth); every survivor must be triaged.

    cd backend
    python -m mutation.run list                 # the catalog
    python -m mutation.run check                # every snippet occurs exactly once (no test is run)
    python -m mutation.run run [id-prefix ...]  # run all mutants, or those whose id starts with a prefix
    python -m mutation.run report               # summary of the last results

Safety: the whole ``app/`` tree is hashed before and after a run and the run fails loudly if it
differs, so an interrupted run can never leave a mutated file behind unnoticed. Integration mutants
need ``docker compose up -d db redis``.
"""

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from mutation.catalog import M as PHASE_4
from mutation.catalog_5 import M as PHASE_5
from mutation.catalog_6 import M as PHASE_6
from mutation.catalog_7 import M as PHASE_7
from mutation.catalog_8 import M as PHASE_8
from mutation.catalog_9 import M as PHASE_9
from mutation.catalog_10 import M as PHASE_10
from mutation.catalog_41 import M as PHASE_4_1
from mutation.catalog_platform import M as PLATFORM
from mutation.expected import EXPECTED_SURVIVORS

BACKEND = Path(__file__).resolve().parents[1]
RESULTS = Path(__file__).with_name("results.json")
MUTANTS = [*PHASE_4, *PHASE_4_1, *PHASE_5, *PHASE_6, *PHASE_7, *PHASE_8, *PHASE_9, *PHASE_10, *PLATFORM]


def tree_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted((BACKEND / "app").rglob("*.py")):
        digest.update(str(path.relative_to(BACKEND)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def variants(text: str, old, new):
    pairs = list(zip(old, new, strict=True)) if isinstance(old, list) else [(old, new)]
    if "\r\n" in text:
        pairs = [(o.replace("\n", "\r\n"), n.replace("\n", "\r\n")) for o, n in pairs]
    return pairs


def check() -> int:
    bad = 0
    ids = [m[0] for m in MUTANTS]
    for duplicate in {i for i in ids if ids.count(i) > 1}:
        print(f"BAD   duplicate id {duplicate}")
        bad += 1
    for mid, file, old, new, _ in MUTANTS:
        text = (BACKEND / file).read_bytes().decode("utf-8")
        for o, _n in variants(text, old, new):
            n = text.count(o)
            if n != 1:
                print(f"BAD   {mid}: snippet occurs {n}x in {file}")
                bad += 1
    print(f"{len(MUTANTS)} mutants: " + ("all snippets unique" if not bad else f"{bad} problem(s)"))
    return bad


def run(prefixes: list[str]) -> int:
    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    before = tree_hash()
    for mid, file, old, new, args in MUTANTS:
        if prefixes and not any(mid.startswith(p) for p in prefixes):
            continue
        path = BACKEND / file
        original = path.read_bytes()
        text = original.decode("utf-8")
        mutated = text
        for o, n in variants(text, old, new):
            assert text.count(o) == 1, (mid, text.count(o))
            mutated = mutated.replace(o, n, 1)
        started = time.time()
        try:
            path.write_bytes(mutated.encode("utf-8"))
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-x",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--tb=line",
                    *args,
                ],
                cwd=BACKEND,
                capture_output=True,
                text=True,
                timeout=900,
            )
            tail = (proc.stdout + proc.stderr).strip().splitlines()
            failed = [ln for ln in tail if ln.startswith("FAILED") or " Error" in ln][:2]
            verdict = "SURVIVED" if proc.returncode == 0 else "killed"
            summary = tail[-1] if tail else ""
        except subprocess.TimeoutExpired:
            verdict, summary, failed = "killed", "timeout (hang)", []
        finally:
            path.write_bytes(original)
        assert path.read_bytes() == original
        results[mid] = {"verdict": verdict, "by": failed[:1], "summary": summary[:120]}
        RESULTS.write_text(json.dumps(results, indent=1))
        print(f"{verdict:9} {mid}  ({time.time() - started:.0f}s)  {summary[:90]}", flush=True)
    after = tree_hash()
    if before != after:
        print(
            "ERROR: the app/ tree differs after the run; inspect `git status` before doing anything else"
        )
        return 2
    print("app/ tree identical before and after the run")
    return 0


def report() -> int:
    """Exit 1 if any mutant survived that is not a documented equivalent, or any has not run."""
    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    wanted = {m[0] for m in MUTANTS}
    shown = {k: v for k, v in results.items() if k in wanted}
    survivors = sorted(k for k, v in shown.items() if v["verdict"] != "killed")
    unexpected = [k for k in survivors if k not in EXPECTED_SURVIVORS]
    unexpectedly_killed = sorted(
        k for k in EXPECTED_SURVIVORS if shown.get(k, {}).get("verdict") == "killed"
    )
    killed = sum(1 for v in shown.values() if v["verdict"] == "killed")
    print(f"{len(shown)} of {len(wanted)} mutants run: {killed} killed, {len(survivors)} survived")
    for k in survivors:
        if k in EXPECTED_SURVIVORS:
            print(f"  expected survivor   {k}: {EXPECTED_SURVIVORS[k]}")
    for k in unexpected:
        print(f"  UNEXPECTED SURVIVOR {k}: a gap in the tests, or an equivalent mutant to document")
    for k in unexpectedly_killed:
        print(
            f"  note: {k} was expected to survive but was killed (the expectation can be removed)"
        )
    return 1 if unexpected or len(shown) < len(wanted) else 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "list"
    if command == "list":
        for mid, file, *_ in MUTANTS:
            print(f"{mid:55} {file}")
        sys.exit(0)
    if command == "report":
        sys.exit(report())
    if command == "check":
        sys.exit(1 if check() else 0)
    if command == "run":
        sys.exit(2 if check() else run(sys.argv[2:]))
    sys.exit(f"unknown command {command!r}")
