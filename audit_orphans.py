"""Read-only backlog triage: for each of the four Sheet tabs where a manager's action can
silently orphan against a fresh pipeline run (carry_forward_resolutions, commlead_updates, flags,
spiff_corrections), find every recorded resolution/correction whose id no longer matches anything
in the current month's computed output, and propose a natural-key remap candidate for it.

This is the direct answer to "of the N pending items, how many were already resolved and lost?" —
built once as an importable module so process_month.py's future pre-flight gate (Phase 2) and
migrate_log_ids.py (Phase 3) reuse the exact same matching rules instead of each reimplementing
"how do I match this row back to its target" slightly differently, which is how two of today's
real incidents happened (a stale `ref` column, and a last-name-only match producing a false
positive between two unrelated customers who share a surname).

Matching rules, one per tab — deliberately NOT matching on any column that can legitimately
differ between runs for the same real-world item (see full docstring on each match_* function):
  - carry_forward_resolutions -> (emp, type)
  - commlead_updates          -> (tech, last_name_key(customer))
  - flags                     -> (mgr, emp, ref, detail)
  - spiff_corrections         -> (mgr, empName, job, customer, type, item, amount) when the row
                                  carries those natural-key columns (added 2026-09-04); legacy
                                  rows without them are reported as unrecoverable, not guessed at.

No writes. Usage: python audit_orphans.py "Aug 2026" [--json out.json]

carry_forward_resolutions and commlead_updates recognize an "already_remediated" verdict: if an
orphan's single candidate id ALREADY has its own recorded disposition/status, it's been fixed by
a separate corrective row (append-only logs never remove the original orphaned row, so without
this it would be reported as an open, money-moving orphan forever, even after being migrated).
Added 2026-09-04 after this exact thing blocked a real pre-flight run over 13 items that had
already been corrected the day before. flags and spiff_corrections don't have this check yet —
spiff_corrections in particular often can't be automatically remapped at all (a legacy row
without natural-key columns has nothing to match against), so those need a human to look at
audit_orphans.py's raw output rather than trusting an "already handled" inference.
"""
import argparse
import json
import sys
from collections import defaultdict

import process_month as pm  # reuses sheet_get/norm_month/last_name_key/configure_month/MONTH_NAMES


# ── Per-tab: compute latest-state-per-id from the raw Sheet rows ─────────────────────────────
def latest_carry_forward_resolutions(month_label):
    """id -> {emp, ref, type, amount, disposition, note} for rows recorded against `month_label`
    (mirrors index.html's loadSheets() cfRes replay, including its month filter)."""
    latest = {}
    for r in pm.sheet_get("carry_forward_resolutions", required=True):
        if len(r) < 9:
            continue
        month, mgr, id_, emp, ref, type_, amount, disposition, note = r[:9]
        if pm.norm_month(month) != month_label or not id_:
            continue
        latest[id_] = {"mgr": mgr, "emp": emp, "ref": ref, "type": type_, "amount": amount,
                        "disposition": disposition or "", "note": note or ""}
    return latest


def latest_commlead_updates(month_label):
    """id -> {tech, customer, status, ...} for rows recorded against `month_label`."""
    latest = {}
    for r in pm.sheet_get("commlead_updates", required=True):
        if len(r) < 6:
            continue
        month, id_, tech, customer, job, status = r[:6]
        if pm.norm_month(month) != month_label or not id_:
            continue
        latest[id_] = {"tech": tech, "customer": customer, "job": job, "status": status or ""}
    return latest


def latest_flags(month_label):
    """id -> {mgr, emp, ref, detail, resolved, disp} for rows recorded against `month_label`."""
    latest = {}
    for r in pm.sheet_get("flags", required=True):
        if len(r) < 12:
            continue
        month, mgr, id_ = r[0], r[1], r[2]
        detail, disp, note, resolved = r[8], r[9], r[10], r[11]
        if pm.norm_month(month) != month_label or not id_ or not mgr:
            continue
        latest[id_] = {"mgr": mgr, "detail": detail or "", "disp": disp or "", "note": note or "",
                        "resolved": str(resolved).lower() == "true"}
    return latest


def latest_spiff_corrections(month_label):
    """id (here, a synthetic mgr|empName|idx key, since these rows have no single id column) ->
    the latest action for that (mgr, empName, idx). Carries the natural-key columns (added
    2026-09-04) through when present so callers can attempt a remap; legacy rows (7 columns, no
    natural-key data) are marked recoverable=False outright."""
    latest = {}
    for r in pm.sheet_get("spiff_corrections", required=True):
        if len(r) < 7:
            continue
        month, mgr, emp_name, idx_str, action, reason, ts = r[:7]
        if pm.norm_month(month) != month_label:
            continue
        line_id = r[7] if len(r) > 7 else ""
        has_natural_key = len(r) >= 13 and any(r[8:13])
        key = f"{mgr}|{emp_name}|{idx_str}"
        latest[key] = {
            "mgr": mgr, "emp": emp_name, "idx": idx_str, "action": action, "reason": reason or "",
            "lineId": line_id,
            "job": r[8] if len(r) > 8 else "", "customer": r[9] if len(r) > 9 else "",
            "type": r[10] if len(r) > 10 else "", "item": r[11] if len(r) > 11 else "",
            "amount": r[12] if len(r) > 12 else "",
            "recoverable": has_natural_key,
        }
    return latest


# ── Per-tab: find orphans + propose a remap against the current computed output ─────────────
def audit_carry_forward(output, month_label):
    current_by_id = {c["id"]: c for c in output["carryForward"]}
    current_by_key = defaultdict(list)
    for c in output["carryForward"]:
        current_by_key[(c["emp"], c["type"])].append(c["id"])
    all_resolutions = latest_carry_forward_resolutions(month_label)

    orphans = []
    for id_, r in all_resolutions.items():
        if not r["disposition"]:
            continue  # an "undone" row — nothing to check
        if id_ in current_by_id:
            continue  # still matches — not an orphan
        candidates = current_by_key.get((r["emp"], r["type"]), [])
        # Already fixed by a separate corrective row under the new id (append-only logs never
        # remove the original orphaned row, so it would otherwise be reported as open forever,
        # even after being migrated by hand or by migrate_log_ids.py). Confirmed real 2026-09-04:
        # without this check, the pre-flight gate blocked a live run over 13 items that had
        # already been corrected the day before.
        if len(candidates) == 1 and all_resolutions.get(candidates[0], {}).get("disposition"):
            orphans.append({
                "tab": "carry_forward_resolutions", "old_id": id_, "emp": r["emp"], "type": r["type"],
                "disposition": r["disposition"], "note": r["note"], "money_moving": False,
                "candidates": candidates, "verdict": "already_remediated",
            })
            continue
        orphans.append({
            "tab": "carry_forward_resolutions", "old_id": id_, "emp": r["emp"], "type": r["type"],
            "disposition": r["disposition"], "note": r["note"],
            "money_moving": r["disposition"] in ("paid",),
            "candidates": candidates,
            "verdict": ("recoverable" if len(candidates) == 1 else
                        "ambiguous" if len(candidates) > 1 else "obsolete"),
        })
    return orphans


def audit_commlead(output, month_label):
    current_by_id = {l["id"]: l for l in output["commLeads"]}
    current_by_key = defaultdict(list)
    for l in output["commLeads"]:
        current_by_key[(l["tech"], pm.last_name_key(l["customer"]))].append(l["id"])
    all_updates = latest_commlead_updates(month_label)

    terminal = {"Sold & Completed", "Did Not Sell — Close Lead", "Dismissed"}
    orphans = []
    for id_, r in all_updates.items():
        if not r["status"]:
            continue
        if id_ in current_by_id:
            continue
        candidates = current_by_key.get((r["tech"], pm.last_name_key(r["customer"])), [])
        # Same "already fixed by a separate corrective row" check as carry-forward above.
        if len(candidates) == 1 and all_updates.get(candidates[0], {}).get("status"):
            orphans.append({
                "tab": "commlead_updates", "old_id": id_, "tech": r["tech"], "customer": r["customer"],
                "status": r["status"], "money_moving": False,
                "candidates": candidates, "verdict": "already_remediated",
            })
            continue
        orphans.append({
            "tab": "commlead_updates", "old_id": id_, "tech": r["tech"], "customer": r["customer"],
            "status": r["status"], "money_moving": r["status"] in terminal,
            "candidates": candidates,
            "verdict": ("recoverable" if len(candidates) == 1 else
                        "ambiguous" if len(candidates) > 1 else "obsolete"),
        })
    return orphans


def audit_flags(output, month_label):
    current_by_id = set()
    current_by_key = defaultdict(list)
    for mgr, flags in output["flags"].items():
        for f in flags:
            current_by_id.add(f["id"])
            current_by_key[(mgr, f["emp"], f["ref"], f["detail"])].append(f["id"])

    orphans = []
    for id_, r in latest_flags(month_label).items():
        if not r["disp"] and not r["resolved"]:
            continue
        if id_ in current_by_id:
            continue
        candidates = current_by_key.get((r["mgr"], r.get("emp", ""), r.get("ref", ""), r["detail"]), [])
        orphans.append({
            "tab": "flags", "old_id": id_, "mgr": r["mgr"], "detail": r["detail"],
            "disposition": r["disp"], "money_moving": r["disp"] == "Pay this month",
            "candidates": candidates,
            "verdict": ("recoverable" if len(candidates) == 1 else
                        "ambiguous" if len(candidates) > 1 else "obsolete"),
        })
    return orphans


def audit_spiff_corrections(output, month_label):
    # Build lineId -> exists, and a natural-key -> lineId index from the CURRENT spiffDetail.
    current_line_ids = set()
    current_by_key = defaultdict(list)
    for mgr in ("steven", "caleb"):
        for emp_name, lines in output["spiffDetail"].get(mgr, {}).items():
            for line in lines:
                lid = line.get("lineId", "")
                if lid:
                    current_line_ids.add(lid)
                key = (mgr, emp_name, line.get("job", ""), line.get("customer", ""),
                       line.get("type", ""), line.get("item", ""), str(line.get("spiff", "")))
                current_by_key[key].append(lid)

    orphans = []
    for key, r in latest_spiff_corrections(month_label).items():
        if r["action"] not in ("flagged", "unflagged"):
            continue
        if r["lineId"] and r["lineId"] in current_line_ids:
            continue  # still matches by lineId — not an orphan
        if not r["recoverable"]:
            orphans.append({
                "tab": "spiff_corrections", "old_id": key, "emp": r["emp"], "action": r["action"],
                "money_moving": r["action"] == "flagged",
                "candidates": [], "verdict": "unrecoverable_legacy_row",
            })
            continue
        natural_key = (r["mgr"], r["emp"], r["job"], r["customer"], r["type"], r["item"], str(r["amount"]))
        candidates = current_by_key.get(natural_key, [])
        orphans.append({
            "tab": "spiff_corrections", "old_id": key, "emp": r["emp"], "action": r["action"],
            "money_moving": r["action"] == "flagged",
            "candidates": [c for c in candidates if c],
            "verdict": ("recoverable" if len([c for c in candidates if c]) == 1 else
                        "ambiguous" if len(candidates) > 1 else "obsolete"),
        })
    return orphans


def run_audit(month_label, output):
    """Returns the full orphan list across all four tabs. `output` is the parsed
    output_<month>.json dict for `month_label` — the caller loads it so this module has no
    opinion about where that file lives."""
    return (audit_carry_forward(output, month_label)
            + audit_commlead(output, month_label)
            + audit_flags(output, month_label)
            + audit_spiff_corrections(output, month_label))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("month", help='e.g. "Aug 2026"')
    ap.add_argument("--json", help="also write the full result to this path")
    args = ap.parse_args()

    pm.configure_month(args.month)
    mon_str, year_str = args.month.split()
    output_file = f"output_{year_str}-{pm._MONTH_NAMES.index(mon_str) + 1:02d}.json"
    with open(output_file) as f:
        output = json.load(f)

    orphans = run_audit(args.month, output)

    by_verdict = defaultdict(list)
    for o in orphans:
        by_verdict[o["verdict"]].append(o)

    print(f"=== Orphan audit for {args.month} ({len(orphans)} total) ===\n")
    for verdict in ("recoverable", "ambiguous", "obsolete", "unrecoverable_legacy_row"):
        rows = by_verdict.get(verdict, [])
        if not rows:
            continue
        money = sum(1 for r in rows if r["money_moving"])
        print(f"-- {verdict} ({len(rows)}, {money} money-moving) --")
        for r in rows:
            who = r.get("emp") or r.get("tech") or ""
            what = r.get("type") or r.get("customer") or r.get("detail") or r.get("action") or ""
            cand = f" -> {r['candidates']}" if r["candidates"] else ""
            print(f"  [{r['tab']}] {r['old_id']} | {who} | {what}{cand}")
        print()

    if args.json:
        with open(args.json, "w") as f:
            json.dump(orphans, f, indent=2)
        print(f"Full detail written to {args.json}")


if __name__ == "__main__":
    main()
