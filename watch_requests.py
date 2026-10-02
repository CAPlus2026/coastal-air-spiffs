"""Long-running watcher for the self-service "Run payroll" button.

WHY THIS EXISTS (found 2026-10-02): the workflow used to rely on a `*/5 * * * *` GitHub Actions
schedule. GitHub heavily throttles scheduled workflows — in practice they fired only every 4-6
HOURS — so a request from the app sat on "Queued" for hours. A schedule can't be made reliable
from our side, so instead this job stays alive for ~5h45m, polls the run_requests Sheet tab every
30 seconds, processes a request within seconds of it appearing, and then starts its own successor
(workflow_dispatch via the built-in GITHUB_TOKEN is allowed) before the 6h job limit. The cron
schedule stays as a safety net that restarts the chain if it ever dies.

For each queued request it: pulls the latest code (a fix pushed while the watcher is running takes
effect on the next request), runs the same test suites the old `test` job gated on, runs
run_requested_month.py (which does the processing and records running/complete/failed/blocked),
then commits and pushes index.html + output files exactly like the old workflow step did.

Usage (from the workflow): python watch_requests.py
"""
import os
import subprocess
import sys
import time

WATCH_SECONDS = int(os.environ.get("WATCH_SECONDS", 345 * 60))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", 30))


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def tail(text, n=4):
    return " | ".join((text or "").strip().splitlines()[-n:])[:400]


def queued_month():
    """Month of the first request whose latest status is 'queued', else None. A fresh interpreter
    every time, so a `git pull` that changed run_requested_month.py is picked up immediately.
    Any hiccup (Sheet unreachable, import error) counts as 'nothing queued' — keep polling."""
    r = sh([sys.executable, "-c",
            "import run_requested_month as r; print(r.find_queued_month() or '')"])
    if r.returncode != 0:
        print(f"  (queue check failed: {tail(r.stderr)})", flush=True)
        return None
    return r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else None


def record(month, status, message):
    sh([sys.executable, "-c",
        "import sys, run_requested_month as r; r.append_run_status(sys.argv[1], sys.argv[2], sys.argv[3])",
        month, status, message[:500]])


def run_tests():
    """Same gate the old `test` job applied before anything touches live payroll data. Returns
    an error string, or None when everything passed."""
    sh([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements-dev.txt"])
    py = sh([sys.executable, "-m", "pytest", "-q"])
    if py.returncode != 0:
        return f"Python tests failed: {tail(py.stdout)}"
    js = sh(["node", "--test", "tests/js/replay.test.mjs"])
    if js.returncode != 0:
        return f"JS tests failed: {tail(js.stdout)}"
    return None


def publish(month):
    """Commit and push the regenerated index.html / output files (what the workflow's last step
    used to do). Returns an error string, or None."""
    sh(["git", "config", "user.name", "Coastal Air Spiffs Bot"])
    sh(["git", "config", "user.email", "actions@users.noreply.github.com"])
    sh(["git", "add", "index.html", "output_*.json"])
    if sh(["git", "diff", "--staged", "--quiet"]).returncode == 0:
        print("  No changes to commit.", flush=True)
        return None
    sh(["git", "commit", "-q", "-m", "Automated: process spiffs month via self-service runner"])
    for attempt in range(3):
        sh(["git", "pull", "--rebase", "-q"])
        push = sh(["git", "push", "-q"])
        if push.returncode == 0:
            return None
        time.sleep(3)
    return f"git push failed for {month}: {tail(push.stderr)}"


def handle(month):
    print(f"[watch] request for {month}", flush=True)
    pull = sh(["git", "pull", "--rebase", "-q"])
    if pull.returncode != 0:
        print(f"  (git pull failed, continuing with current code: {tail(pull.stderr)})", flush=True)
    err = run_tests()
    if err:
        record(month, "failed", f"Not run — {err}")
        print(f"  {err}", flush=True)
        return
    proc = subprocess.run([sys.executable, "run_requested_month.py"])
    # run_requested_month.py records running/complete/failed/blocked itself. If it died before
    # recording anything the request would stay 'queued' and be retried every 30s forever.
    if queued_month() == month:
        record(month, "failed", f"The runner exited ({proc.returncode}) without recording a status.")
    err = publish(month)
    if err:
        record(month, "failed", f"Processed, but couldn't publish: {err}")
        print(f"  {err}", flush=True)


def start_successor():
    """Queue the next watcher before this one hits the 6h job limit. GITHUB_TOKEN-triggered
    workflow_dispatch is the one event type GitHub allows to start a new run."""
    r = sh(["gh", "workflow", "run", "process-spiffs.yml", "--ref", "main"])
    print(f"[watch] successor dispatch exit {r.returncode}: {tail(r.stderr or r.stdout)}", flush=True)


def main():
    deadline = time.time() + WATCH_SECONDS
    print(f"[watch] polling every {POLL_SECONDS}s for up to {WATCH_SECONDS // 60} minutes", flush=True)
    while time.time() < deadline:
        month = queued_month()
        if month:
            handle(month)
            continue  # check again immediately: more than one request may be waiting
        time.sleep(POLL_SECONDS)
    start_successor()


if __name__ == "__main__":
    main()
