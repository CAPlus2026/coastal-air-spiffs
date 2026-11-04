"""READ-ONLY diagnostic (2026-11-04): Jay Hall's 3 equipment jobs. GET + read-only report POSTs only.
Writes nothing anywhere. Prints job/invoice facts and names/amounts only (no secrets)."""
import json
import process_month as pm

JOBS = ["173189570", "173303046", "173437542"]
pm.configure_month("Oct 2026")
c = pm.get_client(); t = c.tenant_id
people = {}
for p in pm.fetch_all_settings("technicians") + pm.fetch_all_settings("employees"):
    people[p.get("id")] = p.get("name")
nm = lambda i: people.get(i, i)

def g(path, **params):
    return c._request("GET", path, params=params)

for jn in JOBS:
    print(f"\n################ JOB {jn}")
    jobs = g(f"/jpm/v2/tenant/{t}/jobs", number=jn).get("data", [])
    if not jobs:
        jobs = [x for x in g(f"/jpm/v2/tenant/{t}/jobs", ids=jn).get("data", [])]
    for j in jobs:
        print("JOB", json.dumps({k: j.get(k) for k in ("id", "jobNumber", "jobStatus", "completedOn", "businessUnitId",
              "jobTypeId", "soldById", "total", "createdOn", "invoiceId")}), "soldBy=", nm(j.get("soldById")))
        try:
            bu = g(f"/settings/v2/tenant/{t}/business-units/{j.get('businessUnitId')}")
            print("  BU", bu.get("name"))
        except Exception as e:
            print("  BU err", str(e)[:80])
        try:
            jt = g(f"/jpm/v2/tenant/{t}/job-types/{j.get('jobTypeId')}")
            print("  JOBTYPE", jt.get("name"))
        except Exception as e:
            print("  JOBTYPE err", str(e)[:80])
        try:
            for a in g(f"/jpm/v2/tenant/{t}/appointments", jobId=j["id"]).get("data", []):
                pass
            for a in g(f"/dispatch/v2/tenant/{t}/appointment-assignments", jobId=j["id"]).get("data", []):
                print("  ASSIGNED", a.get("technicianName"), a.get("assignedOn", "")[:10])
        except Exception as e:
            print("  assign err", str(e)[:80])
        invs = g(f"/accounting/v2/tenant/{t}/invoices", jobId=j["id"]).get("data", [])
        for inv in invs:
            print("INVOICE", json.dumps({k: inv.get(k) for k in ("id", "referenceNumber", "invoiceDate", "subTotal",
                  "salesTax", "total", "balance", "invoiceType", "paidOn", "syncStatus")}))
            for it in inv.get("items", []):
                code = (it.get("skuName") or "")
                rate = pm.spiff_rate_for_code(code) if code else None
                print("  ITEM", json.dumps({"sku": code, "type": it.get("type"), "desc": (it.get("description") or "")[:70],
                      "qty": it.get("quantity"), "unitPrice": it.get("price"), "total": it.get("total"),
                      "soldHours": it.get("soldHours"), "accessoryRate": rate and rate.get("spiff"),
                      "rateDesc": rate and rate.get("desc"), "rateCat": rate and rate.get("category")}))
        try:
            for e in g(f"/sales/v2/tenant/{t}/estimates", jobId=j["id"]).get("data", []):
                print("ESTIMATE", e.get("id"), e.get("name"), "status=", (e.get("status") or {}).get("name"),
                      "soldBy=", nm(e.get("soldBy")), "soldOn=", (e.get("soldOn") or "")[:10], "subtotal=", e.get("subtotal"))
        except Exception as ex:
            print("  estimates err", str(ex)[:80])

print("\n################ REPORT ROWS for these jobs")
for key in ("accessorySales", "masterPayFile"):
    rows = pm.fetch_report(key)
    for r in rows:
        if str(r.get("JobNumber") or r.get("Job") or r.get("JobNumber ") or "") in JOBS or any(str(v) in JOBS for v in r.values()):
            print(key, json.dumps(r, default=str)[:400])

print("\n################ RECOMPUTE: Jay lines on these jobs")
res = pm.compute("Oct 2026")["result"]
for mgr, emps in res["spiffDetail"].items():
    for name, ls in emps.items():
        for l in ls:
            if str(l.get("job")) in JOBS:
                print("LINE", mgr, name, json.dumps(l))
for f in res["flags"]:
    s = json.dumps(f)
    if any(j in s for j in JOBS):
        print("FLAG", s[:300])
