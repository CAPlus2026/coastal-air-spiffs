"""Evidence report for pending carry-forward items ("Lead Stage 2" payouts waiting on a sale).

Answers, per item, the question a manager otherwise has to look up by hand in ServiceTitan one at
a time: did this lead turn into a completed install?

This is EVIDENCE ONLY — it never resolves, pays or dismisses anything. The business owner's rule
(2026-09-04, see reconcile_carry_forward.py) is that a completed job at the same customer is NOT
proof this employee's lead was credited: ServiceTitan links an eventual sold estimate to exactly
one lead, so a different tech's lead can get the credit. So each item gets one verdict, strongest
first, and the manager still makes the call:

  paid_on_mpf         Master Pay File itself shows TGL Lead Sold Res for this employee + customer
                      (the one authoritative signal — safe to pay).
  credited_elsewhere  Master Pay File / the payout ledger shows the Stage 2 went to someone ELSE
                      for this customer — this employee's lead is dead.
  install_completed   ServiceTitan shows a completed install-type job at this customer after the
                      lead was created, but nobody has been credited with Stage 2 — most likely
                      candidates to pay, but verify the lead really was this employee's.
  no_install_found    No completed install-type job found since the lead — still genuinely open.
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
INSTALL_RE = re.compile(r"install|replac|change.?out|changeout", re.I)


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


def find_customer_id(client, item):
    """Prefer the lead's own job (the item's `ref`, 'Job 169257990') — an exact id, no name
    guessing. Falls back to a customer-name search for items with a blank ref."""
    num = _job_number(item.get("ref"))
    if num:
        jobs = _paged(client, "jpm/v2/tenant/{t}/jobs", {"number": num})
        if jobs:
            j = jobs[0]
            return j.get("customerId"), num, (j.get("createdOn") or j.get("completedOn") or "")[:10]
    name = _customer_of(item)
    if name:
        key = pm.full_customer_key(name)
        for c in _paged(client, "crm/v2/tenant/{t}/customers", {"name": name}):
            if pm.full_customer_key(c.get("name", "")) == key:
                return c.get("id"), None, ""
    return None, num, ""


def completed_installs(client, customer_id, since, lead_job_number, types, units):
    """Completed jobs at this customer on/after `since`, minus the lead's own job, classified as
    install-type when the job type or business unit name says so. If neither lookup table loaded
    there's nothing to classify with, so every completed job counts (marked type unknown)."""
    params = {"customerId": customer_id, "jobStatus": "Completed"}
    if since:
        params["completedOnOrAfter"] = since
    out = []
    for j in _paged(client, "jpm/v2/tenant/{t}/jobs", params):
        if lead_job_number and str(j.get("jobNumber")) == str(lead_job_number):
            continue
        tname = types.get(j.get("jobTypeId"), "")
        bname = units.get(j.get("businessUnitId"), "")
        if types or units:
            if not (INSTALL_RE.search(tname) or INSTALL_RE.search(bname)):
                continue
        out.append({"job": j.get("jobNumber"), "type": tname, "unit": bname,
                    "completedOn": (j.get("completedOn") or "")[:10]})
    out.sort(key=lambda d: d["completedOn"])
    return out


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


def evaluate(item, by_emp, by_customer, ledger_rows, client, types, units):
    """Returns (verdict, summary, jobs). Never raises — a failed lookup becomes 'unknown'."""
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
        customer_id, lead_num, since = find_customer_id(client, item)
        if not customer_id:
            return "unknown", "Couldn't find this customer in ServiceTitan to check.", []
        jobs = completed_installs(client, customer_id, since or _month_start(item.get("fromMonth", "")),
                                  lead_num, types, units)
    except Exception as e:  # noqa: BLE001 — see docstring
        return "unknown", f"ServiceTitan lookup failed: {str(e)[:120]}", []

    if jobs:
        j = jobs[0]
        more = f" (+{len(jobs) - 1} more)" if len(jobs) > 1 else ""
        label = j["type"] or j["unit"] or "job"
        return ("install_completed",
                f"Completed install found: job {j['job']} ({label}) on {j['completedOn']}{more}. "
                f"No Stage 2 credited to anyone yet — verify this lead was {item['emp']}'s.", jobs[:5])
    return "no_install_found", "No completed install found at this customer since the lead.", []


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
    types, units, lookup_errors = load_lookups(client)
    for e in lookup_errors:
        print(f"  [evidence] lookup warning — {e}")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results = []
    for item in pending:
        verdict, summary, jobs = evaluate(item, by_emp, by_customer, ledger_rows, client, types, units)
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
