"""READ-ONLY diagnostic (2026-11-04): October commercial-lead log vs ServiceTitan.
GET + read-only report POSTs only. Writes nothing anywhere. No secrets printed."""
import json, re, time
import process_month as pm

def P(tag, obj): print(tag, json.dumps(obj, default=str))

pm.configure_month("Oct 2026")
c = pm.get_client(); t = c.tenant_id
def g(path, **params):
    try:
        return c._request("GET", path, params=params)
    except Exception as e:
        return {"error": str(e)[:150], "data": []}

people = {}
for p in pm.fetch_all_settings("technicians") + pm.fetch_all_settings("employees"):
    people[p.get("id")] = p.get("name")
nm = lambda i: people.get(i, i)
bus = {b["id"]: b.get("name") for b in pm.fetch_all_settings("business-units")}
jts = {}
def jt(i):
    if i not in jts:
        jts[i] = (g(f"/jpm/v2/tenant/{t}/job-types/{i}") or {}).get("name")
    return jts[i]

out = json.load(open("output_2026-10.json"))
leads = out["commLeads"]

# Reports Jun-Oct: lead requests + MPF lead/comm lines
lr_rows, mpf_rows = [], []
for m in ["Jun 2026", "Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026"]:
    pm.configure_month(m)
    for r in pm.fetch_report("leadRequestReport"):
        r["_month"] = m; lr_rows.append(r)
    for r in pm.fetch_report("masterPayFile"):
        if "lead" in str(r.get("Activity", "")).lower() or "comm" in str(r.get("Activity", "")).lower():
            r["_month"] = m; mpf_rows.append(r)
pm.configure_month("Oct 2026")
if lr_rows: P("LRFIELDS", list(lr_rows[0].keys()))
for r in mpf_rows: P("MPFLEAD", r)

def norm(s): return re.sub(r"[^a-z0-9]", "", str(s or "").lower())

for L in leads:
    rec = {"lead": L}
    keys = {norm(L["customer"]), norm(L.get("location"))} - {""}
    lr = [r for r in lr_rows if norm(r.get("CustomerName")) in keys or norm(r.get("LocationName")) in keys]
    rec["leadReport"] = lr
    cust_ids = set()
    for r in lr:
        for k, v in r.items():
            if k.lower() in ("customerid",) and v: cust_ids.add(v)
    if not cust_ids:
        res = g(f"/crm/v2/tenant/{t}/customers", name=L["customer"], pageSize=20).get("data", [])
        exact = [x for x in res if norm(x.get("name")) == norm(L["customer"])]
        for x in (exact or res)[:3]: cust_ids.add(x["id"])
    custs = []
    for cid in list(cust_ids)[:3]:
        cu = g(f"/crm/v2/tenant/{t}/customers/{cid}")
        locs = g(f"/crm/v2/tenant/{t}/locations", customerId=cid, pageSize=50).get("data", [])
        jobs = g(f"/jpm/v2/tenant/{t}/jobs", customerId=cid, createdOnOrAfter="2026-04-01T00:00:00Z", pageSize=100).get("data", [])
        ests = g(f"/sales/v2/tenant/{t}/estimates", customerId=cid, createdOnOrAfter="2026-04-01T00:00:00Z", pageSize=100)
        jl = []
        for j in jobs:
            assigned = sorted({a.get("technicianName") for a in g(f"/dispatch/v2/tenant/{t}/appointment-assignments", jobId=j["id"]).get("data", [])} - {None})
            jl.append({"job": j.get("jobNumber"), "status": j.get("jobStatus"), "created": (j.get("createdOn") or "")[:10],
                       "completed": (j.get("completedOn") or "")[:10], "bu": bus.get(j.get("businessUnitId")), "type": jt(j.get("jobTypeId")),
                       "soldBy": nm(j.get("soldById")), "total": j.get("total"), "assigned": assigned, "locationId": j.get("locationId"),
                       "leadCallId": j.get("leadCallId"), "campaign": j.get("campaignId")})
        el = []
        for e in (ests.get("data", []) if isinstance(ests, dict) else []):
            el.append({"id": e.get("id"), "name": e.get("name"), "job": e.get("jobNumber"), "status": (e.get("status") or {}).get("name"),
                       "created": (e.get("createdOn") or "")[:10], "soldOn": (e.get("soldOn") or "")[:10], "soldBy": nm(e.get("soldBy")),
                       "subtotal": e.get("subtotal"), "bu": e.get("businessUnitName"), "locationId": e.get("locationId")})
        custs.append({"id": cid, "name": cu.get("name"), "type": cu.get("type"), "error": cu.get("error"),
                      "locations": [{"id": l.get("id"), "name": l.get("name"), "street": (l.get("address") or {}).get("street"),
                                     "city": (l.get("address") or {}).get("city")} for l in locs],
                      "jobs": jl, "estimates": el, "estErr": ests.get("error") if isinstance(ests, dict) else None})
    rec["customers"] = custs
    P("LEADJSON", rec)
print("DONE")
