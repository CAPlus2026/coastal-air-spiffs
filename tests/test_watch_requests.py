"""watch_requests.py — the long-running poller that replaced the throttled GitHub cron."""
import types

import watch_requests as w


class Recorder:
    def __init__(self):
        self.status = []
        self.ran = []


def patch(monkeypatch, *, tests_error=None, still_queued_after=False, publish_error=None, rc=0):
    r = Recorder()
    monkeypatch.setattr(w, "sh", lambda args, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(w, "run_tests", lambda: tests_error)
    monkeypatch.setattr(w, "record", lambda month, status, msg: r.status.append((month, status, msg)))
    monkeypatch.setattr(w.subprocess, "run", lambda args, **kw: r.ran.append(args) or types.SimpleNamespace(returncode=rc))
    monkeypatch.setattr(w, "queued_month", lambda: "Sep 2026" if still_queued_after else None)
    monkeypatch.setattr(w, "publish", lambda month: publish_error)
    return r


def test_failing_tests_block_processing_and_record_why(monkeypatch):
    r = patch(monkeypatch, tests_error="Python tests failed: 1 failed")
    w.handle("Sep 2026")
    assert r.ran == [], "live payroll data must not be touched when the test suite fails"
    assert r.status == [("Sep 2026", "failed", "Not run — Python tests failed: 1 failed")]


def test_passing_tests_run_the_processor_once(monkeypatch):
    r = patch(monkeypatch)
    w.handle("Sep 2026")
    assert len(r.ran) == 1 and r.ran[0][-1] == "run_requested_month.py"
    assert r.status == []


def test_runner_that_dies_without_a_status_is_marked_failed_not_retried_forever(monkeypatch):
    r = patch(monkeypatch, still_queued_after=True, rc=1)
    w.handle("Sep 2026")
    assert r.status and r.status[0][1] == "failed" and "without recording" in r.status[0][2]


def test_publish_failure_is_reported(monkeypatch):
    r = patch(monkeypatch, publish_error="git push failed")
    w.handle("Sep 2026")
    assert r.status[-1][1] == "failed" and "publish" in r.status[-1][2]


def test_main_polls_handles_a_request_then_starts_a_successor(monkeypatch):
    seq = iter(["Sep 2026"])
    handled, slept, successor = [], [], []
    monkeypatch.setattr(w, "queued_month", lambda: next(seq, None))
    monkeypatch.setattr(w, "handle", lambda m: handled.append(m))
    monkeypatch.setattr(w.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(w, "start_successor", lambda: successor.append(True))
    ticks = {"t": -1}

    def fake_time():
        ticks["t"] += 1
        return ticks["t"] * 40  # 40 "seconds" per call, so the 100s watch window closes quickly
    monkeypatch.setattr(w.time, "time", fake_time)
    monkeypatch.setattr(w, "WATCH_SECONDS", 100)
    w.main()
    assert handled == ["Sep 2026"]
    assert slept and successor == [True]
