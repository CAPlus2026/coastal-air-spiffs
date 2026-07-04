"""Run once after filling in .env to find the report category + report ID for each
of the 6 reports process_month.py needs. Prints everything so we can pick the right
IDs and hardcode them into report_ids.json.

Usage: python discover_reports.py
"""
import json

from st_client import ServiceTitanClient

WANTED = [
    "Master Pay File",
    "Accessory Sales",
    "Membership Report",
    "Lead Request Report",
    "Technician Performance",
    "Rich",  # partial match for Rich's Commission Report
]


def main():
    client = ServiceTitanClient()
    categories = client.list_report_categories()
    print(f"Found {len(categories.get('data', categories) if isinstance(categories, dict) else categories)} categories\n")

    cat_list = categories.get("data", categories) if isinstance(categories, dict) else categories
    matches = []
    for cat in cat_list:
        cat_id = cat.get("id") or cat.get("name")
        cat_name = cat.get("name", cat_id)
        try:
            reports = client.list_reports(cat_id)
        except Exception as e:
            print(f"  [skip category {cat_name}: {e}]")
            continue
        report_list = reports.get("data", reports) if isinstance(reports, dict) else reports
        for r in report_list:
            r_name = r.get("name", "")
            for w in WANTED:
                if w.lower() in r_name.lower():
                    entry = {"category": cat_id, "categoryName": cat_name, "reportId": r.get("id"), "reportName": r_name}
                    matches.append(entry)
                    print(f"MATCH [{w}] -> category={cat_id} ({cat_name}) reportId={r.get('id')} name={r_name!r}")

    print("\nAll categories/reports (for manual lookup if a report above didn't match):")
    for cat in cat_list:
        cat_id = cat.get("id") or cat.get("name")
        cat_name = cat.get("name", cat_id)
        try:
            reports = client.list_reports(cat_id)
        except Exception:
            continue
        report_list = reports.get("data", reports) if isinstance(reports, dict) else reports
        for r in report_list:
            print(f"  [{cat_id}] {r.get('id')}: {r.get('name')}")

    with open("report_matches_raw.json", "w") as f:
        json.dump(matches, f, indent=2)
    print("\nWrote report_matches_raw.json — review it, then fill report_ids.json with the confirmed IDs.")


if __name__ == "__main__":
    main()
