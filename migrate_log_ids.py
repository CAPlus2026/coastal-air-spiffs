"""Migration helper for orphaned resolution/correction rows — the remedy for what
audit_orphans.py (and process_month.py's pre-flight gate) finds.

Reuses audit_orphans.py's exact matching rules (never re-derives them) so this can't drift from
what the audit tool itself would report as recoverable. Dry-run by default: prints what it would
do and refuses to touch anything ambiguous. --apply appends corrected rows carrying the current
(correct) id, plus a `migratedFrom=<old id>` provenance column — never rewrites history, since
every tab in this app relies on append-only logs for its own audit trail.

Usage:
  python migrate_log_ids.py "Aug 2026"                 # dry run, all tabs
  python migrate_log_ids.py "Aug 2026" --sheet carry_forward_resolutions
  python migrate_log_ids.py "Aug 2026" --apply          # actually write the corrected rows

Only handles the "recoverable" verdict (exactly one unambiguous candidate) — "ambiguous" and
"obsolete"/"unrecoverable_legacy_row" orphans need a human to look at them directly (see
audit_orphans.py's own output for those) rather than a guess baked into an automated tool.
"""
import argparse
import json
import time

import audit_orphans
import process_month as pm

APPLY_FNS = {}


def _timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def apply_carry_forward(orphan, output, month_label):
    new_id = orphan["candidates"][0]
    current = next(c for c in output["carryForward"] if c["id"] == new_id)
    row = [month_label, "steven", new_id, current["emp"], current["ref"], current["type"],
           current["amount"], orphan["disposition"], orphan["note"], _timestamp(), orphan["old_id"]]
    pm.sheet_write_table("carry_forward_resolutions",
                          ["month", "mgr", "id", "emp", "ref", "type", "amount", "disposition",
                           "note", "timestamp", "migratedFrom"],
                          [row], mode="append")
    return row


APPLY_FNS["carry_forward_resolutions"] = apply_carry_forward


def apply_commlead(orphan, output, month_label):
    new_id = orphan["candidates"][0]
    current = next(l for l in output["commLeads"] if l["id"] == new_id)
    row = [month_label, new_id, current["tech"], current["customer"], current.get("job", ""),
           orphan["status"], current.get("spiff", 0), current.get("payMonth", ""), _timestamp(),
           "", "", current.get("location", ""), orphan["old_id"]]
    pm.sheet_write_table("commlead_updates",
                          ["month", "id", "tech", "customer", "job", "status", "spiff", "payMonth",
                           "timestamp", "disposition", "note", "location", "migratedFrom"],
                          [row], mode="append")
    return row


APPLY_FNS["commlead_updates"] = apply_commlead


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("month", help='e.g. "Aug 2026"')
    ap.add_argument("--sheet", choices=["carry_forward_resolutions", "commlead_updates"],
                     help="Only migrate this tab (default: both — flags/spiff_corrections need "
                          "a human, not this tool, so they're never included here).")
    ap.add_argument("--apply", action="store_true", help="Actually write the corrected rows (default: dry run).")
    args = ap.parse_args()

    pm.configure_month(args.month)
    output_path = pm._output_path_for(args.month)
    with open(output_path) as f:
        output = json.load(f)

    audits = {
        "carry_forward_resolutions": audit_orphans.audit_carry_forward,
        "commlead_updates": audit_orphans.audit_commlead,
    }
    tabs = [args.sheet] if args.sheet else list(audits.keys())

    total_applied = 0
    for tab in tabs:
        orphans = audits[tab](output, args.month)
        recoverable = [o for o in orphans if o["verdict"] == "recoverable"]
        skipped = [o for o in orphans if o["verdict"] not in ("recoverable", "already_remediated")]
        print(f"=== {tab}: {len(recoverable)} recoverable, {len(skipped)} need a human ===")
        for o in recoverable:
            who = o.get("emp") or o.get("tech")
            print(f"  {o['old_id']} -> {o['candidates'][0]} | {who}"
                  + (f" | {o.get('type') or o.get('customer')}"))
            if args.apply:
                APPLY_FNS[tab](o, output, args.month)
                total_applied += 1
        for o in skipped:
            who = o.get("emp") or o.get("tech")
            print(f"  SKIPPED ({o['verdict']}): {o['old_id']} | {who} — needs a human, see audit_orphans.py")

    if args.apply:
        print(f"\nApplied {total_applied} migration(s).")
    else:
        print(f"\nDRY RUN — would apply {sum(1 for t in tabs for o in audits[t](output, args.month) if o['verdict']=='recoverable')} migration(s). Re-run with --apply to write them.")


if __name__ == "__main__":
    main()
