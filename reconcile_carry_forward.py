"""Master-Pay-File reconciliation for pending carry-forward items (Phase 4 of the reliability
plan) — answers "has this employee's Stage 1 lead actually been credited with a Stage 2 sale?"
by checking the ONE real source of truth for that: Master Pay File itself.

Rebuilt 2026-09-04 after the first version shipped with the wrong signal. That version checked
whether ServiceTitan showed ANY completed job for the customer since the lead — which is NOT the
same thing as this employee's lead having been credited. ServiceTitan links a customer's eventual
sold estimate to exactly ONE lead; if a different tech also touched the same customer and their
lead is the one that got linked, the original tech's lead is dead and MPF will never post a Stage
2 for them — even though a real job for that customer did complete. A customer having a completed
job is not evidence of anything for this specific employee.

The correct and ONLY correct signal (explained directly by the business owner, 2026-09-04): "if
the master pay file doesn't flag that it's a paid lead, assume nobody ever estimated it." This is
also explicitly NOT time-sensitive — a lead can sit for months before a customer books, and
whenever MPF eventually posts the "TGL Lead Sold Res" activity for that employee, that's the
determination, whatever the calendar says.

Given that, the normal monthly pipeline already performs the correct check every month it runs
(see stage1_paid_detail / tgl_sold_seen in process_month.py, which look for "TGL Lead Sold Res" in
THAT month's own MPF pull) — a real sale, whenever it posts, is auto-detected and excluded from
carrying forward by the ordinary run, with zero manual reconciliation needed. This script's only
useful job is checking whether MPF has recorded that activity in some window the ordinary
month-by-month runs might not have re-checked (a wide historical pull is cheap and catches any
gap), NOT independently guessing from ServiceTitan job-completion data.

Usage:
  python reconcile_carry_forward.py "Aug 2026"             # read-only report
  python reconcile_carry_forward.py "Aug 2026" --json out.json
  python reconcile_carry_forward.py "Aug 2026" --apply     # append 'paid' resolutions, but ONLY
                                                            # for items MPF itself shows sold —
                                                            # every other verdict is report-only,
                                                            # never auto-applied.
"""
import argparse
import json
import time
from collections import defaultdict
from datetime import date

import process_month as pm

TODAY = date.today().strftime("%Y-%m-%d")


def fetch_mpf_range(from_date, to_date):
    """One wide Master Pay File pull covering every pending item's fromMonth through today,
    instead of guessing from ServiceTitan job/customer data. Same report as the normal monthly
    pipeline uses (fetch_report("masterPayFile")), just with an explicit wider date range."""
    meta = pm.REPORT_IDS["masterPayFile"]
    params = [{"name": "From", "value": from_date}, {"name": "To", "value": to_date},
              *meta.get("extraParams", [])]
    fields, rows = pm.get_client().get_report_data_all_pages(meta["category"], meta["reportId"], parameters=params)
    field_names = [f["name"] for f in fields]
    return [dict(zip(field_names, row)) for row in rows]


def build_sold_index(mpf_rows):
    """(employee, last_name_key(customer)) -> earliest {date, job} where MPF recorded
    "TGL Lead Sold Res" for that employee — the exact same activity/matching the ordinary
    monthly pipeline checks via tgl_sold_seen, just built once across a wide date range."""
    index = defaultdict(list)
    for row in mpf_rows:
        if row.get("Activity") != "TGL Lead Sold Res":
            continue
        name = row.get("EmployeeName")
        customer = row.get("CustomerName") or ""
        if not name or not customer:
            continue
        key = (name, pm.last_name_key(customer))
        index[key].append({"job": row.get("JobNumber"), "date": (row.get("Date") or "")[:10]})
    for key in index:
        index[key].sort(key=lambda d: d["date"])
    return index


def already_paid_elsewhere(item, ledger_rows):
    """Checks spiff_ledger for this customer's Stage 2 already paid to someone other than
    item['emp'] — real past ledger entries, not a guess, so this check stays valid regardless of
    the job-completion methodology problem the rest of this module was rebuilt over."""
    type_ = item.get("type", "")
    customer_name = type_.split(" — ", 1)[-1].strip() if " — " in type_ else ""
    key = pm.full_customer_key(customer_name)
    if not key:
        return None
    for row in ledger_rows:
        if len(row) < 5:
            continue
        _month, _mgr, employee, _job, customer = row[:5]
        if employee != item["emp"] and pm.full_customer_key(customer) == key:
            return employee
    return None


def reconcile(pending_items, sold_index, ledger_rows):
    results = []
    for item in pending_items:
        other_emp = already_paid_elsewhere(item, ledger_rows)
        if other_emp:
            results.append({**item, "verdict": "already_paid_elsewhere",
                             "detail": f"spiff_ledger shows this customer already paid to {other_emp}"})
            continue

        type_ = item.get("type", "")
        customer_name = type_.split(" — ", 1)[-1].strip() if " — " in type_ else ""
        lnk = pm.last_name_key(customer_name)
        if not lnk:
            results.append({**item, "verdict": "no_customer_name", "detail": "couldn't extract a customer name to check"})
            continue

        sold = sold_index.get((item["emp"], lnk))
        if sold:
            hit = sold[0]
            results.append({**item, "verdict": "mpf_confirms_sold",
                             "detail": f"MPF shows TGL Lead Sold Res for this employee, job {hit['job']}, on {hit['date']}",
                             "mpf_job": hit["job"], "mpf_date": hit["date"]})
        else:
            results.append({**item, "verdict": "still_open",
                             "detail": "no TGL Lead Sold Res found for this employee in Master Pay File — "
                                       "per the business owner, assume nobody has estimated it yet"})
    return results


def apply_results(results, month_label):
    applied = 0
    for r in results:
        if r["verdict"] != "mpf_confirms_sold":
            continue
        note = f"Confirmed via Master Pay File — TGL Lead Sold Res job {r['mpf_job']} on {r['mpf_date']}"
        row = [month_label, "reconciliation", r["id"], r["emp"], r["ref"], r["type"], r["amount"],
               "paid", note, time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())]
        pm.sheet_write_table("carry_forward_resolutions",
                              ["month", "mgr", "id", "emp", "ref", "type", "amount", "disposition", "note", "timestamp"],
                              [row], mode="append")
        applied += 1
    return applied


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("month", help='e.g. "Aug 2026"')
    ap.add_argument("--json", help="also write the full result to this path")
    ap.add_argument("--apply", action="store_true",
                     help="Append 'paid' resolutions for items Master Pay File itself confirms sold. "
                          "Every other verdict is always report-only, never auto-applied.")
    args = ap.parse_args()

    pm.configure_month(args.month)
    output_path = pm._output_path_for(args.month)
    with open(output_path) as f:
        output = json.load(f)

    pending = [c for c in output["carryForward"] if not c["resolved"]]
    if not pending:
        print(f"No pending carry-forward items for {args.month}.")
        return

    # fromMonth is stored as a label like "Jul 2026" -- convert to a real start date for the report.
    mon_str, year_str = min((c["fromMonth"] for c in pending),
                             key=lambda m: (int(m.split()[1]), pm._MONTH_NAMES.index(m.split()[0]))).split()
    from_date = f"{year_str}-{pm._MONTH_NAMES.index(mon_str) + 1:02d}-01"

    print(f"Pulling Master Pay File from {from_date} through {TODAY} to check {len(pending)} "
          f"pending carry-forward item(s) for {args.month}...\n")
    mpf_rows = fetch_mpf_range(from_date, TODAY)
    sold_index = build_sold_index(mpf_rows)
    ledger_rows = pm.sheet_get("spiff_ledger")

    results = reconcile(pending, sold_index, ledger_rows)

    by_verdict = defaultdict(list)
    for r in results:
        by_verdict[r["verdict"]].append(r)

    for verdict in ("already_paid_elsewhere", "mpf_confirms_sold", "still_open", "no_customer_name"):
        rows = by_verdict.get(verdict, [])
        if not rows:
            continue
        print(f"-- {verdict} ({len(rows)}) --")
        for r in rows:
            print(f"  {r['emp']:20s} {r['type']:45s} {r['detail']}")
        print()

    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Full detail written to {args.json}")

    if args.apply:
        n = apply_results(results, args.month)
        print(f"\nApplied {n} 'paid' resolution(s) confirmed by Master Pay File.")
    else:
        n_would = len(by_verdict.get("mpf_confirms_sold", []))
        print(f"\nDRY RUN — would mark {n_would} item(s) paid. Re-run with --apply to write them.")


if __name__ == "__main__":
    main()
