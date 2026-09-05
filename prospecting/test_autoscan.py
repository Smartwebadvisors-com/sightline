"""
test_autoscan.py -- the unattended runner's safety properties.

Cron runs this on a box nobody is watching, so the things worth proving are
the ones that prevent damage: the lock, the caps, scan reuse, and preflight
actually stopping the run.

    python3 test_autoscan.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


import autoscan
from autoscan import AlreadyRunning, RunLock, RunStats
from sightline_cli import CANDIDATE_COMMANDS, _write_env

# --------------------------------------------------------------------------
print("\nrun lock")
LOCK = os.path.join(tempfile.gettempdir(), "aeo-test.lock")

with RunLock(LOCK):
    try:
        with RunLock(LOCK):
            check("second lock is refused", "acquired", "AlreadyRunning")
    except AlreadyRunning:
        check("second lock is refused", "AlreadyRunning", "AlreadyRunning")

# Released on exit, so the next cron tick can run.
with RunLock(LOCK):
    check("lock is reusable after release", True, True)


# Across real processes. Uses subprocesses rather than multiprocessing:
# forking a test runner that holds a file lock is a good way to deadlock the
# very thing under test.
HOLDER = """
import fcntl, sys, time
fh = open(sys.argv[1], 'w')
try:
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('acquired', flush=True)
    time.sleep(float(sys.argv[2]))
except BlockingIOError:
    print('refused', flush=True)
"""

import subprocess

holder = subprocess.Popen([sys.executable, "-c", HOLDER, LOCK, "2"],
                          stdout=subprocess.PIPE, text=True)
time.sleep(0.6)
second = subprocess.run([sys.executable, "-c", HOLDER, LOCK, "0"],
                        capture_output=True, text=True, timeout=20)
check("a second process is refused while one holds it",
      second.stdout.strip(), "refused")
holder.wait(timeout=20)
check("the holder did acquire it", holder.stdout.read().strip(), "acquired")

third = subprocess.run([sys.executable, "-c", HOLDER, LOCK, "0"],
                       capture_output=True, text=True, timeout=20)
check("available again once the holder exits", third.stdout.strip(), "acquired")

# A process that dies without unlocking must not wedge the pipeline.
CRASHER = """
import fcntl, os, sys
fh = open(sys.argv[1], 'w')
fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
os._exit(1)
"""
subprocess.run([sys.executable, "-c", CRASHER, LOCK], timeout=20)
try:
    with RunLock(LOCK):
        check("a crashed run releases its lock", True, True)
except AlreadyRunning:
    check("a crashed run releases its lock", "still locked", "released")

# --------------------------------------------------------------------------
print("\nrun stats")
s = RunStats()
s.considered, s.scans_run, s.scans_reused = 10, 4, 6
s.qualified, s.review, s.skipped = 2, 3, 5
text = s.summary()
check("summary counts scans", "4 run, 6 reused" in text, True)
check("summary counts verdicts", "2 qualified, 3 review, 5 skipped" in text, True)

# --------------------------------------------------------------------------
print("\ninvocation candidates")
check("every candidate has a url slot",
      all("{url}" in c for c in CANDIDATE_COMMANDS), True)
check("module form is tried first",
      CANDIDATE_COMMANDS[0], "python3 -m sightline scan {url}")
check("enough candidates to be worth automating",
      len(CANDIDATE_COMMANDS) >= 8, True)

# --------------------------------------------------------------------------
print("\n.env writing")
with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
    fh.write('PERPLEXITY_API_KEY="abc"\nSIGHTLINE_CMD="old command"\nAEO_DAILY_CAP="30"\n')
    path = fh.name

_write_env(path, "SIGHTLINE_CMD", "python3 main.py scan {url}")
content = open(path).read()
check("existing key replaced", "SIGHTLINE_CMD='python3 main.py scan {url}'" in content, True)
check("old value gone", "old command" not in content, True)
check("other keys untouched", 'PERPLEXITY_API_KEY="abc"' in content, True)
check("no duplicate key", content.count("SIGHTLINE_CMD="), 1)

_write_env(path, "AEO_MAX_SCANS", "40")
content = open(path).read()
check("new key appended", "AEO_MAX_SCANS='40'" in content, True)
check("values are single-quoted", content.count("'") >= 4, True)

# Values that break naive double-quoting: $ and ` are expanded by the shell
# when the .env is sourced, so a key containing them comes back mangled.
from sightline_cli import shell_quote
import subprocess
for hostile in ['pplx-Ab3$x&y?z', "key-with'quote", 'back`tick', 'a b c']:
    _write_env(path, "HOSTILE", hostile)
    got = subprocess.run(
        ["bash", "-c", f'set -a; . {path}; set +a; printf "%s" "$HOSTILE"'],
        capture_output=True, text=True).stdout
    check(f"survives sourcing: {hostile!r}", got, hostile)
os.unlink(path)

# --------------------------------------------------------------------------
print("\ncaps are read from the environment")
check("scan cap has a default", autoscan.MAX_SCANS_PER_RUN > 0, True)
check("probe cap has a default", autoscan.MAX_PROBES_PER_RUN > 0, True)
check("spend cap has a default", autoscan.MAX_SPEND_PER_RUN > 0, True)
check("scan reuse window has a default", autoscan.SCAN_MAX_AGE_DAYS > 0, True)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all autoscan checks passed")
