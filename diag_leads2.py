"""READ-ONLY diagnostic #2 (2026-11-04): jobs/estimates at each lead's site since the lead date.
GET + read-only report POSTs only. Writes nothing. No secrets printed."""
import json
import process_month as pm
def P(tag, obj): print(tag, json.dumps(obj, default=str))
pm.configure_month("Oct 2026")
c = pm.get_client(); t = c.tenant_id
def g(path, **params):
    try: return c._request("GET", path, params=params)
    except Exception as e: return {"error": str(e)[:150], "data": []}
def allpages(path, **params):
    out, page = [], 1
    while page < 20:
        r = g(path, page=page, pageSize=200, **params)
        out += r.get("data", [])
        if not r.get("hasMore"): break
        page += 1
    return out
people = {p.get("id"): p.get("name") for p in pm.fetch_all_settings("technicians") + pm.fetch_all_settings("employees")}
nm = lambda i: people.get(i, i)
bus = {b["id"]: b.get("name") for b in pm.fetch_all_settings("business-units")}
jts = {}
def jt(i):
    if i not in jts: jts[i] = (g(f"/jpm/v2/tenant/{t}/job-types/{i}") or {}).get("name")
    return jts[i]

def job_detail(j):
    assigned = sorted({a.get("technicianName") for a in g(f"/dispatch/v2/tenant/{t}/appointment-assignments", jobId=j["id"]).get("data", [])} - {None})
    ests = []
    for e in g(f"/sales/v2/tenant/{t}/estimates", jobId=j["id"], pageSize=50).get("data", []):
        if str(e.get("jobId")) != str(j["id"]): continue
        ests.append({"id": e.get("id"), "name": e.get("name"), "status": (e.get("status") or {}).get("name"),
                     "created": (e.get("createdOn") or "")[:10], "soldOn": (e.get("soldOn") or "")[:10],
                     "soldBy": nm(e.get("soldBy")), "subtotal": e.get("subtotal")})
    return {"job": j.get("jobNumber"), "status": j.get("jobStatus"), "created": (j.get("createdOn") or "")[:10],
            "completed": (j.get("completedOn") or "")[:10], "bu": bus.get(j.get("businessUnitId")), "type": jt(j.get("jobTypeId")),
            "soldBy": nm(j.get("soldById")), "total": j.get("total"), "assigned": assigned, "locationId": j.get("locationId"),
            "projectId": j.get("projectId"), "estimates": ests}

plan = json.load(open("diag_leads_plan.json"))
for i, p in plan.items():
    jobs = []
    for loc in p["locs"]:
        jobs += allpages(f"/jpm/v2/tenant/{t}/jobs", locationId=loc, createdOnOrAfter=p["d0"] + "T00:00:00Z")
    P("SITEJOBS", {"i": int(i), "jobs": [job_detail(j) for j in jobs]})

# unresolved customers
for term in ["Kaul", "Petty", "Pure Property", "Vesta"]:
    custs = g(f"/crm/v2/tenant/{t}/customers", name=term, pageSize=20).get("data", [])
    locs = g(f"/crm/v2/tenant/{t}/locations", name=term, pageSize=20).get("data", [])
    if term == "Vesta":
        locs += g(f"/crm/v2/tenant/{t}/locations", street="230 Vesta", pageSize=20).get("data", [])
    res = {"term": term, "customers": [{"id": x["id"], "name": x.get("name"), "type": x.get("type")} for x in custs],
           "locations": [{"id": l["id"], "name": l.get("name"), "customerId": l.get("customerId"),
                          "street": (l.get("address") or {}).get("street")} for l in locs]}
    jl = []
    for l in locs[:4]:
        for j in allpages(f"/jpm/v2/tenant/{t}/jobs", locationId=l["id"], createdOnOrAfter="2026-07-01T00:00:00Z"):
            jl.append(job_detail(j))
    for x in custs[:3]:
        cu = g(f"/crm/v2/tenant/{t}/customers/{x['id']}")
        res.setdefault("types", []).append((x.get("name"), cu.get("type")))
        if not locs:
            for j in allpages(f"/jpm/v2/tenant/{t}/jobs", customerId=x["id"], createdOnOrAfter="2026-07-01T00:00:00Z"):
                jl.append(job_detail(j))
    res["jobs"] = jl
    P("SEARCH", res)

# Rich's sold list (Jun-Oct), commercial sales
for m in ["Jun 2026", "Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026"]:
    pm.configure_month(m)
    for r in pm.fetch_report("richCommissionReport"):
        r["_month"] = m; P("RICH", r)
print("DONE")
