"""Pull a small sample (fields + first 3 rows) of each of the 6 SPIFF reports
for a given month, so we can confirm shapes before running the full pipeline.

Usage: python sample_all_reports.py [YYYY-MM-DD] [YYYY-MM-DD]
       (defaults to June 2026 if no dates given)
"""
import json
import sys

from st_client import ServiceTitanClient

with open("report_ids.json") as f:
    REPORTS = json.load(f)

client = ServiceTitanClient()
FROM = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
TO = sys.argv[2] if len(sys.argv) > 2 else "2026-06-30"

for key, meta in REPORTS.items():
    print(f"\n=== {key} :: {meta['name']} ===")
    params = [{"name": "From", "value": FROM}, {"name": "To", "value": TO}, *meta.get("extraParams", [])]
    try:
        result = client.get_report_data(meta["category"], meta["reportId"], parameters=params, page=1, page_size=3)
        print("Fields:", [f["name"] for f in result.get("fields", [])])
        print("Sample rows:")
        for row in result.get("data", [])[:3]:
            print(" ", row)
        print("Total on page:", len(result.get("data", [])), "hasMore:", result.get("hasMore"))
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", str(e))
        print("ERROR:", body[:500])
