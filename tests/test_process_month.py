"""Regression tests for process_month.py, each mapped to a real incident found in this project's
history (see the docstring above each test) or a design invariant that would have prevented one.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import process_month as pm  # noqa: E402

from conftest import run_month  # noqa: E402


# ── sheet_get(required=...) — 2026-09-04 finding A: silent data loss ────────────────────────
def test_sheet_get_required_raises_on_fetch_failure(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("simulated network failure")
    monkeypatch.setattr(pm.requests, "get", boom)
    with pytest.raises(pm.SheetFetchError):
        pm.sheet_get("res_carry_forward", required=True)


def test_sheet_get_optional_still_fails_open(monkeypatch):
    """Deliberately NOT changing the default — some reads really are fine to treat as empty."""
    def boom(*a, **k):
        raise ConnectionError("simulated network failure")
    monkeypatch.setattr(pm.requests, "get", boom)
    assert pm.sheet_get("some_tab") == []


def test_main_writes_nothing_when_a_required_read_fails(mock_pipeline, monkeypatch):
    """The actual incident: a flaky fetch used to silently return [] and get written back via
    replaceMonth, permanently erasing the pending backlog. Now it must abort before any write.

    sheet_get()'s own required=True -> raises behavior is separately and directly tested above
    (test_sheet_get_required_raises_on_fetch_failure) — this test is purely about the
    integration: does main() actually stop before commit() writes anything."""
    write_calls = []
    real_write_table = mock_pipeline.write_table
    monkeypatch.setattr(mock_pipeline, "write_table",
                         lambda *a, **k: write_calls.append(a) or real_write_table(*a, **k))

    fake_sheet_get = pm.sheet_get  # the in-memory fake installed by mock_pipeline

    def failing_sheet_get(sheet_name, timeout=20, required=False):
        if sheet_name == "res_carry_forward" and required:
            raise pm.SheetFetchError(f"simulated failure fetching {sheet_name}")
        return fake_sheet_get(sheet_name, timeout=timeout, required=required)
    monkeypatch.setattr(pm, "sheet_get", failing_sheet_get)

    with pytest.raises(pm.SheetFetchError):
        pm.main("Aug 2026")
    assert write_calls == [], "a write happened despite a required read failing"


# ── Manual carry-forward duplication — bug introduced and fixed 2026-09-04 ───────────────────
def test_manual_carry_forward_not_duplicated_across_three_runs(mock_pipeline):
    mock_pipeline.tabs["manual_carry_forward"] = [
        ["Jun 2026", "mcf_test1", "steven", "Test Tech One", "Manager note — test reason",
         "50", "MB Install Residential", "test reason", "steven", "2026-06-15T00:00:00.000Z"],
    ]

    def count(result):
        return sum(1 for c in result["carryForward"] if c["emp"] == "Test Tech One")

    assert count(run_month("Jun 2026")) == 1
    assert count(run_month("Jul 2026")) == 1
    assert count(run_month("Aug 2026")) == 1


def test_manual_carry_forward_keeps_its_own_reason(mock_pipeline):
    mock_pipeline.tabs["manual_carry_forward"] = [
        ["Jun 2026", "mcf_test1", "steven", "Test Tech One", "Manager note — a specific reason",
         "50", "MB Install Residential", "a specific reason", "steven", "2026-06-15T00:00:00.000Z"],
    ]
    result = run_month("Jul 2026")  # carried forward at least once by now
    item = next(c for c in result["carryForward"] if c["emp"] == "Test Tech One")
    assert item["reason"] == "a specific reason", (
        "manual item's real reason was overwritten with the generic Stage-2 explanation"
    )


# ── Ledger dict-key bug — 2026-09-04 finding B ───────────────────────────────────────────────
def test_ledger_contains_technician_lines(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [
        {"EmployeeName": "Test Tech One", "Activity": "Sales Spiff", "Date": "2026-08-05",
         "JobNumber": "999001", "GrossPay": 25.0, "CustomerName": "Test Customer, Alpha",
         "LocationName": "", "LaborTypeCode": ""},
    ]
    run_month("Aug 2026")
    ledger_rows = mock_pipeline.get("spiff_ledger")
    assert any(row[2] == "Test Tech One" for row in ledger_rows), (
        "technician line missing from the ledger — regression of the dict-key bug where "
        "spiff_detail.get(mgr_, {}) always returned {} because spiff_detail is keyed by name"
    )


# ── Cross-technician duplicate detection ─────────────────────────────────────────────────────
def test_cross_technician_duplicate_flagged_by_job_number(mock_pipeline, fixture_reports):
    mock_pipeline.tabs["spiff_ledger"] = [
        ["Jul 2026", "steven", "Test Tech Two", "999002", "Test Customer, Beta", "Sales Spiff",
         "Test Item", "25.0", "MPF"],
    ]
    fixture_reports["masterPayFile"] = [
        {"EmployeeName": "Test Tech One", "Activity": "Sales Spiff", "Date": "2026-08-05",
         "JobNumber": "999002", "GrossPay": 25.0, "CustomerName": "Test Customer, Beta",
         "LocationName": "", "LaborTypeCode": ""},
    ]
    result = run_month("Aug 2026")
    assert any("duplicate" in f["title"].lower() for f in result["flags"]["steven"])


def test_same_surname_different_people_not_flagged(mock_pipeline, fixture_reports):
    """2026-09-04: Nick Scarpa ("Harris, Heather") vs Jenny Miller ("Harris, Loni") were falsely
    flagged as a duplicate because the no-job-number match key was last-name-only."""
    fixture_reports["membershipReport"] = [
        {"SoldBy": "Test Tech One", "MembershipBonus": 25.0, "CustomerName": "Harris, Heather",
         "SoldOn": "2026-08-01", "CustomerMembershipId": "1", "MembershipType": "Standard",
         "ActivationMethod": "New Sale"},
        {"SoldBy": "Test Office Rep", "MembershipBonus": 20.0, "CustomerName": "Harris, Loni",
         "SoldOn": "2026-08-01", "CustomerMembershipId": "2", "MembershipType": "Standard",
         "ActivationMethod": "New Sale"},
    ]
    result = run_month("Aug 2026")
    assert result["flags"]["steven"] == [], (
        "two different customers sharing a surname were flagged as a duplicate"
    )


def test_full_customer_key_vs_last_name_key():
    """Documents exactly why the false positive above happened, and that the fix is real."""
    assert pm.last_name_key("Harris, Heather") == pm.last_name_key("Harris, Loni")
    assert pm.full_customer_key("Harris, Heather") != pm.full_customer_key("Harris, Loni")
    assert pm.full_customer_key("Harris, Heather") == pm.full_customer_key("Heather Harris")


# ── Lead Stage 1 auto-add fix ─────────────────────────────────────────────────────────────────
def test_lead_stage1_not_confirmed_on_mpf_is_not_auto_added(mock_pipeline, fixture_reports):
    """2026-09-04: a Lead Request Report row for a salesperson who never appears on the Master
    Pay File means they weren't able to quote the job — not a payable event."""
    fixture_reports["leadRequestReport"] = [
        {"State": "Completed", "Completer": "test tech one", "CustomerName": "Test Customer, Gamma",
         "LocationName": "", "LastModifiedDate": "2026-08-10"},
    ]
    result = run_month("Aug 2026")
    steven_emps = {e["name"]: e for e in result["emps"]["steven"]}
    assert "Test Tech One" not in steven_emps or steven_emps["Test Tech One"]["ins"] == 0
    assert result["carryForward"] == []  # no phantom Stage 2 should be seeded from this either


# ── Month-format normalization — 2026-09-02 incident ─────────────────────────────────────────
def test_norm_month_handles_iso_and_plain_string_identically():
    assert pm.norm_month("Aug 2026") == "Aug 2026"
    assert pm.norm_month("2026-08-01T04:00:00.000Z") == "Aug 2026"


## ── Phase 2: preflight() ──────────────────────────────────────────────────────────────────────
def test_preflight_blocks_on_skipped_month_baseline(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [
        {"EmployeeName": "Test Tech One", "Activity": "TGL Lead Set Res", "Date": "2026-05-05",
         "JobNumber": "999010", "GrossPay": 25.0, "CustomerName": "Test Customer, Epsilon",
         "LocationName": "", "LaborTypeCode": ""},
    ]
    run_month("May 2026")  # creates a real res_carry_forward baseline with 1 pending item
    fixture_reports["masterPayFile"] = []
    computed = pm.compute("Aug 2026")  # PREV_LABEL="Jul 2026" -- May exists, Jun/Jul don't
    pf = pm.preflight(computed)
    assert pf["blocked"], "a missing prior-month baseline (while other months exist) should block"
    assert "Jul 2026" in pf["message"]


def test_preflight_does_not_block_a_genuinely_fresh_deployment(mock_pipeline, fixture_reports):
    """No res_carry_forward history AT ALL (not even for a different month) is a legitimate
    brand-new deployment, not a skipped month -- must not require --bootstrap."""
    computed = pm.compute("Aug 2026")
    pf = pm.preflight(computed)
    assert not pf["blocked"], pf["message"]


def test_preflight_blocks_on_money_moving_orphan(mock_pipeline):
    mock_pipeline.tabs["carry_forward_resolutions"] = [
        ["Aug 2026", "steven", "cf_stale_id_that_wont_match", "Test Tech One", "some ref",
         "Lead Stage 2 — Nonexistent Customer", "75", "paid", "", "2026-08-01T00:00:00.000Z"],
    ]
    computed = pm.compute("Aug 2026")
    pf = pm.preflight(computed)
    assert pf["blocked"]
    assert "money-moving" in pf["message"]


def test_preflight_warns_but_does_not_block_on_cosmetic_orphan(mock_pipeline):
    mock_pipeline.tabs["carry_forward_resolutions"] = [
        ["Aug 2026", "steven", "cf_stale_id_that_wont_match", "Test Tech One", "some ref",
         "Lead Stage 2 — Nonexistent Customer", "75", "dead", "customer cancelled", "2026-08-01T00:00:00.000Z"],
    ]
    computed = pm.compute("Aug 2026")
    pf = pm.preflight(computed)
    assert not pf["blocked"], pf["message"]
    assert any("cosmetic" in w for w in pf["warnings"])


def test_preflight_blocks_on_large_payout_swing_on_rerun(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [
        {"EmployeeName": "Test Tech One", "Activity": "Sales Spiff", "Date": "2026-08-05",
         "JobNumber": "999011", "GrossPay": 500.0, "CustomerName": "Test Customer, Zeta",
         "LocationName": "", "LaborTypeCode": ""},
    ]
    run_month("Aug 2026")  # writes output_2026-08.json with $500 on the books
    fixture_reports["masterPayFile"] = []  # simulate a data source suddenly going empty
    computed = pm.compute("Aug 2026")
    pf = pm.preflight(computed)
    assert pf["blocked"]
    assert "change" in pf["message"].lower()


def test_preflight_blocked_run_raises_from_main_without_force(mock_pipeline):
    mock_pipeline.tabs["carry_forward_resolutions"] = [
        ["Aug 2026", "steven", "cf_stale", "Test Tech One", "ref", "Lead Stage 2 — X",
         "75", "paid", "", "2026-08-01T00:00:00.000Z"],
    ]
    with pytest.raises(pm.PreflightBlocked):
        pm.main("Aug 2026")


def test_preflight_force_override_proceeds_despite_block(mock_pipeline):
    mock_pipeline.tabs["carry_forward_resolutions"] = [
        ["Aug 2026", "steven", "cf_stale", "Test Tech One", "ref", "Lead Stage 2 — X",
         "75", "paid", "", "2026-08-01T00:00:00.000Z"],
    ]
    pm.main("Aug 2026", force_preflight=True)  # must not raise
    import os
    assert os.path.exists(pm._output_path_for("Aug 2026"))


def test_carry_forward_resolution_replays_regardless_of_month_cell_format(mock_pipeline, fixture_reports):
    """The actual 2026-09-02 bug: Google Sheets silently converts a plain 'Aug 2026' cell into a
    Date on write, and every r[0]===MONTH-style comparison used to fail against that. Here:
    a carry-forward baseline row with an ISO-datetime month cell must still be recognized as
    belonging to the previous month."""
    fixture_reports["masterPayFile"] = [
        {"EmployeeName": "Test Tech One", "Activity": "TGL Lead Set Res", "Date": "2026-07-05",
         "JobNumber": "999003", "GrossPay": 25.0, "CustomerName": "Test Customer, Delta",
         "LocationName": "", "LaborTypeCode": ""},
    ]
    jul_result = run_month("Jul 2026")
    cf_item = next(c for c in jul_result["carryForward"] if c["emp"] == "Test Tech One")

    # Simulate what Google Sheets actually does to the month cell on write.
    for row in mock_pipeline.tabs["res_carry_forward"]:
        if pm.norm_month(row[0]) == "Jul 2026":
            row[0] = "2026-07-01T04:00:00.000Z"

    fixture_reports["masterPayFile"] = []  # nothing new in August
    aug_result = run_month("Aug 2026")
    assert any(c["emp"] == "Test Tech One" and c["id"] == cf_item["id"] for c in aug_result["carryForward"]), (
        "carry-forward item vanished across the month boundary once its baseline row's month "
        "cell was an ISO datetime instead of a plain string"
    )
