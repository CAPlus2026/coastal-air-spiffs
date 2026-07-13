"""Monthly Coastal Air spiff processing pipeline.

Pulls the 6 SPIFF reports + employee roster from ServiceTitan, applies the
business rules from the spiff program, cross-references reports against the
Master Pay File to catch things payroll missed, and emits the JS object
literals to paste into index.html's `S` state.

Philosophy (per Billy, 2026-07-02): best-guess + exception-only flagging.
Auto-resolve anything we can compute confidently; only raise a flag for
genuinely broken/contradictory data (unrecognized accessory code, employee
we can't classify, username that doesn't resolve, etc).

Usage: python process_month.py
"""
import json
import re
import sys
from collections import defaultdict

from st_client import ServiceTitanClient

# ── Month config ─────────────────────────────────────────────────────
MONTH_LABEL = "Jun 2026"
PREV_LABEL = "May 2026"
NEXT_LABEL = "Jul 2026"
FROM_DATE = "2026-06-01"
TO_DATE = "2026-06-30"

# Same Apps Script Web App URL embedded in index.html — already public there, not a secret.
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbwHV_lR6gQQGs4LOmst5JxYlg6NqDjJIhJjs0h6l4wCs8DFEtBaWQ6RfURKIVdrfkqI/exec"

# ── Static rosters (fallback only — dynamic classification below is primary) ──
LEAD_ACTIVITIES = {"TGL Lead Set Res", "TGL Lead Sold Res"}
PLUMBERS = {"Jesse Mertens", "Herbie Windley"}
COMM_TECHS = {"Nick Scarpa", "Javi Vazquez", "Ray Lambert", "Kyle Freeman", "Stuart Akel"}
CH_TECHS = {"Jay Hall", "Steve Gordon"}
STEVEN_ROSTER = ["Darren Goida", "Jim LeBlanc", "Karl Welch", "Jesse Mertens", "Herbie Windley",
                 "Nick Scarpa", "Javi Vazquez", "Ray Lambert", "Kyle Freeman", "Stuart Akel"]
CALEB_ROSTER = ["Jay Hall", "Steve Gordon", "Quincy Fields"]
OFFICE_NAMES = {"Jenny Miller", "Katie Osterling", "Chris Port", "Danielle Gerthung"}

# People who show up in reports but aren't part of this spiff pool at all — confirmed by Billy 2026-07-03.
EXCLUDED_FROM_SPIFFS = {
    "Chase Shumate": "Salesperson — spiffs/commissions processed separately",
    "Caleb Harnish": "Location Manager (Charleston) — does not receive spiffs/commissions",
    "Tracy Dennis": "Office staff — membership attribution artifact, not an actual seller",
    "Rich Smith": "Commercial equipment salesperson — only service-work commission (3%, via Rich's "
                  "Commission Report) is processed here; equipment commissions handled separately",
    "Lauren Mancino": "Does not receive spiffs — not actually selling the renewals attributed to her",
    "Derrick Hall": "Commissions tracked via the separate CAP Lead Tracker app (Derrick's commercial lead "
                    "tool) — excluded from this pipeline entirely",
}

def normalize_code(code):
    return re.sub(r"\s+", "", str(code or "")).upper()


with open("spiff_rates.json") as f:
    _raw_rates = json.load(f)
PREFIX_RATES = {normalize_code(k): v for k, v in _raw_rates.pop("_prefixRates", {}).items()}
_raw_rates.pop("_meta", None)
SPIFF_RATES = {normalize_code(k): v for k, v in _raw_rates.items()}

with open("report_ids.json") as f:
    REPORT_IDS = json.load(f)

client = ServiceTitanClient()


# ── Fetch helpers ────────────────────────────────────────────────────
def fetch_report(key):
    meta = REPORT_IDS[key]
    params = [{"name": "From", "value": FROM_DATE}, {"name": "To", "value": TO_DATE}, *meta.get("extraParams", [])]
    fields, rows = client.get_report_data_all_pages(meta["category"], meta["reportId"], parameters=params)
    field_names = [f["name"] for f in fields]
    return [dict(zip(field_names, row)) for row in rows]


def fetch_all_settings(path):
    page, out = 1, []
    while True:
        data = client._request("GET", f"/settings/v2/tenant/{client.tenant_id}/{path}?page={page}&pageSize=200")
        out.extend(data.get("data", []))
        if not data.get("hasMore"):
            return out
        page += 1


def build_umap():
    people = fetch_all_settings("technicians") + fetch_all_settings("employees")
    umap = {}
    for p in people:
        name = p.get("name", "").strip()
        if not name:
            continue
        if p.get("loginName"):
            umap[p["loginName"].strip().lower()] = name
        if p.get("email"):
            umap[p["email"].strip().lower()] = name
    return umap


def resolve_completer(umap, completer):
    if not completer:
        return None, False
    key = completer.strip().lower()
    if key in umap:
        return umap[key], True
    return completer, False  # unresolved — caller decides whether to flag


# ── Name matching helpers ────────────────────────────────────────────
def last_name_key(customer):
    if not customer:
        return ""
    customer = customer.strip()
    first_segment = customer.split(",")[0].strip() if "," in customer else customer
    tokens = re.findall(r"[a-zA-Z]+", first_segment)
    return tokens[0].lower() if tokens else ""


# ── Classification ───────────────────────────────────────────────────
def classify_mpf_line(name, activity, business_unit_hint=""):
    is_lead = activity in LEAD_ACTIVITIES
    if name in PLUMBERS:
        return "plb"
    if name in COMM_TECHS:
        return "com"
    if name in CH_TECHS:
        return "chi" if is_lead else "chs"
    if name in STEVEN_ROSTER:
        return "ins" if is_lead else "svc"
    # Unknown employee — best-guess from business unit text, else flag upstream
    bu = business_unit_hint.lower()
    if "plumb" in bu:
        return "plb"
    if "ch -" in bu or "ch-" in bu:
        return "chi" if is_lead else "chs"
    if "commercial" in bu:
        return "com"
    return "ins" if is_lead else "svc"


def team_of(name):
    if name in CALEB_ROSTER:
        return "caleb"
    return "steven"  # default; unknown names still land somewhere and get flagged


def spiff_rate_for_code(code):
    code = normalize_code(code)
    if code in SPIFF_RATES:
        return SPIFF_RATES[code]
    for prefix, meta in PREFIX_RATES.items():
        if code.startswith(prefix):
            return meta
    return None


# ── Main pipeline ─────────────────────────────────────────────────────
def main():
    print("Fetching UMAP (technicians + employees)...")
    umap = build_umap()

    print("Fetching Master Pay File...")
    mpf = fetch_report("masterPayFile")
    print(f"  {len(mpf)} rows")

    print("Fetching Accessory Sales...")
    accessories = fetch_report("accessorySales")
    print(f"  {len(accessories)} rows")

    print("Fetching Membership Report...")
    memberships = fetch_report("membershipReport")
    print(f"  {len(memberships)} rows")

    print("Fetching Lead Request Report...")
    leads = fetch_report("leadRequestReport")
    print(f"  {len(leads)} rows")

    print("Fetching Technician Performance...")
    tech_perf = fetch_report("technicianPerformance")
    print(f"  {len(tech_perf)} rows")

    print("Fetching Rich's Commission Report...")
    rich = fetch_report("richCommissionReport")
    print(f"  {len(rich)} rows")

    business_unit_by_name = {r["Name"]: r.get("TechnicianBusinessUnit", "") for r in tech_perf if r.get("Name")}

    emps = {}  # name -> {svc,ins,plb,com,chs,chi,dups}
    spiff_detail = defaultdict(list)  # name -> [{date,job,customer,type,item,spiff}]
    flags = {"steven": [], "caleb": []}
    office_mems = defaultdict(float)
    flag_seq = [0]

    def new_flag_id(mgr):
        flag_seq[0] += 1
        return f"{mgr[0]}{flag_seq[0]}"

    def ensure_emp(name):
        if name not in emps:
            emps[name] = {"name": name, "svc": 0, "ins": 0, "plb": 0, "com": 0, "chs": 0, "chi": 0, "dups": []}
        return emps[name]

    def add_spiff(name, col, amount, date, job, customer, type_, item, auto_added=False):
        e = ensure_emp(name)
        e[col] += amount
        spiff_detail[name].append({
            "date": date, "job": job, "customer": customer, "type": type_, "item": item, "spiff": amount,
            **({"note": "Auto-added — not on Master Pay File"} if auto_added else {}),
        })

    def add_flag(mgr, emp, ref, title, detail, sev="yellow"):
        flags[mgr].append({
            "id": new_flag_id(mgr), "sev": sev, "emp": emp, "ref": ref, "resolved": False,
            "disp": "", "note": "", "title": title, "detail": detail,
        })

    # ── 1) Master Pay File — primary source of truth for what's already paid ──
    mpf_job_keys = set()  # (name, jobnumber) already paid, for accessory/lead cross-check
    mpf_customer_keys = defaultdict(set)  # name -> set of last-name keys already paid (any activity)
    tgl_set_keys = defaultdict(set)  # name -> set of last-name keys with Stage 1 paid
    tgl_sold_seen = defaultdict(list)  # (name, last-name key) -> [job,...] for dup detection
    stage1_paid_detail = {}  # (name, last-name key) -> {job, customer, dept} for carry-forward generation

    for row in mpf:
        name = row.get("EmployeeName")
        activity = row.get("Activity")
        pay = row.get("GrossPay") or 0
        job = row.get("JobNumber")
        customer = row.get("CustomerName") or ""
        date = (row.get("Date") or "")[:10]
        if not name or not activity or not pay or name in EXCLUDED_FROM_SPIFFS:
            continue

        bu_hint = business_unit_by_name.get(name, "")
        col = classify_mpf_line(name, activity, bu_hint)
        add_spiff(name, col, float(pay), date, job, customer, activity,
                  f"{activity} — {customer}")
        mpf_job_keys.add((name, str(job)))
        lnk = last_name_key(customer)
        mpf_customer_keys[name].add(lnk)

        if activity == "TGL Lead Set Res":
            tgl_set_keys[name].add(lnk)
            stage1_paid_detail[(name, lnk)] = {"job": job, "customer": customer,
                                                "dept": "CH Install" if name in CH_TECHS else "MB Install Residential"}
        if activity == "TGL Lead Sold Res":
            tgl_sold_seen[(name, lnk)].append(job)

        if name not in STEVEN_ROSTER and name not in CALEB_ROSTER and name not in PLUMBERS \
                and name not in COMM_TECHS and name not in CH_TECHS:
            mgr = team_of(name)
            add_flag(mgr, name, f"Job #{job}",
                      f"Unrecognized employee on Master Pay File — {name}",
                      f"{name} appears on the Master Pay File ({activity}, {customer}) but isn't in the known "
                      f"roster. Best-guess classified via business unit '{bu_hint}'. Verify this is a current "
                      f"employee and the department is correct.", sev="red")

    # Duplicate TGL Sold detection
    for (name, lnk), jobs in tgl_sold_seen.items():
        if len(jobs) > 1:
            mgr = team_of(name)
            add_flag(mgr, name, ", ".join(f"#{j}" for j in jobs),
                      f"Duplicate TGL Stage 2 — {name}",
                      f"TGL Lead Sold Res appears more than once for the same customer. Jobs: "
                      f"{', '.join('#' + str(j) for j in jobs)}. One is likely a duplicate payroll entry — verify "
                      f"and reverse the incorrect one.", sev="red")

    # ── 2) Accessory Sales cross-check — catch spiffs missing from MPF ──
    for row in accessories:
        tech = row.get("Technician")
        code = row.get("AccessorySold")
        qty = row.get("Quantity") or 1
        job = row.get("JobNumber")
        customer = row.get("CustomerName") or ""
        date = (row.get("Date") or "")[:10]
        if not tech or not code or tech in EXCLUDED_FROM_SPIFFS:
            continue
        if (tech, str(job)) in mpf_job_keys:
            continue  # already paid via MPF, nothing to do

        rate = spiff_rate_for_code(code)
        mgr = team_of(tech)
        if rate is None:
            add_flag(mgr, tech, f"Job #{job}",
                      f"Unrecognized accessory code — {code}",
                      f"{tech} sold {code} x{qty} to {customer} (Job #{job}), not on Master Pay File, and "
                      f"'{code}' isn't in the spiff rate table. Confirm the spiff amount and add manually.",
                      sev="red")
            continue
        amount = rate["spiff"] * qty
        col = "chs" if tech in CH_TECHS else "com" if tech in COMM_TECHS else "plb" if tech in PLUMBERS else "svc"
        add_spiff(tech, col, amount, date, job, customer, "Sales Spiff", f"{rate['desc']} ({code})", auto_added=True)

    # ── 3) Membership Report cross-check ──
    for row in memberships:
        sold_by = (row.get("SoldBy") or "").strip()
        bonus = row.get("MembershipBonus") or 0
        customer = (row.get("CustomerName") or "").strip()
        sold_on = (row.get("SoldOn") or "")[:10]
        cust_id = row.get("CustomerMembershipId")
        if not sold_by or not bonus or sold_by in EXCLUDED_FROM_SPIFFS:
            continue
        lnk = last_name_key(customer)
        if lnk in mpf_customer_keys.get(sold_by, set()):
            continue  # already paid via MPF

        if sold_by in OFFICE_NAMES:
            office_mems[sold_by] += bonus
            continue

        mgr = team_of(sold_by)
        if sold_by not in STEVEN_ROSTER and sold_by not in CALEB_ROSTER and sold_by not in PLUMBERS \
                and sold_by not in COMM_TECHS and sold_by not in CH_TECHS:
            add_flag(mgr, sold_by, f"Membership #{cust_id}",
                      f"Unrecognized membership seller — {sold_by}",
                      f"Membership sold by '{sold_by}' (not on Master Pay File, {row.get('MembershipType')}, "
                      f"{customer}). Not a recognized tech, office staff, or manager. Verify and add manually.",
                      sev="red")
            continue
        col = "chs" if sold_by in CH_TECHS else "svc"
        add_spiff(sold_by, col, bonus, sold_on, "", customer, "Sales Spiff",
                  f"{row.get('MembershipType')} — {row.get('ActivationMethod')}", auto_added=True)

    # ── 4) Lead Request Report cross-check — Stage 1 gaps ──
    unresolved_completers = set()
    for row in leads:
        if row.get("State") != "Completed":
            continue
        completer_raw = row.get("Completer")
        name, resolved = resolve_completer(umap, completer_raw)
        customer = (row.get("CustomerName") or row.get("LocationName") or "").strip()
        lnk = last_name_key(customer)

        if not resolved:
            unresolved_completers.add(completer_raw)
            continue
        if name not in STEVEN_ROSTER and name not in CALEB_ROSTER:
            continue  # not a spiff-eligible role (office/CSR/etc submitting on someone's behalf)
        if lnk in tgl_set_keys.get(name, set()):
            continue  # Stage 1 already paid via MPF

        mgr = team_of(name)
        date = (row.get("LastModifiedDate") or "")[:10]
        is_comm = name in COMM_TECHS
        # Residential Stage 1 = $25 best-guess add; commercial leads go into the rolling log, not a flat add
        if is_comm:
            continue  # handled by commercial lead log, not a per-line spiff
        col = "chi" if name in CH_TECHS else "ins"
        add_spiff(name, col, 25.0, date, "", customer, "TGL Lead Set Res",
                  "Lead Stage 1 — quote delivered (auto-added, not on pay file)", auto_added=True)
        stage1_paid_detail[(name, lnk)] = {"job": "", "customer": customer,
                                            "dept": "CH Install" if name in CH_TECHS else "MB Install Residential"}

    if unresolved_completers:
        for c in unresolved_completers:
            add_flag("steven", c, "Lead Request Report",
                      f"Unresolved lead requester — {c}",
                      f"'{c}' appears as a lead-request completer but doesn't match any known ServiceTitan "
                      f"username or email. Could be a new hire or a typo'd login. Verify who this is before "
                      f"crediting a Stage 1 spiff.", sev="red")

    # ── 5) Residential Stage 2 carry-forward ──
    # Known pending Stage 2 ($75) items from May 2026 (from index.html S.carryForward).
    # NOTE: before running this script for a new month, refresh this list from the CURRENT
    # index.html's S.carryForward (including any items already resolved via the app) — this
    # hardcoded seed only reflects state as of the June 2026 build.
    prior_carry_forward = [
        {"id": "cf1", "fromMonth": "May 2026", "emp": "Darren Goida", "ref": "Job 157782511",
         "type": "Lead Stage 2 — Mains, Charlie", "amount": 75, "dept": "MB Install Residential", "lnk": "mains"},
        {"id": "cf2", "fromMonth": "May 2026", "emp": "Jim LeBlanc", "ref": "Job 157876582",
         "type": "Lead Stage 2 — Parsons, Karen", "amount": 75, "dept": "MB Install Residential", "lnk": "parsons"},
        {"id": "cf3", "fromMonth": "May 2026", "emp": "Jim LeBlanc", "ref": "Job 158111360",
         "type": "Lead Stage 2 — Sea-Mix LLC", "amount": 75, "dept": "MB Install Residential", "lnk": "sea"},
        {"id": "cf4", "fromMonth": "May 2026", "emp": "Karl Welch", "ref": "Job 157799674",
         "type": "Lead Stage 2 — Cooper, Ray", "amount": 75, "dept": "MB Install Residential", "lnk": "cooper"},
        {"id": "cf5", "fromMonth": "May 2026", "emp": "Karl Welch", "ref": "Job 156979623",
         "type": "Lead Stage 2 — Ogletree, Bill", "amount": 75, "dept": "MB Install Residential", "lnk": "ogletree"},
        {"id": "cf6", "fromMonth": "May 2026", "emp": "Karl Welch", "ref": "Job 157783679",
         "type": "Lead Stage 2 — Bernard, Jonathan", "amount": 75, "dept": "MB Install Residential", "lnk": "bernard"},
    ]
    # Manual resolutions from the app (Steven/Caleb marking a carry-forward item "paid" or "dead" directly) —
    # respect these so a dead lead doesn't keep rolling forward forever, and a manually-paid one doesn't reappear.
    def fetch_carry_forward_resolutions():
        import requests as _requests
        try:
            resp = _requests.get(APPS_SCRIPT_URL, params={"action": "get", "sheet": "carry_forward_resolutions"}, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("values") or []
        except Exception as e:
            print(f"  (couldn't fetch carry_forward_resolutions — treating as none: {e})")
            return {}
        resolved = {}
        for row in data:
            if len(row) < 8:
                continue
            _month, _mgr, cf_id, emp, ref, type_, amount, disposition = row[:8]
            if disposition in ("paid", "dead"):
                resolved[cf_id] = disposition
            elif cf_id in resolved:
                del resolved[cf_id]  # undone
        return resolved

    manually_resolved = fetch_carry_forward_resolutions()

    carry_forward_out = []
    resolved_keys = set()
    cf_seq = 0
    for cf in prior_carry_forward:
        key = (cf["emp"], cf["lnk"])
        if key in tgl_sold_seen:
            # Stage 2 was paid this month via MPF (already counted in the main MPF loop) — drop from the list.
            resolved_keys.add(key)
            continue
        if cf.get("id") and manually_resolved.get(cf["id"]) in ("paid", "dead"):
            continue  # manually resolved in the app — don't carry forward again
        cf_seq += 1
        carry_forward_out.append({
            "id": f"cf_{FROM_DATE[:7]}_{cf_seq}", "fromMonth": cf["fromMonth"], "emp": cf["emp"], "ref": cf["ref"],
            "type": cf["type"], "amount": cf["amount"], "dept": cf["dept"],
            "reason": "Stage 1 paid, still pending sold/installed/paid confirmation — carried forward again.",
            "resolved": False, "disposition": "", "note": "",
        })

    # New Stage 1s paid this month (real MPF or auto-added) without a matching Stage 2 in the same month
    # become next month's carry-forward.
    for (name, lnk), detail in stage1_paid_detail.items():
        key = (name, lnk)
        if key in tgl_sold_seen or key in resolved_keys:
            continue
        if any(cf["emp"] == name and cf["lnk"] == lnk for cf in prior_carry_forward):
            continue  # already represented above
        cf_seq += 1
        carry_forward_out.append({
            "id": f"cf_{FROM_DATE[:7]}_{cf_seq}", "fromMonth": MONTH_LABEL, "emp": name,
            "ref": f"Job {detail['job']}" if detail["job"] else "",
            "type": f"Lead Stage 2 — {detail['customer']}", "amount": 75, "dept": detail["dept"],
            "reason": f"Stage 1 paid {MONTH_LABEL}. Pay $75 when sold, installed, paid.",
            "resolved": False, "disposition": "", "note": "",
        })

    # ── 6) Commercial lead rolling log ──
    # Carry-forward state from May 2026 (from index.html S.commLeads, status='Sold — Install In Progress')
    carried_leads = [
        {"id": "cl1", "month": "May 2026", "tech": "Kyle Freeman", "job": "156731670",
         "customer": "AMC Theaters NMB", "status": "Sold — Install In Progress", "spiff": 100,
         "payMonth": "Jun 2026", "paid": False},
        {"id": "cl3", "month": "May 2026", "tech": "Javi Vazquez", "job": "157867483",
         "customer": "Angel Oak Nursing & Rehab", "status": "Sold — Install In Progress", "spiff": 100,
         "payMonth": "Jun 2026", "paid": False},
    ]
    comm_leads_out = []
    for lead in carried_leads:
        if (lead["tech"], lead["job"]) in mpf_job_keys:
            # Already paid via this month's Master Pay File (counted in the main MPF loop above) —
            # just mark the log entry resolved, don't add the spiff a second time.
            lead["status"] = "Sold & Completed"
            lead["paid"] = True
            lead["payMonth"] = MONTH_LABEL
        else:
            # Still not paid — roll forward again. No flag generated here: the Commercial Leads Log
            # (S.commLeads, rCommLeads() in index.html) is the single place managers track and disposition
            # these via its own status dropdown — a duplicate flag entry would just be noise.
            lead["payMonth"] = NEXT_LABEL
        comm_leads_out.append(lead)

    # New commercial leads this month — from Lead Request Report, comm techs only, not already in the log.
    # Note: this report has no real ServiceTitan job number field (AssignedToId is the assigned user, not
    # a job) — matches the brief's own note that lead-request numbers don't match MPF job numbers anyway.
    # Job number is left blank for manual entry when a manager confirms sold/completed status in the app.
    known_leads = {(l["tech"], last_name_key(l["customer"])) for l in comm_leads_out}
    seq = 0
    for row in leads:
        if row.get("State") != "Completed":
            continue
        name, resolved = resolve_completer(umap, row.get("Completer"))
        if not resolved or name not in COMM_TECHS:
            continue
        customer = (row.get("CustomerName") or row.get("LocationName") or "").strip()
        lnk = last_name_key(customer)
        key = (name, lnk)
        if key in known_leads or lnk in mpf_customer_keys.get(name, set()):
            continue  # already logged, or already paid via MPF this month
        known_leads.add(key)
        seq += 1
        comm_leads_out.append({
            "id": f"cl_{FROM_DATE[:7]}_{seq}", "month": MONTH_LABEL, "tech": name, "job": "",
            "customer": customer, "status": "Pending", "spiff": 0, "payMonth": "", "paid": False,
        })

    # ── 7) Rich Smith commission (3%) ──
    rich_rows = [r for r in rich if r.get("SoldBy") == "Rich Smith"]
    rich_total_base = sum((r.get("EstimateSalesInstalled") or 0) for r in rich_rows)
    rich_commission = round(rich_total_base * 0.03, 2)
    rich_details = [
        {
            "job": r.get("InstallJobs") or "", "customer": r.get("LocationName") or "",
            "item": r.get("EstimateName") or "", "soldOn": (r.get("SoldOn") or "")[:10],
            "sale": r.get("EstimateSalesInstalled") or 0,
            "commission": round((r.get("EstimateSalesInstalled") or 0) * 0.03, 2),
        }
        for r in rich_rows
    ]

    # ── Output ──────────────────────────────────────────────────────
    steven_emps = [emps[n] for n in STEVEN_ROSTER if n in emps]
    caleb_emps = [emps[n] for n in CALEB_ROSTER if n in emps]
    other_emps = [e for n, e in emps.items() if n not in STEVEN_ROSTER and n not in CALEB_ROSTER]

    result = {
        "month": MONTH_LABEL,
        "emps": {"steven": steven_emps, "caleb": caleb_emps, "_unclassified": other_emps},
        "flags": flags,
        "spiffDetail": {
            "steven": {n: spiff_detail[n] for n in STEVEN_ROSTER if n in spiff_detail},
            "caleb": {n: spiff_detail[n] for n in CALEB_ROSTER if n in spiff_detail},
        },
        "officeMems": [{"name": n, "total": round(t, 2)} for n, t in office_mems.items()],
        "commLeads": comm_leads_out,
        "carryForward": carry_forward_out,
        "bonuses": [
            {"id": "rich", "name": "Rich Smith", "type": "Commercial Commission (3%)",
             "amount": rich_commission, "dept": "MB Service Commercial", "approved": False,
             "note": f"3% of {rich_total_base:.2f} total EstimateSalesInstalled (Sold On basis) — "
                     f"recomputed fresh each month from Rich's Commission Report, does not carry forward",
             "details": rich_details},
        ],
        "unresolvedCompleters": list(unresolved_completers),
    }

    with open(f"output_{FROM_DATE[:7]}.json", "w") as f:
        json.dump(result, f, indent=2)

    print("\n=== SUMMARY ===")
    print(f"Steven's team: {len(steven_emps)} employees with spiffs")
    print(f"Caleb's team: {len(caleb_emps)} employees with spiffs")
    if other_emps:
        print(f"UNCLASSIFIED (flagged): {[e['name'] for e in other_emps]}")
    print(f"Flags — Steven: {len(flags['steven'])}, Caleb: {len(flags['caleb'])}")
    print(f"Rich Smith commission: ${rich_commission} (base ${rich_total_base:.2f})")
    print(f"Commercial leads log: {len(comm_leads_out)} entries "
          f"({sum(1 for l in comm_leads_out if l['paid'])} paid this month, "
          f"{sum(1 for l in comm_leads_out if l['status'] == 'Pending')} new/pending)")
    if unresolved_completers:
        print(f"Unresolved lead-request usernames: {unresolved_completers}")
    print(f"\nFull output written to output_{FROM_DATE[:7]}.json")


if __name__ == "__main__":
    main()
