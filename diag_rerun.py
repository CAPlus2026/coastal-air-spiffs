"""READ-ONLY diagnostic (2026-11-04): recompute Oct 2026 and diff against the published output.
Writes nothing to Sheets, ServiceTitan or the repo. Prints names/amounts only (no secrets)."""
import json, collections
import process_month as pm
import audit_orphans

M = "Oct 2026"
old = json.load(open("output_2026-10.json"))
computed = pm.compute(M)
new = computed["result"]
cols = ["svc", "ins", "plb", "com", "chs", "chi"]

def people(r):
    t = collections.defaultdict(float)
    for mgr in ("steven", "caleb"):
        for e in r["emps"][mgr]:
            t[(mgr, e["name"])] += sum(e[c] for c in cols)
    for m in r["officeMems"]:
        t[("office-mems", m["name"])] += m["total"]
    for b in r["bonuses"]:
        t[("bonus", b["name"])] += b["amount"]
    return t

o, n = people(old), people(new)
print(f"OLD total {pm._grand_total(old):.2f}  NEW total {pm._grand_total(new):.2f}")
print("== per-person changes ==")
for k in sorted(set(o) | set(n)):
    if abs(o.get(k, 0) - n.get(k, 0)) > 0.004:
        print(f"  {k[0]:12} {k[1]:30} {o.get(k,0):9.2f} -> {n.get(k,0):9.2f}  ({n.get(k,0)-o.get(k,0):+.2f})")

def lines(r):
    c = collections.Counter()
    for mgr, emps in r["spiffDetail"].items():
        for name, ls in emps.items():
            for l in ls:
                c[(mgr, name, str(l.get("job")), l.get("type"), l.get("item"), float(l.get("spiff") or 0),
                   l.get("note", "MPF") and ("auto" if l.get("note") else "MPF"))] += 1
    for m in r["officeMems"]:
        for d in m["details"]:
            c[("office-mems", m["name"], "", d["type"], d["customer"], float(d["amount"]), d["activation"])] += 1
    return c

lo, ln = lines(old), lines(new)
print("== lines ADDED ==")
for k, v in (ln - lo).items(): print("  +", v, k)
print("== lines REMOVED ==")
for k, v in (lo - ln).items(): print("  -", v, k)

pf = pm.preflight(computed)
print("== preflight ==", json.dumps(pf)[:600])
orph = audit_orphans.run_audit(M, new)
print(f"== orphan audit vs fresh compute: {len(orph)} ==")
for x in orph: print("  ", x.get("tab"), x.get("verdict"), x.get("money_moving"), x.get("emp") or x.get("tech"))
