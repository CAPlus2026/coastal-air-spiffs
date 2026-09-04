"""Test infrastructure for process_month.py.

Mocks the two external dependencies (ServiceTitan reports/settings, and the Apps Script Sheet
backend) so compute()/commit() can be exercised end-to-end, including across multiple simulated
monthly runs, entirely offline and without touching any real data.

Fixtures are synthetic (invented names/jobs), not captured real payroll data — this repo is
public, and a captured fixture would freeze real dollar amounts into git history forever.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import process_month as pm  # noqa: E402


class FakeSheets:
    """In-memory stand-in for the Apps Script Sheet backend. Persists across multiple
    compute()/commit() calls within one test (so a test can simulate several monthly runs in
    sequence, which is what most of the interesting bugs in this project have involved), but
    never touches the real network or the real spreadsheet.
    """

    def __init__(self):
        self.tabs = {}  # sheet_name -> list[list[str]] (rows only, no header row)

    def get(self, sheet_name):
        return [list(r) for r in self.tabs.get(sheet_name, [])]

    def append(self, sheet_name, row):
        self.tabs.setdefault(sheet_name, []).append(list(row))

    def write_table(self, sheet_name, rows, mode, month=None):
        existing = self.tabs.setdefault(sheet_name, [])
        if mode == "replaceMonth":
            existing[:] = [r for r in existing if pm.norm_month(r[0]) != month]
        existing.extend([list(r) for r in rows])
        return {"success": True, "wrote": len(rows)}


@pytest.fixture
def fake_sheets(monkeypatch):
    store = FakeSheets()

    def fake_sheet_get(sheet_name, timeout=20, required=False):
        return store.get(sheet_name)

    def fake_sheet_write_table(sheet_name, headers, rows, mode="replaceMonth", month=None):
        return store.write_table(sheet_name, rows, mode, month)

    def fake_sheet_append(sheet_name, row):
        store.append(sheet_name, row)

    monkeypatch.setattr(pm, "sheet_get", fake_sheet_get)
    monkeypatch.setattr(pm, "sheet_write_table", fake_sheet_write_table)
    # A few call sites (audit_orphans.py, and any future migration helper) use requests.get
    # directly against the Apps Script `append` action rather than sheet_write_table — none of
    # process_month.py's own code does this anymore (Phase 0 consolidated it), but expose the
    # helper on the store anyway so a test can seed a resolution/correction row the same way a
    # manager's browser would (an append), not just by pre-loading write_table rows.
    store.seed_append = fake_sheet_append
    return store


# ── Synthetic roster ──────────────────────────────────────────────────────────────────────────
# name, team, role, eligible, active, exclusionReason, updatedBy, updatedAt
ROSTER_ROWS = [
    ["Test Tech One", "steven", "tech", "TRUE", "TRUE", "", "test", "2026-01-01T00:00:00.000Z"],
    ["Test Tech Two", "steven", "tech", "TRUE", "TRUE", "", "test", "2026-01-01T00:00:00.000Z"],
    ["Test Comm Tech", "steven", "comm_tech", "TRUE", "TRUE", "", "test", "2026-01-01T00:00:00.000Z"],
    ["Test CH Tech", "caleb", "ch_tech", "TRUE", "TRUE", "", "test", "2026-01-01T00:00:00.000Z"],
    ["Test Office Rep", "steven", "office", "TRUE", "TRUE", "", "test", "2026-01-01T00:00:00.000Z"],
]

# A real spiff_rates.json code, used as-is so the accessory cross-check doesn't hit "unrecognized
# code" — spiff_rates.json is static config, not something under test here.
ACCESSORY_CODE = "BM-RENEWAL-1"


@pytest.fixture
def fixture_reports():
    """Returns a dict of report-key -> list[dict], shaped like fetch_report()'s real return value
    (one dict per row, keyed by the report's real column names). Empty by default for reports a
    given test doesn't care about."""
    return {
        "masterPayFile": [],
        "accessorySales": [],
        "membershipReport": [],
        "leadRequestReport": [],
        "technicianPerformance": [],
        "richCommissionReport": [],
    }


@pytest.fixture
def mock_pipeline(monkeypatch, fake_sheets, fixture_reports, tmp_path):
    """The main fixture most tests want: wires fixture_reports into fetch_report(), seeds the
    roster into fake_sheets, and stubs build_umap(). Tests mutate `fixture_reports` (in-place,
    before calling pm.compute()) to shape the scenario, and can pre-seed `fake_sheets.tabs[...]`
    directly for res_carry_forward/carry_forward_resolutions/etc. scenarios.

    Returns fake_sheets, so a test that wants both fixture_reports and fake_sheets access has one
    fixture to depend on.
    """
    fake_sheets.tabs["roster"] = [list(r) for r in ROSTER_ROWS]
    # commit() writes output_<month>.json to the current directory — chdir to a scratch dir so
    # tests never leave artifact files behind in the real repo.
    monkeypatch.chdir(tmp_path)

    def fake_fetch_report(key):
        return fixture_reports.get(key, [])

    def fake_build_umap():
        # Real build_umap() maps ServiceTitan loginName/email -> display name. Tests use the
        # display name directly as "Completer" in leadRequestReport rows, so identity-map it.
        names = {"test tech one", "test tech two", "test comm tech", "test ch tech", "test office rep"}
        return {n: n.title().replace("Ch Tech", "CH Tech") for n in names}

    monkeypatch.setattr(pm, "fetch_report", fake_fetch_report)
    monkeypatch.setattr(pm, "build_umap", fake_build_umap)
    return fake_sheets


def run_month(month_label):
    """Runs the real compute()->preflight()->commit() pipeline for one month, exactly like the
    CLI entry point does, and returns the parsed result dict. Assumes mock_pipeline (or
    equivalent monkeypatching) is already active."""
    computed = pm.compute(month_label)
    pf = pm.preflight(computed)
    assert not pf["blocked"], pf["message"]
    pm.commit(computed)
    return computed["result"]
