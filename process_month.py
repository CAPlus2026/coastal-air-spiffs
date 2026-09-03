"""Monthly Coastal Air spiff processing pipeline.

Pulls the 6 SPIFF reports + employee roster from ServiceTitan, applies the
business rules from the spiff program, cross-references reports against the
Master Pay File to catch things payroll missed, and emits the JS object
literals to paste into index.html's `S` state.

Philosophy (per Billy, 2026-07-02): best-guess + exception-only flagging.
Auto-resolve anything we can compute confidently; only raise a flag for
genuinely broken/contradictory data (unrecognized accessory code, employee
we can't classify, username that doesn't resolve, etc).

Usage: python process_month.py "Sep 2026"

The month is a required argument (no default) — this is deliberate. A
hardcoded default was the single most common source of error in this
pipeline's history (easy to forget to update, easy to silently re-run last
month). The runner (run_requested_month.py) always passes it explicitly too.
"""
import calendar
import hashlib
import json
import os
import re
import sys
from collections import defaultdict

import requests

from st_client import ServiceTitanClient

_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def stable_id(prefix, *parts):
    """Content-derived id: same inputs always produce the same id, regardless of iteration
    order or which run produced them. Replaces sequence-counter ids (cf_2026-08_3, s1, cl_..._2)
    that got reminted from scratch on every run — including every normal month-to-month carry,
    not just an explicit re-run — which could silently orphan a manager's already-recorded
    resolution/correction (found 2026-09 after a real self-service re-run). Truncated sha1 is
    fine here: this only needs to avoid accidental collisions, not resist a deliberate one."""
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:10]}"


def make_id_factory(prefix):
    """Returns an id-maker that appends a numeric suffix on the rare occasion two genuinely
    different items hash to the same content-key within one run (e.g. two blank-ref carry-forward
    items for the same employee) — deterministic as long as encounter order is deterministic,
    which it is here (both are derived from the same ServiceTitan report order every run)."""
    seen = {}
    def make(*parts):
        base = stable_id(prefix, *parts)
        n = seen.get(base, 0) + 1
        seen[base] = n
        return base if n == 1 else f"{base}_{n}"
    return make


def month_config(label):
    """Given 'Sep 2026', return (from_date, to_date, prev_label, next_label)."""
    mon_str, year_str = label.split()
    idx = _MONTH_NAMES.index(mon_str)
    year = int(year_str)
    last_day = calendar.monthrange(year, idx + 1)[1]
    from_date = f"{year}-{idx + 1:02d}-01"
    to_date = f"{year}-{idx + 1:02d}-{last_day:02d}"
    prev_idx, prev_year = (idx - 1, year) if idx > 0 else (11, year - 1)
    next_idx, next_year = (idx + 1, year) if idx < 11 else (0, year + 1)
    prev_label = f"{_MONTH_NAMES[prev_idx]} {prev_year}"
    next_label = f"{_MONTH_NAMES[next_idx]} {next_year}"
    return from_date, to_date, prev_label, next_label


# ── Month config ─────────────────────────────────────────────────────
# None until configure_month() runs — deliberately not read until main() is actually called, so
# other scripts (run_requested_month.py) can `import process_month` for its helper functions
# (sheet_get, month_config, norm_month, ...) without being forced to supply a month up front.
MONTH_LABEL = PREV_LABEL = NEXT_LABEL = FROM_DATE = TO_DATE = None


def configure_month(month_label):
    global MONTH_LABEL, PREV_LABEL, NEXT_LABEL, FROM_DATE, TO_DATE
    MONTH_LABEL = month_label
    FROM_DATE, TO_DATE, PREV_LABEL, NEXT_LABEL = month_config(month_label)

# Same Apps Script Web App URL embedded in index.html — already public there, not a secret.
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbwHV_lR6gQQGs4LOmst5JxYlg6NqDjJIhJjs0h6l4wCs8DFEtBaWQ6RfURKIVdrfkqI/exec"
# Not yet enforced by the backend (see apps-script/Lib.js checkSharedKey_) — sending it now
# just gets this caller ready ahead of the eventual flip.
SHARED_KEY = os.environ.get("SPIFFS_SHARED_KEY", "")

# ── Roster ────────────────────────────────────────────────────────────
# LEAD_ACTIVITIES isn't roster data (it's ServiceTitan activity-type strings, same for everyone)
# so it stays a plain constant. Everyone/everything else below used to be hardcoded here — now
# loaded from the `roster` Sheet tab via load_roster_globals(), called from the __main__ block
# right after configure_month(). None until then, same reasoning as the month config above.
LEAD_ACTIVITIES = {"TGL Lead Set Res", "TGL Lead Sold Res"}
STEVEN_ROSTER = CALEB_ROSTER = PLUMBERS = COMM_TECHS = CH_TECHS = OFFICE_NAMES = EXCLUDED_FROM_SPIFFS = None


def load_roster_globals():
    global STEVEN_ROSTER, CALEB_ROSTER, PLUMBERS, COMM_TECHS, CH_TECHS, OFFICE_NAMES, EXCLUDED_FROM_SPIFFS
    STEVEN_ROSTER, CALEB_ROSTER, PLUMBERS, COMM_TECHS, CH_TECHS, OFFICE_NAMES, EXCLUDED_FROM_SPIFFS = load_roster()

def normalize_code(code):
    return re.sub(r"\s+", "", str(code or "")).upper()


with open("spiff_rates.json") as f:
    _raw_rates = json.load(f)
PREFIX_RATES = {normalize_code(k): v for k, v in _raw_rates.pop("_prefixRates", {}).items()}
_raw_rates.pop("_meta", None)
SPIFF_RATES = {normalize_code(k): v for k, v in _raw_rates.items()}

with open("report_ids.json") as f:
    REPORT_IDS = json.load(f)

_client = None


def get_client():
    """Lazy — instantiating ServiceTitanClient() reads ST_CLIENT_ID etc from the environment
    and raises immediately if they're missing. Deferring this until a report is actually fetched
    means `import process_month` (e.g. run_requested_month.py, just to reuse its Sheet helpers)
    doesn't require ServiceTitan credentials to be present at all when there's nothing to do."""
    global _client
    if _client is None:
        _client = ServiceTitanClient()
    return _client


# ── Fetch helpers ────────────────────────────────────────────────────
def fetch_report(key):
    meta = REPORT_IDS[key]
    params = [{"name": "From", "value": FROM_DATE}, {"name": "To", "value": TO_DATE}, *meta.get("extraParams", [])]
    fields, rows = get_client().get_report_data_all_pages(meta["category"], meta["reportId"], parameters=params)
    field_names = [f["name"] for f in fields]
    return [dict(zip(field_names, row)) for row in rows]


def fetch_all_settings(path):
    page, out = 1, []
    client = get_client()
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


# ── Sheet read/write helpers (for self-service: auto-seeding + result delivery) ──
_ISO_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})-\d{2}T")


def norm_month(v):
    """Google Sheets silently auto-converts a plain 'MMM YYYY' cell value into a real Date on
    write (confirmed live, 2026-09: every existing tab's month column comes back from the Apps
    Script backend as an ISO datetime like '2026-08-01T04:00:00.000Z' instead of 'Aug 2026').
    Every month-string comparison against Sheet data must go through this first, or it silently
    never matches — which is exactly why compute_prior_carry_forward()/compute_carried_leads()
    returned zero rows against real bootstrapped data before this fix."""
    if not v:
        return ""
    m = _ISO_MONTH_RE.match(str(v))
    if m:
        year, mon = m.group(1), int(m.group(2))
        return f"{_MONTH_NAMES[mon - 1]} {year}"
    return str(v)


def sheet_get(sheet_name, timeout=20):
    """GET a full Sheet tab via the Apps Script backend. Returns [] on any failure — every
    caller here is a best-effort seed, not something that should ever crash a run."""
    try:
        resp = requests.get(APPS_SCRIPT_URL, params={"action": "get", "sheet": sheet_name, "key": SHARED_KEY},
                             timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("values") or []
    except Exception as e:
        print(f"  (couldn't fetch {sheet_name}: {e})")
        return []


def sheet_write_table(sheet_name, headers, rows, mode="replaceMonth", month=None):
    """Bulk-write a result table via the Apps Script backend's writeTable verb (added Phase 1,
    apps-script/Lib.js actionWriteTable_) — one POST instead of one GET per row."""
    payload = {"action": "writeTable", "sheet": sheet_name, "key": SHARED_KEY, "mode": mode,
               "headers": json.dumps(headers), "rows": json.dumps(rows)}
    if month:
        payload["month"] = month
    try:
        resp = requests.post(APPS_SCRIPT_URL, data=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  (couldn't write {sheet_name}: {e})")
        return None


def fetch_disposition_map(sheet_name, id_col, disposition_col, valid_dispositions=("paid", "dead")):
    """Generic last-row-wins reader for an append-only resolutions log. Returns {id: disposition}."""
    resolved = {}
    for row in sheet_get(sheet_name):
        if len(row) <= max(id_col, disposition_col):
            continue
        rid, disp = row[id_col], row[disposition_col]
        if disp in valid_dispositions:
            resolved[rid] = disp
        elif rid in resolved:
            del resolved[rid]  # undone
    return resolved


def compute_prior_carry_forward():
    """Auto-computes the carry-forward seed for this run from res_carry_forward (last month's
    computed output, written by that month's run — see write-back at the end of main()) plus
    carry_forward_resolutions (what's been resolved since). Replaces the hand-maintained
    snapshot that used to require manually diffing the live app before every run — the single
    most error-prone step in this whole pipeline historically."""
    baseline = sheet_get("res_carry_forward")
    prev_rows = [r for r in baseline if len(r) >= 9 and norm_month(r[0]) == PREV_LABEL]
    resolved = fetch_disposition_map("carry_forward_resolutions", id_col=2, disposition_col=7)
    out = []
    for r in prev_rows:
        _month, id_, from_month, emp, ref, type_, amount, dept, reason = r[:9]
        if resolved.get(id_) in ("paid", "dead"):
            continue
        customer = type_.split(" — ", 1)[-1].strip() if " — " in type_ else ""
        out.append({
            "id": id_, "fromMonth": norm_month(from_month), "emp": emp, "ref": ref, "type": type_,
            "amount": float(amount), "dept": dept, "lnk": last_name_key(customer),
        })

    # Manually-tracked pending items — created in the app from a manager note ("Track as pending
    # payout") or a flag resolved "Pay next month" — never go through res_carry_forward on their
    # own (that tab is only ever written by this function's own caller, main(), at the end of a
    # run). Without this, one of these would show up correctly for the rest of the month it was
    # created in (index.html replays `manual_carry_forward` client-side for that), then silently
    # vanish the next time this pipeline runs, since nothing here knew it existed. Folding it in
    # here gives it a real content-hashed id and durable multi-month tracking exactly like every
    # auto-generated carry-forward item, from the next run onward.
    latest_manual = {}
    for r in sheet_get("manual_carry_forward"):
        if len(r) < 8 or not r[1] or not r[3]:
            continue
        month, id_, _mgr, emp, type_, amount, dept, reason = r[:8]
        latest_manual[id_] = {"emp": emp, "type": type_, "amount": float(amount or 0), "dept": dept,
                               "reason": reason, "fromMonth": norm_month(month)}
    for id_, m in latest_manual.items():
        if resolved.get(id_) in ("paid", "dead"):
            continue
        out.append({**m, "id": id_, "ref": "", "lnk": ""})
    return out


def compute_carried_leads():
    """Same idea for the commercial lead rolling log — reads res_comm_leads (last month's
    computed output) + commlead_updates (resolutions since, not month-filtered since an update
    can reference a lead first seeded many months back), drops anything paid or dismissed."""
    baseline = sheet_get("res_comm_leads")
    prev_rows = [r for r in baseline if len(r) >= 10 and norm_month(r[0]) == PREV_LABEL]
    latest_status = {}
    for r in sheet_get("commlead_updates"):
        if len(r) < 6:
            continue
        _month, id_, tech, customer, job, status = r[:6]
        latest_status[id_] = status
    terminal = {"Sold & Completed", "Did Not Sell — Close Lead", "Dismissed"}
    out = []
    for r in prev_rows:
        _month, id_, lead_month, tech, job, customer, status, spiff, pay_month, paid = r[:10]
        location = r[10] if len(r) > 10 else ""  # added 2026-09; older baseline rows just have none
        eff_status = latest_status.get(id_, status)
        if eff_status in terminal:
            continue
        out.append({
            "id": id_, "month": norm_month(lead_month), "tech": tech, "job": job, "customer": customer,
            "location": location, "status": eff_status, "spiff": 0, "payMonth": "", "paid": False,
        })
    return out


def load_roster():
    """Reads the `roster` Sheet tab and returns the same shapes the hardcoded constants used to
    be — (steven_roster, caleb_roster, plumbers, comm_techs, ch_techs, office_names, excluded) —
    so the rest of the pipeline is unchanged below this point. This is what lets Billy add a new
    hire or remove someone who's left without a Claude Code session.

    steven_roster/caleb_roster are lists (not sets) to preserve Sheet row order, since that order
    is what determines display order in the app. Columns: name, team, role, eligible, active,
    exclusionReason, updatedBy, updatedAt.
    """
    # Last-row-wins by name, same append-only-edit-log pattern as every other editable tab in
    # this app (manual_adds, spiff_corrections, ...) — editing/deactivating someone means
    # appending a new row for that name, not mutating the old one, so this dedup step is what
    # makes an edit actually take effect instead of leaving the person in two states at once.
    latest = {}
    for r in sheet_get("roster"):
        if len(r) < 6 or not r[0]:
            continue
        name, team, role, eligible, active, reason = r[:6]
        # sheet_get() has no way to skip row 1 — a brand-new tab gets a real header row from
        # ensureSheet_, so "eligible"/"active" show up literally here instead of TRUE/FALSE.
        # Any genuine data row always has one of those two exact values; anything else (header,
        # or a malformed row) is skipped rather than guessed at.
        if str(eligible).strip().upper() not in ("TRUE", "FALSE"):
            continue
        latest[name] = (team, role, eligible, active, reason)

    steven, caleb = [], []
    plumbers, comm_techs, ch_techs, office = set(), set(), set(), set()
    excluded = {}
    for name, (team, role, eligible, active, reason) in latest.items():
        eligible_b = str(eligible).strip().upper() == "TRUE"
        active_b = str(active).strip().upper() == "TRUE"
        if not eligible_b:
            excluded[name] = reason or "Not eligible for spiffs"
            continue
        if not active_b:
            continue  # departed — not on any team/role set, so team_of()'s red-flag path catches
            # them if they still show up with activity, instead of silently dropping the money
            # (see the steven_emps/caleb_emps fallback near the bottom of main()).
        if role == "plumber":
            plumbers.add(name)
        elif role == "comm_tech":
            comm_techs.add(name)
        elif role == "ch_tech":
            ch_techs.add(name)
        elif role == "office":
            office.add(name)
        if team == "steven" and name not in steven:
            steven.append(name)
        elif team == "caleb" and name not in caleb:
            caleb.append(name)
    return steven, caleb, plumbers, comm_techs, ch_techs, office, excluded


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
    flags = {"steven": [], "caleb": [], "jenny": []}
    office_mems = defaultdict(float)
    office_mem_details = defaultdict(list)
    # Content-derived, not a sequence counter — flags/details raised for the same underlying
    # job/employee get the same id every run, so a manager's resolution survives a re-run instead
    # of silently orphaning when the fresh run's iteration order shifts which item gets which seq.
    new_flag_id = make_id_factory("flag")
    new_line_id = make_id_factory("ln")

    def ensure_emp(name):
        if name not in emps:
            emps[name] = {"name": name, "svc": 0, "ins": 0, "plb": 0, "com": 0, "chs": 0, "chi": 0, "dups": []}
        return emps[name]

    def add_spiff(name, col, amount, date, job, customer, type_, item, auto_added=False):
        e = ensure_emp(name)
        e[col] += amount
        spiff_detail[name].append({
            "lineId": new_line_id(name, job, type_, item, amount),
            "date": date, "job": job, "customer": customer, "type": type_, "item": item, "spiff": amount,
            **({"note": "Auto-added — not on Master Pay File"} if auto_added else {}),
        })

    def add_flag(mgr, emp, ref, title, detail, sev="yellow"):
        flags[mgr].append({
            "id": new_flag_id(mgr, emp, ref, title), "sev": sev, "emp": emp, "ref": ref, "resolved": False,
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
        # MPF has no item/code field (only EmployeeName/Activity/Date/JobNumber/GrossPay/
        # CustomerName/LocationName/LaborTypeCode — confirmed via a live field dump) — item is
        # just the customer name, not `f"{activity} — {customer}"`, which duplicated "Sales
        # Spiff" as both type and item and rendered as "Sales Spiff — Sales Spiff — <customer>".
        add_spiff(name, col, float(pay), date, job, customer, activity, customer)
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
            office_mem_details[sold_by].append({
                "customer": customer, "type": row.get("MembershipType") or "",
                "activation": row.get("ActivationMethod") or "", "soldOn": sold_on, "amount": bonus,
            })
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

        # Residential Stage 1 only counts when it's actually confirmed paid via the Master Pay
        # File. A lead that shows up on the Lead Request Report but never made it onto MPF means
        # the salesperson wasn't able to get in front of the customer to actually quote the job
        # (confirmed by Billy, 2026-09) — not a payable event, so no auto-add, no flag, and no
        # carry-forward gets generated from it. Previously this auto-added a $25 Stage 1 anyway
        # (via add_spiff(..., auto_added=True)), which for at least one real case (Karl Welch /
        # Epstein, job 165972686) went on to generate a phantom Stage 2 carry-forward next month
        # that duplicated a real payout already made to a different technician (Corey Ward) for
        # the same job.
        continue

    if unresolved_completers:
        for c in unresolved_completers:
            add_flag("steven", c, "Lead Request Report",
                      f"Unresolved lead requester — {c}",
                      f"'{c}' appears as a lead-request completer but doesn't match any known ServiceTitan "
                      f"username or email. Could be a new hire or a typo'd login. Verify who this is before "
                      f"crediting a Stage 1 spiff.", sev="red")

    # ── 5) Residential Stage 2 carry-forward ──
    # Auto-computed from res_carry_forward (last month's output) + carry_forward_resolutions —
    # see compute_prior_carry_forward() near the top of this file. This used to be a hardcoded
    # snapshot that had to be manually refreshed from the live app before every run (as of
    # 2026-09, the last month that needed that manual step).
    prior_carry_forward = compute_prior_carry_forward()
    # Manual resolutions from the app (Steven/Caleb marking a carry-forward item "paid" or "dead" directly) —
    # respect these so a dead lead doesn't keep rolling forward forever, and a manually-paid one doesn't reappear.
    def fetch_carry_forward_resolutions():
        import requests as _requests
        try:
            resp = _requests.get(APPS_SCRIPT_URL, params={"action": "get", "sheet": "carry_forward_resolutions", "key": SHARED_KEY}, timeout=10)
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
    # Content-derived from (employee, job/ref) — the same pending item now keeps the same id
    # every month until it's resolved, instead of a cf_<month>_<seq> counter that got reminted
    # from scratch every run (including ordinary month-to-month carry, not just a re-run), which
    # could silently orphan a "mark paid"/"mark dead" resolution already logged against it.
    new_cf_id = make_id_factory("cf")
    for cf in prior_carry_forward:
        key = (cf["emp"], cf["lnk"])
        if key in tgl_sold_seen:
            # Stage 2 was paid this month via MPF (already counted in the main MPF loop) — drop from the list.
            resolved_keys.add(key)
            continue
        if cf.get("id") and manually_resolved.get(cf["id"]) in ("paid", "dead"):
            continue  # manually resolved in the app — don't carry forward again
        carry_forward_out.append({
            "id": new_cf_id(cf["emp"], cf["ref"]), "fromMonth": cf["fromMonth"], "emp": cf["emp"], "ref": cf["ref"],
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
        ref = f"Job {detail['job']}" if detail["job"] else ""
        carry_forward_out.append({
            "id": new_cf_id(name, ref), "fromMonth": MONTH_LABEL, "emp": name,
            "ref": ref,
            "type": f"Lead Stage 2 — {detail['customer']}", "amount": 75, "dept": detail["dept"],
            "reason": f"Stage 1 paid {MONTH_LABEL}. Pay $75 when sold, installed, paid.",
            "resolved": False, "disposition": "", "note": "",
        })

    # ── 6) Commercial lead rolling log ──
    # Auto-computed from res_comm_leads (last month's output) + commlead_updates — see
    # compute_carried_leads() near the top of this file. Same history as the carry-forward
    # seed above: used to be a hand-maintained snapshot before 2026-09.
    carried_leads = compute_carried_leads()
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
    # Content-derived from (tech, customer key) — the same natural dedup key already used for
    # `known_leads` just above, so a re-run detects the same lead as the same lead instead of
    # minting it a new cl_<month>_<seq> counter id every time.
    new_cl_id = make_id_factory("cl")
    for row in leads:
        if row.get("State") != "Completed":
            continue
        name, resolved = resolve_completer(umap, row.get("Completer"))
        if not resolved or name not in COMM_TECHS:
            continue
        customer = (row.get("CustomerName") or row.get("LocationName") or "").strip()
        # Captured separately from `customer` (which falls back to LocationName only when
        # CustomerName is blank) so a customer with multiple locations — the exact case Billy
        # flagged as making the log unusable — shows both, instead of only ever showing the name.
        location = (row.get("LocationName") or "").strip()
        lnk = last_name_key(customer)
        key = (name, lnk)
        if key in known_leads or lnk in mpf_customer_keys.get(name, set()):
            continue  # already logged, or already paid via MPF this month
        known_leads.add(key)
        comm_leads_out.append({
            "id": new_cl_id(name, lnk), "month": MONTH_LABEL, "tech": name, "job": "",
            "customer": customer, "location": location, "status": "Pending", "spiff": 0, "payMonth": "", "paid": False,
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

    # ── 8) Payout ledger — cross-month duplicate detection + carryover history ──
    def fmt_amt(n):
        return f"${n:.2f}"

    def build_ledger_entries():
        entries = []
        for mgr_ in ("steven", "caleb"):
            for emp_name, lines in spiff_detail.get(mgr_, {}).items():
                for line in lines:
                    entries.append({
                        "month": MONTH_LABEL, "mgr": mgr_, "employee": emp_name,
                        "job": line.get("job", ""), "customer": line.get("customer", ""),
                        "type": line.get("type", ""), "item": line.get("item", ""),
                        "amount": line.get("spiff", 0),
                        "source": "auto-added" if line.get("note") else "MPF",
                    })
        for lead in comm_leads_out:
            if lead.get("paid"):
                entries.append({
                    "month": MONTH_LABEL, "mgr": team_of(lead["tech"]), "employee": lead["tech"],
                    "job": lead.get("job", ""), "customer": lead.get("customer", ""),
                    "type": "Commercial Lead", "item": f"Commercial lead — {lead['customer']}",
                    "amount": lead.get("spiff", 0), "source": "commercial-lead",
                })
        for d in rich_details:
            entries.append({
                "month": MONTH_LABEL, "mgr": "", "employee": "Rich Smith",
                "job": d["job"], "customer": d["customer"], "type": "Commission (3%)",
                "item": d["item"], "amount": d["commission"], "source": "rich-commission",
            })
        # One ledger row per sale (not one aggregated row per employee per month) — needed so the
        # duplicate check below has a customer name to match on. Office/CCS staff previously had
        # zero duplicate-detection coverage: an aggregated per-employee-per-month row has no job
        # number and no customer, so it could never match anything (confirmed 2026-09, Billy
        # asked why the Call Center Supervisor page never shows any flags).
        for name_, details in office_mem_details.items():
            for d in details:
                entries.append({
                    "month": MONTH_LABEL, "mgr": "jenny", "employee": name_,
                    "job": "", "customer": d.get("customer", ""), "type": "Membership Spiff",
                    "item": f"{d.get('type', '')} — {d.get('activation', '')}".strip(" —"),
                    "amount": d.get("amount", 0), "source": "office-membership",
                })
        return entries

    def fetch_ledger():
        import requests as _requests
        try:
            resp = _requests.get(APPS_SCRIPT_URL, params={"action": "get", "sheet": "spiff_ledger", "key": SHARED_KEY}, timeout=15)
            resp.raise_for_status()
            return resp.json().get("values") or []
        except Exception as e:
            print(f"  (couldn't fetch spiff_ledger — skipping duplicate check: {e})")
            return []

    ledger_entries = build_ledger_entries()
    prior_ledger = fetch_ledger()
    prior_job_keys = set()          # (employee, job) already paid in a prior month — same-tech repeat
    prior_owner_by_job = defaultdict(set)      # job -> {employees} paid in a prior month — cross-tech
    prior_owner_by_custkey = defaultdict(set)  # last-name key -> {employees}, only for job-less entries
    for row in prior_ledger:
        if len(row) < 5:
            continue
        prior_month, _mgr, prior_emp, prior_job, prior_cust = row[0], row[1], row[2], row[3], row[4]
        if norm_month(prior_month) == MONTH_LABEL:
            continue
        if prior_job:
            prior_job_keys.add((prior_emp, str(prior_job)))
            prior_owner_by_job[str(prior_job)].add(prior_emp)
        elif prior_cust:
            prior_owner_by_custkey[last_name_key(prior_cust)].add(prior_emp)

    # This month's own entries, grouped the same way, to catch two different technicians both
    # getting credited for the same job/customer within the same run (not just across months) —
    # e.g. Karl Welch and Corey Ward both credited for the same Epstein job in the same month.
    this_month_owner_by_job = defaultdict(set)
    this_month_owner_by_custkey = defaultdict(set)
    for entry in ledger_entries:
        if entry["job"]:
            this_month_owner_by_job[str(entry["job"])].add(entry["employee"])
        elif entry["customer"]:
            this_month_owner_by_custkey[last_name_key(entry["customer"])].add(entry["employee"])

    flagged_dup_keys = set()  # avoid raising the same (employees, job/customer) combo twice
    for entry in ledger_entries:
        emp = entry["employee"]
        job = str(entry["job"]) if entry["job"] else ""
        cust_key = last_name_key(entry["customer"]) if entry["customer"] else ""
        mgr_for_flag = entry["mgr"] or team_of(emp)
        if mgr_for_flag not in flags:
            continue

        if job and (emp, job) in prior_job_keys:
            add_flag(mgr_for_flag, emp, f"Job #{job}",
                      f"Possible duplicate payment — job #{job} already paid in a prior month",
                      f"{emp} is being paid {fmt_amt(entry['amount'])} for job #{job} "
                      f"({entry['item']}) this month, but the ledger shows this same job/employee combination "
                      f"was already paid in a previous month. Verify this isn't a duplicate before approving.",
                      sev="red")
            continue  # same-tech repeat already explains it — don't also raise the cross-tech check below

        # Cross-technician check: same job (or, when no job number exists, same customer by last
        # name) credited to a *different* employee, either in a prior month or this same run.
        other_owners = set()
        basis = ""
        if job:
            other_owners |= (prior_owner_by_job.get(job, set()) - {emp})
            other_owners |= (this_month_owner_by_job.get(job, set()) - {emp})
            basis = f"job #{job}"
        elif cust_key:
            other_owners |= (prior_owner_by_custkey.get(cust_key, set()) - {emp})
            other_owners |= (this_month_owner_by_custkey.get(cust_key, set()) - {emp})
            basis = f"customer \"{entry['customer']}\""
        if not other_owners:
            continue
        dup_key = tuple(sorted([emp, *other_owners])) + (job or cust_key,)
        if dup_key in flagged_dup_keys:
            continue
        flagged_dup_keys.add(dup_key)
        others_str = ", ".join(sorted(other_owners))
        add_flag(mgr_for_flag, emp, f"Job #{job}" if job else entry["customer"],
                  f"Possible duplicate spiff — {basis} credited to more than one technician",
                  f"{emp} is being paid {fmt_amt(entry['amount'])} for {basis} ({entry['item']}), but the payout "
                  f"ledger also shows {others_str} credited for the same {'job' if job else 'customer'}"
                  f"{' (no job number on this line, matched by customer name — verify manually)' if not job else ''}. "
                  f"Verify only one technician should be paid before approving.",
                  sev="red")
        ensure_emp(emp)["dups"].append(dup_key[-1])
        for other in other_owners:
            if other in emps:
                emps[other]["dups"].append(dup_key[-1])

    if os.environ.get("ORACLE_DRY_RUN"):
        print(f"  [ORACLE_DRY_RUN] Skipping spiff_ledger write ({len(ledger_entries)} entries would have been sent)")
    else:
        # replaceMonth (not append) so re-running an already-processed month overwrites its own
        # ledger rows instead of duplicating them — found 2026-09 when a real self-service re-run
        # of August wrote a second copy of every ledger-eligible line (Rich Smith's commissions,
        # Chris Port, Jenny Miller) because this used to append unconditionally on every run.
        ledger_rows = [[e["month"], e["mgr"], e["employee"], e["job"], e["customer"],
                         e["type"], e["item"], e["amount"], e["source"]] for e in ledger_entries]
        sheet_write_table("spiff_ledger", ["month", "mgr", "employee", "job", "customer", "type", "item", "amount", "source"],
                           ledger_rows, mode="replaceMonth", month=MONTH_LABEL)
        print(f"  Wrote {len(ledger_rows)} entries to spiff_ledger (replacing any prior {MONTH_LABEL} entries)")

    # Write this month's carry-forward/comm-lead output back to the Sheet — becomes next
    # month's auto-computed seed via compute_prior_carry_forward()/compute_carried_leads().
    # This is what makes the seed step self-service instead of a manual hand-refresh.
    cf_rows = [[MONTH_LABEL, c["id"], c["fromMonth"], c["emp"], c["ref"], c["type"], c["amount"],
                c["dept"], c["reason"]] for c in carry_forward_out]
    sheet_write_table("res_carry_forward", ["month", "id", "fromMonth", "emp", "ref", "type", "amount", "dept", "reason"],
                       cf_rows, mode="replaceMonth", month=MONTH_LABEL)
    cl_rows = [[MONTH_LABEL, l["id"], l["month"], l["tech"], l["job"], l["customer"], l["status"],
                l["spiff"], l["payMonth"], l["paid"], l.get("location", "")] for l in comm_leads_out]
    sheet_write_table("res_comm_leads", ["month", "id", "leadMonth", "tech", "job", "customer", "status", "spiff", "payMonth", "paid", "location"],
                       cl_rows, mode="replaceMonth", month=MONTH_LABEL)
    print(f"  Wrote {len(cf_rows)} carry-forward rows and {len(cl_rows)} comm-lead rows to the Sheet for next month's seed")

    # ── Output ──────────────────────────────────────────────────────
    steven_emps = [emps[n] for n in STEVEN_ROSTER if n in emps]
    caleb_emps = [emps[n] for n in CALEB_ROSTER if n in emps]
    # Anyone not on either roster still gets paid — never silently dropped. This used to land in
    # an "_unclassified" bucket that render_full_S.py never read, so their computed spiff simply
    # vanished from the app (a real bug, found 2026-09, only ever worked around by adding new
    # hires to the roster by hand before this bug could bite). team_of() already defaults an
    # unrecognized name to Steven's team and raises a red flag when it's first classified above —
    # mirror that default here so the money and the flag always land in the same place.
    known_names = set(STEVEN_ROSTER) | set(CALEB_ROSTER)
    steven_emps += [e for n, e in emps.items() if n not in known_names]

    result = {
        "month": MONTH_LABEL,
        "emps": {"steven": steven_emps, "caleb": caleb_emps},
        "flags": flags,
        "spiffDetail": {
            "steven": {n: d for n, d in spiff_detail.items() if n not in CALEB_ROSTER},
            "caleb": {n: d for n, d in spiff_detail.items() if n in CALEB_ROSTER},
        },
        "officeMems": [{"name": n, "total": round(t, 2), "details": office_mem_details.get(n, [])}
                       for n, t in office_mems.items()],
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

    unclassified_names = [n for n in emps if n not in known_names]
    print("\n=== SUMMARY ===")
    print(f"Steven's team: {len(steven_emps)} employees with spiffs")
    print(f"Caleb's team: {len(caleb_emps)} employees with spiffs")
    if unclassified_names:
        print(f"UNCLASSIFIED (flagged, paid under Steven by default): {unclassified_names}")
    print(f"Flags — Steven: {len(flags['steven'])}, Caleb: {len(flags['caleb'])}, Jenny: {len(flags['jenny'])}")
    print(f"Rich Smith commission: ${rich_commission} (base ${rich_total_base:.2f})")
    print(f"Commercial leads log: {len(comm_leads_out)} entries "
          f"({sum(1 for l in comm_leads_out if l['paid'])} paid this month, "
          f"{sum(1 for l in comm_leads_out if l['status'] == 'Pending')} new/pending)")
    if unresolved_completers:
        print(f"Unresolved lead-request usernames: {unresolved_completers}")
    print(f"\nFull output written to output_{FROM_DATE[:7]}.json")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python process_month.py "Sep 2026"')
        sys.exit(1)
    configure_month(sys.argv[1])
    load_roster_globals()
    main()
