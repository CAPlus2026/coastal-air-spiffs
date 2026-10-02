"""carry_forward_evidence.py — evidence-only verdicts for pending Lead Stage 2 items.

The signal is the ServiceTitan PROJECT: lead job -> estimate job (the replacement quotes) ->
installation job. Not "any completed job at the customer" (rejected 2026-09-04: another tech's
lead can get the credit, and unrelated service calls complete all the time)."""
import json

import carry_forward_evidence as cfe
import process_month as pm

TYPES = [{"id": 1, "name": "Estimate Install"}, {"id": 2, "name": "Install - Residential"},
         {"id": 3, "name": "Service Call"}, {"id": 4, "name": "TGL Lead"}]
UNITS = [{"id": 10, "name": "MB - Install Residential"}]


class FakeClient:
    tenant_id = "T1"

    def __init__(self, jobs_by_number=None, jobs_by_project=None, jobs_by_customer=None,
                 customers=None, types=None, units=None, fail_on=None):
        self.jobs_by_number = jobs_by_number or {}
        self.jobs_by_project = jobs_by_project or {}
        self.jobs_by_customer = jobs_by_customer or {}
        self.customers = customers or []
        self.types = TYPES if types is None else types
        self.units = UNITS if units is None else units
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
            if "projectId" in params:
                return self.jobs_by_project.get(params["projectId"], [])
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


LEAD = {"jobNumber": 100001, "customerId": 55, "projectId": 900, "jobTypeId": 4, "jobStatus": "Completed"}


def pj(num, type_id, status, completed="", unit=10):
    return {"jobNumber": num, "jobTypeId": type_id, "businessUnitId": unit, "jobStatus": status,
            "completedOn": completed, "projectId": 900}


def client_with_project(*jobs, **kw):
    return FakeClient(jobs_by_number={"100001": [LEAD]},
                      jobs_by_project={900: [pj(100001, 4, "Completed"), *jobs]}, **kw)


def test_mpf_sold_for_this_employee_is_paid_on_mpf_even_with_zero_pay():
    v, s, _ = evaluate(item(), FakeClient(), mpf=[sold("Test Tech One", "Smith, Pat", pay=0)])
    assert v == "paid_on_mpf" and "200" in s


def test_stage2_credited_to_someone_else_is_credited_elsewhere():
    v, s, _ = evaluate(item(), FakeClient(), mpf=[sold("Test Tech Two", "Smith, Pat")])
    assert v == "credited_elsewhere" and "Test Tech Two" in s


def test_project_with_estimate_then_completed_install_is_install_completed():
    c = client_with_project(pj(200, 1, "Completed", "2026-08-10T00:00:00Z"),
                            pj(300, 2, "Completed", "2026-09-12T00:00:00Z"))
    v, s, jobs = evaluate(item(), c)
    assert v == "install_completed"
    assert [j["job"] for j in jobs] == [300] and "2026-09-12" in s
    assert s.startswith("PAY Stage 2"), "a completed install must be an instruction, not a 'please verify'"
    assert "verify" not in s.lower()
    # looked the lead job up by number, then listed the PROJECT's jobs — never a customer-wide search
    assert [p for path, p in c.calls if path.endswith("/jobs")][1] == {"projectId": 900}
    assert not any(path.endswith("/customers") for path, _ in c.calls)


def test_a_completed_service_call_in_the_project_is_not_an_install():
    c = client_with_project(pj(200, 1, "Completed", "2026-08-10T00:00:00Z"),
                            pj(301, 3, "Completed", "2026-09-13T00:00:00Z"))
    assert evaluate(item(), c)[0] == "estimate_only"


def test_install_job_not_yet_completed_is_in_progress():
    c = client_with_project(pj(200, 1, "Completed", "2026-08-10T00:00:00Z"), pj(300, 2, "Scheduled"))
    v, s, _ = evaluate(item(), c)
    assert v == "install_in_progress" and "Scheduled" in s


def test_estimate_without_install_is_estimate_only():
    c = client_with_project(pj(200, 1, "Completed", "2026-08-10T00:00:00Z"))
    assert evaluate(item(), c)[0] == "estimate_only"


def test_lead_job_alone_in_its_project_is_no_install_found():
    assert evaluate(item(), client_with_project())[0] == "no_install_found"


def test_lead_job_with_no_project_is_no_install_found():
    c = FakeClient(jobs_by_number={"100001": [{**LEAD, "projectId": None}]})
    v, s, _ = evaluate(item(), c)
    assert v == "no_install_found" and "project" in s


def test_estimate_job_type_in_an_install_business_unit_is_still_an_estimate():
    """The Install business unit holds the estimate jobs too — type name wins over business unit."""
    c = client_with_project(pj(200, 1, "Completed", "2026-08-10T00:00:00Z", unit=10))
    assert evaluate(item(), c)[0] == "estimate_only"


def test_unknown_job_type_falls_back_to_the_business_unit_name():
    c = client_with_project(pj(300, 99, "Completed", "2026-09-12T00:00:00Z", unit=10))
    assert evaluate(item(), c)[0] == "install_completed"


def test_lead_job_number_not_found_is_unknown():
    v, s, _ = evaluate(item(), FakeClient())
    assert v == "unknown" and "100001" in s


def test_blank_ref_falls_back_to_customer_name_and_says_to_double_check():
    c = FakeClient(customers=[{"id": 7, "name": "Smith, Patricia"}, {"id": 8, "name": "Smith, Pat"}],
                   jobs_by_customer={8: [{"jobNumber": 1, "projectId": 900}]},
                   jobs_by_project={900: [pj(300, 2, "Completed", "2026-09-12T00:00:00Z")]})
    v, s, _ = evaluate(item(ref=""), c)
    assert v == "install_completed" and "double-check" in s
    assert [p for path, p in c.calls if path.endswith("/jobs")][0]["createdOnOrAfter"] == "2026-08-01"


def test_blank_ref_and_no_matching_customer_is_unknown():
    assert evaluate(item(ref=""), FakeClient(customers=[]))[0] == "unknown"


def test_lookup_failure_is_unknown_not_a_crash():
    v, s, _ = evaluate(item(), FakeClient(jobs_by_number={"100001": [LEAD]}, fail_on="/jobs"))
    assert v == "unknown" and "boom" in s


def test_run_writes_one_row_per_pending_item_and_never_resolves_anything(mock_pipeline, monkeypatch, tmp_path):
    out = {"carryForward": [item(), {**item(emp="Test Tech Two", ref="Job 100002"), "id": "cf_2"},
                            {**item(), "id": "cf_done", "resolved": True}]}
    monkeypatch.chdir(tmp_path)
    (tmp_path / pm._output_path_for("Sep 2026")).write_text(json.dumps(out))
    c = FakeClient(jobs_by_number={"100001": [LEAD], "100002": [{**LEAD, "jobNumber": 100002, "projectId": 901}]},
                   jobs_by_project={900: [pj(300, 2, "Completed", "2026-09-12T00:00:00Z")], 901: []})
    rows = cfe.run("Sep 2026", client=c, mpf_rows=[])
    assert [(r[1], r[3]) for r in rows] == [("cf_1", "install_completed"), ("cf_2", "no_install_found")]
    assert [r[1] for r in mock_pipeline.get("cf_evidence")] == ["cf_1", "cf_2"]
    assert mock_pipeline.get("carry_forward_resolutions") == [], "evidence must never write a resolution"
