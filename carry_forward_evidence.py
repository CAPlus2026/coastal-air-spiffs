"""Evidence report for pending carry-forward items ("Lead Stage 2" payouts waiting on a sale).

Answers, per item, the question a manager otherwise has to look up by hand in ServiceTitan one at
a time: did this lead turn into a completed install?

This is EVIDENCE ONLY — it never resolves, pays or dismisses anything. The business owner's rule
(2026-09-04, see reconcile_carry_forward.py) is that a completed job at the same customer is NOT
proof this employee's lead was credited: ServiceTitan links an eventual sold estimate to exactly
one lead, so a different tech's lead can get the credit. So each item gets one verdict, strongest
first, and the manager still makes the call:

  departed            The employee is marked departed on the roster — don't pay; mark dead unless
                      you decide otherwise. (Checked first.)
  paid_on_mpf         Master Pay File itself shows TGL Lead Sold Res for this employee + customer
                      (it was already paid in payroll — clear it, never pay it again).
  credited_elsewhere  Master Pay File / the payout ledger shows the Stage 2 went to someone ELSE
                      for this customer — this employee's lead is dead.
  install_completed   The lead job's PROJECT in ServiceTitan contains an installation job that is
                      Completed (lead job -> "Estimate Install" job with the replacement quotes ->
                      install job). No Stage 2 credited yet — the card says PAY Stage 2.
  install_in_progress The project has an installation job that isn't completed yet — sold, pay
                      when the install finishes.
  estimate_only       The project has an estimate job but no installation job — quoted, not sold.
  no_install_found    The lead job has no estimate/install work in its project — still open.
  unknown             Couldn't check (lookup failed) — the reason is in the summary.

Written to the `cf_evidence` Sheet tab (one row per item, replaced per month) and shown on each
carry-forward card and on Billy's Overview. Called at the end of every self-service run
(run_requested_month.py) inside a try/except — a failure here must never fail the payroll run.
"""
import json
import re
import time
from collections import defaultdict

import process_month as pm

HEADERS = ["month", "id", "emp", "verdict", "summary", "jobs", "checkedAt"]


def _customer_of(item):
    t = item.get("type", "")
    return t.split(" — ", 1)[-1].strip() if " — " in t else ""


def _job_number(ref):
    m = re.search(r"(\d{6,})", str(ref or ""))
    return m.group(1) if m else None


def _month_start(label):
    try:
        mon, year = label.split()
        return f"{year}-{pm._MONTH_NAMES.index(mon) + 1:02d}-01"
    except (ValueError, AttributeError):
        return ""


def _paged(client, path, params=None):
    return client.get_paged(f"/{path.lstrip('/')}".replace("{t}", str(client.tenant_id)), params or {})


def load_lookups(client):
    """job type id -> name, business unit id -> name. Each is best-effort: if one fails the
    install-type test just falls back to the other (or to 'any completed job')."""
    types, units, errors = {}, {}, []
    try:
        for t in _paged(client, "jpm/v2/tenant/{t}/job-types"):
            types[t.get("id")] = t.get("name", "")
    except Exception as e:  # noqa: BLE001 — best-effort lookup
        errors.append(f"job types: {e}")
    try:
        for b in _paged(client, "settings/v2/tenant/{t}/business-units"):
            units[b.get("id")] = b.get("name", "")
    except Exception as e:  # noqa: BLE001
        errors.append(f"business units: {e}")
    return types, units, errors


def find_lead_job(client, item):
    """The lead's own job, by the item's `ref` ('Job 169257990'). Returns the ServiceTitan job
    dict or None. Items with a blank ref are handled by find_via_customer() instead."""
    num = _job_number(item.get("ref"))
    if not num:
        return None
    jobs = _paged(client, "jpm/v2/tenant/{t}/jobs", {"number": num})
    return jobs[0] if jobs else None


def classify_job(job, types, units):
    """'estimate' | 'install' | 'lead' | 'other' from the job TYPE name. The estimate job is the
    "Estimate Install" type (it carries the replacement quotes); the installation job is any other
    install-type job. Estimate is tested first because "Estimate Install" contains "install". The
    business unit is only a fallback when the type is unknown — the Install business unit holds
    the estimate jobs too."""
    tname = types.get(job.get("jobTypeId"), "") or ""
    name = tname or units.get(job.get("businessUnitId"), "") or ""
    if re.search(r"callback|call.?back|warranty|recall|re-?do", name, re.I):
        return "other", tname  # a redo of earlier work (e.g. "Callback Install"), not a new sale
    if re.search(r"estimate|quote|proposal", name, re.I):
        return "estimate", tname
    if re.search(r"install|replac|change.?out", name, re.I):
        return "install", tname
    if re.search(r"lead", name, re.I):
        return "lead", tname
    return "other", tname


def project_jobs(client, project_id, lead_job_number, types, units):
    """Every job in the lead job's project (all statuses), minus the lead job itself, each tagged
    with its role. Projects are what link lead -> estimate -> installation in ServiceTitan."""
    out = []
    for j in _paged(client, "jpm/v2/tenant/{t}/jobs", {"projectId": project_id}):
        if lead_job_number and str(j.get("jobNumber")) == str(lead_job_number):
            continue
        role, tname = classify_job(j, types, units)
        out.append({"job": j.get("jobNumber"), "role": role, "type": tname,
                    "status": j.get("jobStatus") or "", "completedOn": (j.get("completedOn") or "")[:10]})
    return out


def find_via_customer(client, item):
    """Blank-ref items have no lead job number to start from. Falls back to an exact customer-name
    match, then the project(s) of that customer's jobs created since the lead. Lower confidence —
    the summary says so."""
    name = _customer_of(item)
    if not name:
        return []
    key = pm.full_customer_key(name)
    since = _month_start(item.get("fromMonth", ""))
    projects, seen = [], set()
    for c in _paged(client, "crm/v2/tenant/{t}/customers", {"name": name}):
        if pm.full_customer_key(c.get("name", "")) != key:
            continue
        params = {"customerId": c.get("id")}
        if since:
            params["createdOnOrAfter"] = since
        for j in _paged(client, "jpm/v2/tenant/{t}/jobs", params):
            pid = j.get("projectId")
            if pid and pid not in seen:
                seen.add(pid)
                projects.append(pid)
    return projects


def verdict_from_jobs(emp, jobs, via_name=False):
    """Plain instructions, not 'please verify': the lead job's project is the link, so a completed
    installation job in it means Stage 2 is due. Only a customer-name match (blank-ref items, no
    job number to start from) carries a caveat."""
    installs = [j for j in jobs if j["role"] == "install"]
    caveat = " Matched by customer name (no job number on file) — double-check this one." if via_name else ""
    done = [j for j in installs if j["status"].lower() == "completed"]
    if done:
        j = sorted(done, key=lambda d: d["completedOn"])[0]
        return ("install_completed",
                f"PAY Stage 2 — installation job {j['job']} ({j['type'] or 'install'}) completed {j['completedOn']}.{caveat}",
                done[:5])
    if installs:
        j = installs[0]
        return ("install_in_progress",
                f"Don't pay yet — it sold, but installation job {j['job']} ({j['type'] or 'install'}) is "
                f"{j['status'] or 'not completed'}. Pay Stage 2 once it completes.{caveat}", installs[:5])
    estimates = [j for j in jobs if j["role"] == "estimate"]
    if estimates:
        j = estimates[0]
        return ("estimate_only",
                f"Don't pay Stage 2 yet — estimate job {j['job']} ({j['type'] or 'estimate'}, "
                f"{j['status'] or 'status unknown'}) is quoted but there's no installation job, so it hasn't sold. "
                f"Stage 1 is the payout at this point.{caveat}", estimates[:3])
    return ("no_install_found",
            f"Don't pay Stage 2 yet — no estimate or installation job in the lead's project.{caveat}", [])


def build_mpf_indexes(mpf_rows):
    """(emp, last_name_key) -> sold hits, and last_name_key -> [(emp, date, job)] for every
    employee — same 'TGL Lead Sold Res' activity the monthly pipeline keys on. Zero-pay rows are
    kept on purpose: the only question here is 'was a Stage 2 recorded', not 'how much'."""
    by_emp, by_customer = defaultdict(list), defaultdict(list)
    for row in mpf_rows:
        if row.get("Activity") != "TGL Lead Sold Res":
            continue
        name, customer = row.get("EmployeeName"), row.get("CustomerName") or ""
        lnk = pm.last_name_key(customer)
        if not name or not lnk:
            continue
        hit = {"emp": name, "job": row.get("JobNumber"), "date": (row.get("Date") or "")[:10]}
        by_emp[(name, lnk)].append(hit)
        by_customer[lnk].append(hit)
    return by_emp, by_customer


def departed_names():
    """Employees whose latest roster row is eligible but NOT active (marked departed in the app's
    Roster screen). Same last-row-wins read as process_month.load_roster()."""
    latest = {}
    for r in pm.sheet_get("roster"):
        if len(r) >= 6 and r[0] and str(r[3]).strip().upper() in ("TRUE", "FALSE"):
            latest[r[0]] = r
    return {n for n, r in latest.items()
            if str(r[3]).strip().upper() == "TRUE" and str(r[4]).strip().upper() == "FALSE"}


def evaluate(item, by_emp, by_customer, ledger_rows, client, types, units, departed=frozenset()):
    """Returns (verdict, summary, jobs). Never raises — a failed lookup becomes 'unknown'."""
    if item["emp"] in departed:
        return ("departed",
                f"{item['emp']} is no longer employed — don't pay. Mark it dead unless you decide otherwise.", [])
    customer = _customer_of(item)
    lnk = pm.last_name_key(customer)

    mine = by_emp.get((item["emp"], lnk)) if lnk else None
    if mine:
        h = mine[0]
        return "paid_on_mpf", f"Master Pay File shows Stage 2 for {item['emp']} — job {h['job']} on {h['date']}.", []

    if lnk:
        others = [h for h in by_customer.get(lnk, []) if h["emp"] != item["emp"]]
        if others:
            h = others[0]
            return ("credited_elsewhere",
                    f"Stage 2 for this customer went to {h['emp']} (job {h['job']}, {h['date']}) — likely dead.", [])
    key = pm.full_customer_key(customer)
    if key:
        for row in ledger_rows:
            if len(row) >= 5 and row[2] != item["emp"] and pm.full_customer_key(row[4]) == key \
                    and "Lead" in str(row[5]) and "Sold" in str(row[5]):
                return "credited_elsewhere", f"Payout ledger shows this customer's Stage 2 paid to {row[2]}.", []

    try:
        lead = find_lead_job(client, item)
        if lead is not None:
            pid = lead.get("projectId")
            if not pid:
                return "no_install_found", "The lead job isn't attached to a project, so no estimate or install is linked to it.", []
            jobs = project_jobs(client, pid, lead.get("jobNumber"), types, units)
            return verdict_from_jobs(item["emp"], jobs)
        if _job_number(item.get("ref")):
            return "unknown", f"Couldn't find job {_job_number(item['ref'])} in ServiceTitan.", []
        pids = find_via_customer(client, item)
        if not pids:
            return "unknown", "No job number on file and couldn't match this customer in ServiceTitan.", []
        jobs = []
        for pid in pids:
            jobs += project_jobs(client, pid, None, types, units)
        return verdict_from_jobs(item["emp"], jobs, via_name=True)
    except Exception as e:  # noqa: BLE001 — see docstring
        return "unknown", f"ServiceTitan lookup failed: {str(e)[:120]}", []


def run(month_label, client=None, mpf_rows=None, write=True):
    """Evaluates every pending carry-forward item in output_<month>.json and writes the results.
    `client`/`mpf_rows` are injectable for tests."""
    pm.configure_month(month_label)
    with open(pm._output_path_for(month_label)) as f:
        output = json.load(f)
    pending = [c for c in output["carryForward"] if not c.get("resolved")]
    if not pending:
        print("  [evidence] no pending carry-forward items.")
        return []

    if client is None:
        client = pm.get_client()
    if mpf_rows is None:
        import reconcile_carry_forward as rcf
        earliest = min((c["fromMonth"] for c in pending),
                       key=lambda m: (int(m.split()[1]), pm._MONTH_NAMES.index(m.split()[0])))
        mon, year = earliest.split()
        mpf_rows = rcf.fetch_mpf_range(f"{year}-{pm._MONTH_NAMES.index(mon) + 1:02d}-01", rcf.TODAY)
    by_emp, by_customer = build_mpf_indexes(mpf_rows)
    ledger_rows = pm.sheet_get("spiff_ledger")
    departed = departed_names()
    types, units, lookup_errors = load_lookups(client)
    for e in lookup_errors:
        print(f"  [evidence] lookup warning — {e}")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results = []
    for item in pending:
        verdict, summary, jobs = evaluate(item, by_emp, by_customer, ledger_rows, client, types, units, departed)
        results.append([month_label, item["id"], item["emp"], verdict, summary, json.dumps(jobs), now])
    counts = defaultdict(int)
    for r in results:
        counts[r[3]] += 1
    print(f"  [evidence] {len(results)} item(s): " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if write:
        pm.sheet_write_table("cf_evidence", HEADERS, results, mode="replaceMonth", month=month_label)
    return results


if __name__ == "__main__":
    import sys
    run(sys.argv[1])
