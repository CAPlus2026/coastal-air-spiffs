"""Quick fixes approved by Billy 2026-11-04 (branch quick-fixes-2026-11):
 (a) "TGL Lead Sold Res" and "TGL Sold Res" both match, everywhere
 (b) commercial lead spiff paid once, only from the commercial salesperson's submitted list;
     ST "TGL Lead Sold Comm" is a duplicate check/flag, never a payout
 (c) Rich's 3% — completed, service-related sales only (component replacements count)
 (d) unrecognized people are held in "Needs review", not put on Steven's team
 (e) installers excluded entirely
 plus: "SoldBy Commission" / "Invoice Specific Bonus" MPF lines are flagged, never paid.
Synthetic names/jobs only (public repo).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import process_month as pm  # noqa: E402
import carry_forward_evidence as cfe  # noqa: E402
import reconcile_carry_forward as rcf  # noqa: E402

from conftest import run_month  # noqa: E402


def mpf(name, activity, job, pay, customer="Test, Cust"):
    return {"EmployeeName": name, "Activity": activity, "Date": "2026-08-05", "JobNumber": job,
            "GrossPay": pay, "CustomerName": customer, "LocationName": "", "LaborTypeCode": ""}


def total(result, name):
    for mgr in ("steven", "caleb"):
        for e in result["emps"][mgr]:
            if e["name"] == name:
                return round(sum(e[c] for c in ("svc", "ins", "plb", "com", "chs", "chi")), 2)
    return 0


def all_flags(result):
    return [f for fl in result["flags"].values() for f in fl]


# ── (a) ──────────────────────────────────────────────────────────────────────
def test_both_tgl_sold_names_are_lead_activities():
    assert {"TGL Lead Sold Res", "TGL Sold Res"} <= pm.TGL_SOLD_RES_ACTIVITIES
    assert pm.TGL_SOLD_RES_ACTIVITIES <= pm.LEAD_ACTIVITIES


def test_duplicate_stage2_detected_across_old_and_new_names(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [
        mpf("Test Tech One", "TGL Lead Sold Res", "11", 75.0, "Doe, Jane"),
        mpf("Test Tech One", "TGL Sold Res", "12", 75.0, "Doe, Jane"),
    ]
    result = run_month("Aug 2026")
    assert any("Duplicate TGL Stage 2" in f["title"] for f in result["flags"]["steven"])


def test_new_tgl_sold_name_is_classified_as_lead_column(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [mpf("Test Tech One", "TGL Sold Res", "13", 75.0)]
    result = run_month("Aug 2026")
    e = next(e for e in result["emps"]["steven"] if e["name"] == "Test Tech One")
    assert e["ins"] == 75 and e["svc"] == 0


def test_carry_forward_helpers_use_the_shared_name_set():
    for mod in (cfe, rcf):
        src = Path(mod.__file__).read_text()
        assert "pm.TGL_SOLD_RES_ACTIVITIES" in src
        assert '!= "TGL Lead Sold Res"' not in src


# ── (b) ──────────────────────────────────────────────────────────────────────
def sub(job, tech="Test Comm Tech", completed="TRUE", month="Aug 2026", cust="Acme, Inc"):
    return [month, "Test Salesperson", job, cust, tech, "Tech lead", completed, "2026-08-20", "", ""]


def test_st_tgl_lead_sold_comm_is_never_paid(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [mpf("Test Comm Tech", "TGL Lead Sold Comm", "21", 100.0)]
    result = run_month("Aug 2026")
    assert total(result, "Test Comm Tech") == 0
    assert any("no salesperson submission" in f["title"] for f in all_flags(result))


def test_commercial_lead_paid_once_from_submission_and_st_line_is_flagged_duplicate(mock_pipeline, fixture_reports):
    mock_pipeline.tabs[pm.COMM_SUBMISSIONS_TAB] = [sub("22")]
    fixture_reports["masterPayFile"] = [mpf("Test Comm Tech", "TGL Lead Sold Comm", "22", 100.0)]
    result = run_month("Aug 2026")
    assert total(result, "Test Comm Tech") == pm.COMM_LEAD_SPIFF
    assert any("Duplicate check" in f["title"] for f in all_flags(result))
    ledger = [r for r in mock_pipeline.get("spiff_ledger") if r[8] == "comm-submission"]
    assert len(ledger) == 1


def test_commercial_lead_not_paid_again_next_month(mock_pipeline, fixture_reports):
    mock_pipeline.tabs[pm.COMM_SUBMISSIONS_TAB] = [sub("23")]
    run_month("Aug 2026")
    mock_pipeline.tabs[pm.COMM_SUBMISSIONS_TAB].append(sub("23", month="Sep 2026"))
    result = run_month("Sep 2026")
    assert total(result, "Test Comm Tech") == 0
    assert any("already paid" in f["title"] for f in all_flags(result))


def test_incomplete_submission_does_not_pay(mock_pipeline, fixture_reports):
    mock_pipeline.tabs[pm.COMM_SUBMISSIONS_TAB] = [sub("24", completed="FALSE")]
    result = run_month("Aug 2026")
    assert total(result, "Test Comm Tech") == 0


def test_resubmission_marks_complete_last_row_wins(mock_pipeline, fixture_reports):
    mock_pipeline.tabs[pm.COMM_SUBMISSIONS_TAB] = [sub("25", completed="FALSE"), sub("25")]
    result = run_month("Aug 2026")
    assert total(result, "Test Comm Tech") == pm.COMM_LEAD_SPIFF


def test_submission_for_unknown_lead_tech_is_held(mock_pipeline, fixture_reports):
    mock_pipeline.tabs[pm.COMM_SUBMISSIONS_TAB] = [sub("26", tech="Nobody Known")]
    result = run_month("Aug 2026")
    assert result["needsReview"] == [] or all(n["name"] != "Nobody Known" or n["amount"] == 0
                                              for n in result["needsReview"])
    assert any("not on roster" in f["title"] for f in result["flags"]["review"])
    assert pm._grand_total(result) == 0


def test_comm_log_lead_closes_from_submission_without_double_pay(mock_pipeline, fixture_reports):
    # A lead carried in the Commercial Leads Log from July; the salesperson submits it in August.
    mock_pipeline.tabs["res_comm_leads"] = [
        ["Jul 2026", "cl_test", "Jul 2026", "Test Comm Tech", "27", "Acme, Inc", "Pending", 0, "", "false"]]
    mock_pipeline.tabs[pm.COMM_SUBMISSIONS_TAB] = [sub("27")]
    result = run_month("Aug 2026")
    lead = next(l for l in result["commLeads"] if l["id"] == "cl_test")
    assert lead["paid"] is True and lead["spiff"] == 0
    assert total(result, "Test Comm Tech") == pm.COMM_LEAD_SPIFF  # paid exactly once


def test_comm_log_lead_without_submission_pays_nothing(mock_pipeline, fixture_reports):
    mock_pipeline.tabs["res_comm_leads"] = [
        ["Jul 2026", "cl_test2", "Jul 2026", "Test Comm Tech", "28", "Beta, LLC", "Pending", 0, "", "false"]]
    fixture_reports["masterPayFile"] = [mpf("Test Comm Tech", "Sales Spiff", "28", 15.0, "Beta, LLC")]
    result = run_month("Aug 2026")
    lead = next(l for l in result["commLeads"] if l["id"] == "cl_test2")
    assert lead["paid"] is False  # an MPF line on the same job no longer closes/pays the lead
    assert total(result, "Test Comm Tech") == 15.0


# ── SoldBy Commission / Invoice Specific Bonus ─────────────────────────────────
def test_non_spiff_st_items_are_flagged_not_paid(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [
        mpf("Test Tech One", "SoldBy Commission", "31", 250.0),
        mpf("Test Tech One", "Invoice Specific Bonus", "32", 40.0),
        mpf("Test Tech One", "Sales Spiff", "33", 20.0),
    ]
    result = run_month("Aug 2026")
    assert total(result, "Test Tech One") == 20.0
    titles = [f["title"] for f in result["flags"]["steven"]]
    assert any("SoldBy Commission" in t for t in titles)
    assert any("Invoice Specific Bonus" in t for t in titles)


# ── (c) ──────────────────────────────────────────────────────────────────────
def rich(item, sale, job="41"):
    return {"SoldBy": "Rich Smith", "EstimateSalesInstalled": sale, "InstallJobs": job,
            "LocationName": "Test Loc", "EstimateName": item, "SoldOn": "2026-08-03"}


def test_classify_rich_item():
    assert pm.classify_rich_item("Compressor Replacement")[0] == "pay"
    assert pm.classify_rich_item("Water Heater Replacement 50 gal")[0] == "pay"
    assert pm.classify_rich_item("Pool Boiler Replacement")[0] == "pay"
    assert pm.classify_rich_item("Repair - refrigerant leak")[0] == "pay"
    assert pm.classify_rich_item("Full System Replacement 5 ton")[0] == "exclude"
    assert pm.classify_rich_item("Heat Pump Replacement")[0] == "exclude"
    assert pm.classify_rich_item("Damper replacement")[0] == "review"


def test_rich_paid_only_on_completed_service_sales(mock_pipeline, fixture_reports):
    fixture_reports["richCommissionReport"] = [
        rich("Compressor Replacement", 1000.0, "41"),
        rich("Full System Replacement", 10000.0, "42"),
        rich("Service repair", 500.0, ""),      # no install job yet — not completed
        rich("Water Heater Replacement", 0, "44"),  # nothing installed yet
        {**rich("Compressor Replacement", 900.0), "SoldBy": "Someone Else"},
    ]
    result = run_month("Aug 2026")
    b = next(b for b in result["bonuses"] if b["id"] == "rich")
    assert b["amount"] == 30.0
    assert [d["job"] for d in b["details"]] == ["41"]
    assert any("Rich 3% excluded" in f["title"] for f in result["flags"]["review"])


# ── (d) ──────────────────────────────────────────────────────────────────────
def test_unknown_person_held_in_needs_review_not_on_steven(mock_pipeline, fixture_reports):
    fixture_reports["masterPayFile"] = [mpf("Brand New Person", "Sales Spiff", "51", 60.0)]
    result = run_month("Aug 2026")
    assert all(e["name"] != "Brand New Person" for e in result["emps"]["steven"])
    assert "Brand New Person" not in result["spiffDetail"]["steven"]
    held = next(n for n in result["needsReview"] if n["name"] == "Brand New Person")
    assert held["amount"] == 60.0
    assert pm._grand_total(result) == 0
    assert any(f["emp"] == "Brand New Person" for f in result["flags"]["review"])
    assert not [r for r in mock_pipeline.get("spiff_ledger") if r[2] == "Brand New Person"]


# ── (e) ──────────────────────────────────────────────────────────────────────
def test_installer_role_on_roster_is_excluded(mock_pipeline, fixture_reports):
    mock_pipeline.tabs["roster"].append(
        ["Test Installer", "steven", "installer", "TRUE", "TRUE", "", "test", "2026-01-01T00:00:00.000Z"])
    fixture_reports["masterPayFile"] = [mpf("Test Installer", "Sales Spiff", "61", 50.0)]
    result = run_month("Aug 2026")
    assert total(result, "Test Installer") == 0
    assert all(n["name"] != "Test Installer" for n in result["needsReview"])


def test_unknown_person_with_install_bu_is_excluded_not_held(mock_pipeline, fixture_reports):
    fixture_reports["technicianPerformance"] = [
        {"Name": "Unrostered Installer", "TechnicianBusinessUnit": "CH - Install Residential HVAC"}]
    fixture_reports["masterPayFile"] = [mpf("Unrostered Installer", "Sales Spiff", "62", 50.0)]
    result = run_month("Aug 2026")
    assert all(n["name"] != "Unrostered Installer" for n in result["needsReview"])
    assert pm._grand_total(result) == 0
    f = next(f for f in result["flags"]["review"] if f["emp"] == "Unrostered Installer")
    assert f["resolved"] is True


def test_rostered_tech_with_install_bu_is_flagged(mock_pipeline, fixture_reports):
    fixture_reports["technicianPerformance"] = [
        {"Name": "Test CH Tech", "TechnicianBusinessUnit": "CH - Install Residential HVAC"}]
    fixture_reports["masterPayFile"] = [mpf("Test CH Tech", "Sales Spiff", "63", 50.0)]
    result = run_month("Aug 2026")
    assert any("Possible installer" in f["title"] for f in result["flags"]["caleb"])
