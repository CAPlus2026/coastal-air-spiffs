"""Tests for audit_orphans.py and migrate_log_ids.py — built out fully 2026-09-04 after being
asked directly whether Phase 3 (the migration helper) had been skipped. It hadn't been skipped
entirely (migrate_log_ids.py was pulled forward early because Phase 2 needed it), but it only
covered 2 of the 4 at-risk tabs. These tests cover the other two (flags, spiff_corrections) and
the real bug found while finishing them out: audit_flags()'s matching key silently ignored the
emp/ref columns from the very beginning.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import audit_orphans  # noqa: E402
import migrate_log_ids  # noqa: E402
import process_month as pm  # noqa: E402

from conftest import run_month  # noqa: E402


def test_audit_flags_matches_on_emp_and_ref_not_just_detail(mock_pipeline, fixture_reports):
    """BUG (found 2026-09-04): latest_flags() never extracted emp/ref from the raw row, so the
    matching key was silently always (mgr, "", "", detail) — two DIFFERENT flags whose id changed
    would only be told apart by detail text happening to differ, which isn't guaranteed."""
    fixture_reports["masterPayFile"] = [
        # Two unrecognized-employee flags, deliberately with an IDENTICAL detail template except
        # for the name interpolated into it -- close enough to catch a matcher that ignores emp/ref.
        {"EmployeeName": "Unknown Person Alpha", "Activity": "Sales Spiff", "Date": "2026-08-01",
         "JobNumber": "1", "GrossPay": 10.0, "CustomerName": "Test, One", "LocationName": "", "LaborTypeCode": ""},
        {"EmployeeName": "Unknown Person Beta", "Activity": "Sales Spiff", "Date": "2026-08-01",
         "JobNumber": "2", "GrossPay": 10.0, "CustomerName": "Test, Two", "LocationName": "", "LaborTypeCode": ""},
    ]
    result = run_month("Aug 2026")
    flags = result["flags"]["steven"]
    alpha_flag = next(f for f in flags if f["emp"] == "Unknown Person Alpha")
    beta_flag = next(f for f in flags if f["emp"] == "Unknown Person Beta")
    assert alpha_flag["id"] != beta_flag["id"]

    # latest_flags() reads columns 5/6 as emp/ref -- confirm they round-trip correctly, which is
    # the actual bug: they used to be silently dropped.
    latest = audit_orphans.latest_flags("Aug 2026")
    # (no resolution rows exist yet in this fixture, so latest_flags legitimately returns {} here
    # -- write one by hand to exercise the column parsing.)
    mock_pipeline.tabs["flags"] = [
        ["Aug 2026", "steven", alpha_flag["id"], "red", "red", "Unknown Person Alpha",
         alpha_flag["ref"], "", alpha_flag["detail"], "Not paid", "test note", "true",
         "2026-08-01T00:00:00.000Z"],
    ]
    latest = audit_orphans.latest_flags("Aug 2026")
    row = latest[alpha_flag["id"]]
    assert row["emp"] == "Unknown Person Alpha"
    assert row["ref"] == alpha_flag["ref"]


def test_audit_flags_already_remediated(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [
        {"EmployeeName": "Unknown Person Gamma", "Activity": "Sales Spiff", "Date": "2026-08-01",
         "JobNumber": "3", "GrossPay": 10.0, "CustomerName": "Test, Three", "LocationName": "", "LaborTypeCode": ""},
    ]
    result = run_month("Aug 2026")
    flag = result["flags"]["steven"][0]
    mock_pipeline.tabs["flags"] = [
        # Old (stale) id, resolved.
        ["Aug 2026", "steven", "flag_stale_id", flag["sev"], flag["sev"], flag["emp"], flag["ref"],
         "", flag["detail"], "Not paid", "old note", "true", "2026-08-01T00:00:00.000Z"],
        # Corrected row under the real current id -- makes the stale one "already_remediated".
        ["Aug 2026", "steven", flag["id"], flag["sev"], flag["sev"], flag["emp"], flag["ref"],
         "", flag["detail"], "Not paid", "old note", "true", "2026-08-01T00:00:01.000Z"],
    ]
    orphans = audit_orphans.audit_flags(result, "Aug 2026")
    stale = next(o for o in orphans if o["old_id"] == "flag_stale_id")
    assert stale["verdict"] == "already_remediated"
    assert stale["money_moving"] is False


def test_migrate_log_ids_applies_flags_migration(mock_pipeline, fixture_reports, tmp_path):
    fixture_reports["masterPayFile"] = [
        {"EmployeeName": "Unknown Person Delta", "Activity": "Sales Spiff", "Date": "2026-08-01",
         "JobNumber": "4", "GrossPay": 10.0, "CustomerName": "Test, Four", "LocationName": "", "LaborTypeCode": ""},
    ]
    result = run_month("Aug 2026")
    flag = result["flags"]["steven"][0]
    old_id = "flag_stale_id_for_migration_test"
    mock_pipeline.tabs["flags"] = [
        ["Aug 2026", "steven", old_id, flag["sev"], flag["sev"], flag["emp"], flag["ref"],
         "", flag["detail"], "Not paid", "a real note", "true", "2026-08-01T00:00:00.000Z"],
    ]
    orphan = next(o for o in audit_orphans.audit_flags(result, "Aug 2026") if o["old_id"] == old_id)
    assert orphan["verdict"] == "recoverable"

    migrate_log_ids.apply_flags(orphan, result, "Aug 2026")
    migrated_rows = [r for r in mock_pipeline.get("flags") if len(r) > 13 and r[13] == old_id]
    assert len(migrated_rows) == 1
    assert migrated_rows[0][2] == flag["id"]  # new id
    assert migrated_rows[0][9] == "Not paid"  # disposition preserved


def test_migrate_log_ids_applies_spiff_corrections_migration(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [
        {"EmployeeName": "Test Tech One", "Activity": "Sales Spiff", "Date": "2026-08-05",
         "JobNumber": "999020", "GrossPay": 25.0, "CustomerName": "Test Customer, Migrate",
         "LocationName": "", "LaborTypeCode": ""},
    ]
    result = run_month("Aug 2026")
    line = result["spiffDetail"]["steven"]["Test Tech One"][0]
    old_key = "steven|Test Tech One|0"
    # A legacy-shaped row (no natural-key columns) would be unrecoverable by design -- this row
    # HAS the natural-key columns but under a since-changed lineId, which is the case this tool
    # can actually fix.
    mock_pipeline.tabs["spiff_corrections"] = [
        ["Aug 2026", "steven", "Test Tech One", "0", "flagged", "test reason",
         "2026-08-01T00:00:00.000Z", "ln_some_old_id_that_no_longer_exists",
         line["job"], line["customer"], line["type"], line["item"], str(line["spiff"])],
    ]
    orphan = next(o for o in audit_orphans.audit_spiff_corrections(result, "Aug 2026")
                  if o["old_id"] == old_key)
    assert orphan["verdict"] == "recoverable"
    assert orphan["candidates"][0] == line["lineId"]

    migrate_log_ids.apply_spiff_corrections(orphan, result, "Aug 2026")
    migrated_rows = [r for r in mock_pipeline.get("spiff_corrections") if len(r) > 13 and r[13] == old_key]
    assert len(migrated_rows) == 1
    assert migrated_rows[0][7] == line["lineId"]  # new lineId
    assert migrated_rows[0][4] == "flagged"  # action preserved
