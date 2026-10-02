"""carry_forward_evidence.py — evidence-only verdicts for pending Lead Stage 2 items."""
import json

import carry_forward_evidence as cfe
import process_month as pm


class FakeClient:
    """Canned ServiceTitan REST responses keyed by (path suffix, key param)."""
    tenant_id = "T1"

    def __init__(self, jobs_by_number=None, jobs_by_customer=None, customers=None,
                 types=None, units=None, fail_on=None):
        self.jobs_by_number = jobs_by_number or {}
        self.jobs_by_customer = jobs_by_customer or {}
        self.customers = customers or []
        self.types = types if types is not None else [{"id": 1, "name": "Install - Residential"}, {"id": 2, "name": "Service Call"}]
        self.units = units if units is not None else [{"id": 10, "name": "MB - Install Residential"}, {"id": 11, "name": "MB - Service"}]
        self.fail_on = fail_on
        self.calls = []

    def get_paged(self, path, params=None):
        params = params or {}
        self.calls.append((path, dict(params)))
        if self.fail_on and self.fail_on in path:
            raise RuntimeError("boom")
        if path.endswith("/job-types"):
            return self.types
        if path.endswith("/business-units"):
            return self.units
        if path.endswith("/customers"):
            return self.customers
        if path.endswith("/jobs"):
            if "number" in params:
                return self.jobs_by_number.get(str(params["number"]), [])
            return self.jobs_by_customer.get(params.get("customerId"), [])
        raise AssertionError(path)


def item(emp="Test Tech One", ref="Job 100001", cust="Smith, Pat", frm="Aug 2026"):
    return {"id": "cf_1", "emp": emp, "ref": ref, "type": f"Lead Stage 2 — {cust}", "amount": 75,
            "fromMonth": frm, "resolved": False}


def evaluate(it, client, mpf=(), ledger=()):
    by_emp, by_cust = cfe.build_mpf_indexes(list(mpf))
    types, units, _ = cfe.load_lookups(client)
    return cfe.evaluate(it, by_emp, by_cust, list(ledger), client, types, units)


def sold(emp, cust, job="200", date="2026-09-10", pay=0):
    return {"EmployeeName": emp, "Activity": "TGL Lead Sold Res", "CustomerName": cust,
            "JobNumber": job, "Date": date, "GrossPay": pay}


LEAD_JOB = {"jobNumber": 100001, "customerId": 55, "createdOn": "2026-08-03T12:00:00Z"}


def test_mpf_sold_for_this_employee_is_paid_on_mpf_even_with_zero_pay():
    v, s, _ = evaluate(item(), FakeClient(), mpf=[sold("Test Tech One", "Smith, Pat", pay=0)])
    assert v == "paid_on_mpf" and "200" in s


def test_stage2_credited_to_someone_else_is_credited_elsewhere():
    v, s, _ = evaluate(item(), FakeClient(), mpf=[sold("Test Tech Two", "Smith, Pat")])
    assert v == "credited_elsewhere" and "Test Tech Two" in s


def test_completed_install_without_credit_is_install_completed():
    c = FakeClient(jobs_by_number={"100001": [LEAD_JOB]}, jobs_by_customer={55: [
        {"jobNumber": 300, "jobTypeId": 1, "businessUnitId": 10, "completedOn": "2026-09-12T00:00:00Z"},
        {"jobNumber": 301, "jobTypeId": 2, "businessUnitId": 11, "completedOn": "2026-09-13T00:00:00Z"}]})
    v, s, jobs = evaluate(item(), c)
    assert v == "install_completed"
    assert [j["job"] for j in jobs] == [300], "a plain service call must not count as an install"
    assert "verify" in s.lower()
    # the lookup used the lead's own job number, then filtered to Completed since the lead was created
    cust_call = [p for path, p in c.calls if path.endswith("/jobs") and "customerId" in p][0]
    assert cust_call["jobStatus"] == "Completed" and cust_call["completedOnOrAfter"] == "2026-08-03"


def test_lead_job_itself_is_not_counted_as_the_install():
    c = FakeClient(jobs_by_number={"100001": [LEAD_JOB]}, jobs_by_customer={55: [
        {"jobNumber": 100001, "jobTypeId": 1, "businessUnitId": 10, "completedOn": "2026-08-05T00:00:00Z"}]})
    assert evaluate(item(), c)[0] == "no_install_found"


def test_no_completed_job_is_no_install_found():
    c = FakeClient(jobs_by_number={"100001": [LEAD_JOB]}, jobs_by_customer={55: []})
    assert evaluate(item(), c)[0] == "no_install_found"


def test_blank_ref_falls_back_to_exact_customer_name_search():
    c = FakeClient(customers=[{"id": 7, "name": "Smith, Patricia"}, {"id": 8, "name": "Smith, Pat"}],
                   jobs_by_customer={8: [{"jobNumber": 400, "jobTypeId": 1, "businessUnitId": 10,
                                          "completedOn": "2026-09-20T00:00:00Z"}]})
    v, _, jobs = evaluate(item(ref=""), c)
    assert v == "install_completed" and jobs[0]["job"] == 400
    assert [p for path, p in c.calls if path.endswith("/jobs")][0]["completedOnOrAfter"] == "2026-08-01"


def test_customer_not_found_is_unknown():
    assert evaluate(item(ref=""), FakeClient(customers=[]))[0] == "unknown"


def test_lookup_failure_is_unknown_not_a_crash():
    c = FakeClient(jobs_by_number={"100001": [LEAD_JOB]}, fail_on="/jobs")
    v, s, _ = evaluate(item(), c)
    assert v == "unknown" and "boom" in s


def test_missing_type_tables_count_every_completed_job():
    c = FakeClient(jobs_by_number={"100001": [LEAD_JOB]}, types=[], units=[], jobs_by_customer={55: [
        {"jobNumber": 500, "jobTypeId": 2, "businessUnitId": 11, "completedOn": "2026-09-01T00:00:00Z"}]})
    assert evaluate(item(), c)[0] == "install_completed"


def test_run_writes_one_row_per_pending_item_and_never_resolves_anything(mock_pipeline, monkeypatch, tmp_path):
    out = {"carryForward": [item(), {**item(emp="Test Tech Two", ref="Job 100002"), "id": "cf_2"},
                            {**item(), "id": "cf_done", "resolved": True}]}
    monkeypatch.chdir(tmp_path)
    (tmp_path / pm._output_path_for("Sep 2026")).write_text(json.dumps(out))
    c = FakeClient(jobs_by_number={"100001": [LEAD_JOB], "100002": [{**LEAD_JOB, "jobNumber": 100002, "customerId": 56}]},
                   jobs_by_customer={55: [{"jobNumber": 300, "jobTypeId": 1, "businessUnitId": 10,
                                           "completedOn": "2026-09-12T00:00:00Z"}], 56: []})
    rows = cfe.run("Sep 2026", client=c, mpf_rows=[])
    assert [(r[1], r[3]) for r in rows] == [("cf_1", "install_completed"), ("cf_2", "no_install_found")]
    stored = mock_pipeline.get("cf_evidence")
    assert [r[1] for r in stored] == ["cf_1", "cf_2"]
    assert mock_pipeline.get("carry_forward_resolutions") == [], "evidence must never write a resolution"
