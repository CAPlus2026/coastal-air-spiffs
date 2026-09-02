"""Self-service runner entry point — checks run_requests for a queued month and processes it
end to end: pulls ServiceTitan data, regenerates the S block, splices into index.html, validates,
and leaves index.html ready to commit. Intended to run from GitHub Actions on a schedule; the
actual `git commit`/`push` happens in the workflow, not here, so this stays testable standalone.

Usage: python run_requested_month.py
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

import process_month as pm  # reuses sheet_get/APPS_SCRIPT_URL/SHARED_KEY/month_config/norm_month


def find_queued_month():
    """run_requests is an append-only log (month, status, requestedBy, requestedAt, message) —
    same last-row-wins pattern as every other tab in this app. Returns the first month whose
    latest status is 'queued', or None."""
    rows = pm.sheet_get("run_requests")
    latest = {}
    for r in rows:
        if len(r) < 2:
            continue
        month = pm.norm_month(r[0])
        latest[month] = r[1]
    for month, status in latest.items():
        if status == "queued":
            return month
    return None


def append_run_status(month, status, message=""):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        import requests
        requests.get(pm.APPS_SCRIPT_URL, params={
            "action": "append", "sheet": "run_requests", "key": pm.SHARED_KEY,
            "values": json.dumps([month, status, "system", ts, message]),
        }, timeout=15)
    except Exception as e:
        print(f"  (couldn't append run_requests status — continuing anyway: {e})")


def splice_s_block(month):
    """Same splice this session has done by hand three times: find the const S={...}; block in
    index.html, replace it with the freshly rendered full_S_block.js, update the month constants.
    Returns the new file text — does NOT write it, so a failed validation never touches disk."""
    with open("index.html", encoding="utf-8") as f:
        lines = f.readlines()
    start = end = None
    for i, line in enumerate(lines):
        if line.startswith("const S={"):
            start = i
        elif start is not None and line.strip() == "};":
            end = i
            break
    if start is None or end is None:
        raise RuntimeError("Could not find const S={...}; block in index.html")

    with open("full_S_block.js", encoding="utf-8") as f:
        block = f.read()
    if not block.endswith("\n"):
        block += "\n"
    new_lines = lines[:start] + [block] + lines[end + 1:]
    html = "".join(new_lines)

    _, _, prev_label, next_label = pm.month_config(month)
    new_html, n = re.subn(
        r"const MONTH='[^']*', PREV_M='[^']*', NEXT_M='[^']*';",
        f"const MONTH='{month}', PREV_M='{prev_label}', NEXT_M='{next_label}';",
        html, count=1,
    )
    if n != 1:
        raise RuntimeError("Failed to update the MONTH/PREV_M/NEXT_M constant line in index.html")
    return new_html


def validate(html_text):
    """Same checks used manually all session: extract <script>, node --check it, sanity-check
    the spliced S literal is parseable and non-empty. Raises on any failure."""
    m = re.search(r"<script>([\s\S]*)</script>", html_text)
    if not m:
        raise RuntimeError("Could not find <script> block for validation")
    fd, tmp_path = tempfile.mkstemp(suffix=".js")
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(m.group(1))
        result = subprocess.run(["node", "--check", tmp_path], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"node --check failed: {result.stderr}")
    finally:
        os.unlink(tmp_path)

    sm = re.search(r"const S=(\{[\s\S]*?\n\});", html_text)
    if not sm:
        raise RuntimeError("Could not find a parseable const S={...}; block after splice")
    # Passed as a script FILE, not `node -e "..."` — the full S literal is tens of thousands of
    # characters and blows the Windows command-line length limit as an inline argument.
    fd2, tmp_path2 = tempfile.mkstemp(suffix=".js")
    os.close(fd2)
    try:
        with open(tmp_path2, "w", encoding="utf-8") as f:
            f.write(f"const S={sm.group(1)};\n"
                     f"if(!S.emps||!S.emps.steven) throw new Error('S.emps.steven missing');\n")
        check = subprocess.run(["node", tmp_path2], capture_output=True, text=True)
        if check.returncode != 0:
            raise RuntimeError(f"Spliced S literal failed sanity check: {check.stderr}")
    finally:
        os.unlink(tmp_path2)


def main():
    month = find_queued_month()
    if not month:
        print("No queued run request found — nothing to do.")
        return
    print(f"Processing {month}...")
    append_run_status(month, "running", "Started by GitHub Actions")
    try:
        subprocess.run([sys.executable, "process_month.py", month], check=True)
        subprocess.run([sys.executable, "render_full_S.py", month], check=True)
        new_html = splice_s_block(month)
        validate(new_html)
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(new_html)
        append_run_status(month, "complete", "Processed and spliced successfully")
        print(f"{month} processed and spliced successfully.")
    except Exception as e:
        append_run_status(month, "failed", str(e)[:500])
        print(f"FAILED processing {month}: {e}")
        raise


if __name__ == "__main__":
    main()
