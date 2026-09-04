// JS-side replay tests — three of this project's four historical real incidents (manual-add
// zero-vals, the ISO-date-mangling bug, unpersisted flag lines) lived entirely in index.html's
// loadSheets()/buildRows() replay layer. A Python-only test suite would not have caught any of
// them, so this file exercises the actual extracted <script> block under a stubbed DOM, the same
// way run_requested_month.py's validate() step does for syntax checking.
//
// Run: node --test tests/js/replay.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INDEX_HTML = path.join(__dirname, '..', '..', 'index.html');

function makeSandbox() {
  const elements = {};
  const makeEl = (id) => {
    if (!elements[id]) {
      elements[id] = {
        id, value: '', textContent: '', innerHTML: '', className: '', style: {}, disabled: false,
        classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
        addEventListener(){}, appendChild(){}, scrollIntoView(){},
      };
    }
    return elements[id];
  };
  const sandbox = {
    console,
    document: {
      getElementById: (id) => makeEl(id),
      querySelector: (sel) => (sel === '.view' ? makeEl('__view__') : null),
      querySelectorAll: () => ({ forEach(){} }),
      createElement: () => makeEl('__created__' + Math.random()),
    },
    window: { print(){} },
    fetch: async () => ({ json: async () => ({ values: [], tabs: {} }) }),
    URLSearchParams, AbortController, setTimeout, clearTimeout,
    setInterval: () => 0, clearInterval(){},
    Intl, Date, Math, JSON, prompt: () => null, confirm: () => true, alert(){},
  };
  sandbox.globalThis = sandbox;
  return { sandbox, elements };
}

function loadApp() {
  const html = readFileSync(INDEX_HTML, 'utf-8');
  const m = html.match(/<script>([\s\S]*)<\/script>/);
  if (!m) throw new Error('no <script> block found in index.html');
  const { sandbox, elements } = makeSandbox();
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox, { filename: 'index.html-script' });
  // Top-level const/let bindings aren't own properties of the vm context (function declarations
  // and var are) — pull out what tests need explicitly.
  const S = vm.runInContext('S', sandbox);
  sandbox.MONTH = vm.runInContext('MONTH', sandbox);
  sandbox.ROSTER = vm.runInContext('ROSTER', sandbox);
  return { sandbox, elements, S };
}

// ── Incident: manual-add zero-vals bug (fixed 2026-08-07) ────────────────────────────────────
test('a manual add contributes its dollar amount to buildRows()', () => {
  const { sandbox, S } = loadApp();
  // buildRows()'s steven/caleb loop only considers manual adds for names already present in
  // S.emps[mgr] (a brand-new name with no other spiff activity isn't picked up there at all —
  // that's what S.manuals.jenny's separate non-technician path exists for). Use a real existing
  // employee, matching the actual historical incident (Jay Hall's manual add being zeroed out).
  const existing = S.emps.steven[0];
  assert.ok(existing, 'fixture has no steven employees to test against');
  const before = sandbox.buildRows().find(r => r.name === existing.name)?.total || 0;
  S.manuals.steven.push({ id: 'ma_test', name: existing.name, reason: 'test', ref: '',
                          amount: 123, dept: 'MB Residential Service' });
  const after = sandbox.buildRows().find(r => r.name === existing.name)?.total || 0;
  assert.equal(after - before, 123, 'manual add did not contribute its dollar amount to the total');
});

// ── Incident: flagSpiffLine never persisted (fixed 2026-07-13) ───────────────────────────────
test('flagging a spiff line deducts from the employee total', () => {
  const { sandbox, S } = loadApp();
  const mgr = 'steven';
  const empName = Object.keys(S.spiffDetail[mgr] || {})[0];
  assert.ok(empName, 'fixture has no spiffDetail to test against');
  const line = S.spiffDetail[mgr][empName][0];
  const emp = S.emps[mgr].find(e => e.name === empName);
  const col = sandbox.spiffFieldFor(mgr, empName, line.type);
  const before = emp[col] || 0;

  // flagSpiffLine reads a reason from a DOM input by id — seed it via the sandbox's document.
  const reasonId = `flag-reason-${mgr}-${empName.replace(/\s/g, '_')}-0`;
  const reasonEl = sandbox.document.getElementById(reasonId);
  reasonEl.value = 'test reason';

  return sandbox.flagSpiffLine(mgr, empName, 0).then(() => {
    assert.equal(emp[col], Math.max(0, before - line.spiff),
      'flagged line did not deduct from the employee total');
    assert.equal(line.flagged, true);
  });
});

// ── Incident: ISO-datetime month cells silently broke every replay check (fixed 2026-09-02) ──
test('normMonth treats an ISO datetime and a plain month string identically', () => {
  const { sandbox } = loadApp();
  assert.equal(sandbox.normMonth('Aug 2026'), 'Aug 2026');
  assert.equal(sandbox.normMonth('2026-08-01T04:00:00.000Z'), 'Aug 2026');
});

// ── Carry-forward payout re-application on reload ────────────────────────────────────────────
test('marking a carry-forward item paid adds its amount to the employee total', () => {
  const { sandbox, S } = loadApp();
  // Not every carry-forward item's employee is guaranteed to also have a base S.emps entry (an
  // employee with zero OTHER spiff activity this month besides one pending carry-forward won't
  // be in S.emps at all) — pick one that does, since that's what this test is actually about.
  let cf, mgr, emp;
  for (const c of S.carryForward) {
    if (c.resolved) continue;
    const sEmp = S.emps.steven.find(e => e.name === c.emp);
    const cEmp = S.emps.caleb.find(e => e.name === c.emp);
    if (sEmp) { cf = c; mgr = 'steven'; emp = sEmp; break; }
    if (cEmp) { cf = c; mgr = 'caleb'; emp = cEmp; break; }
  }
  assert.ok(cf, 'fixture has no unresolved carry-forward item whose employee also has a base S.emps entry');
  const col = sandbox.deptToCol(cf.dept);
  const before = emp[col] || 0;
  sandbox.payOutCarryForward(mgr, cf);
  assert.equal(emp[col], before + cf.amount, 'payOutCarryForward did not add the amount to the employee total');
});

// ── Bug found 2026-09-04 via this test file: an employee whose ONLY current-month activity is a
// pending carry-forward has no S.emps entry (that list only ever gets populated from MPF/
// accessory/membership lines) — payOutCarryForward's old `if(!emp) return` meant marking their
// item "paid" flipped the UI badge to "✓ Paid" while silently adding $0. Confirmed live: Jim
// LeBlanc currently has two real $75 pending items and zero other spiff activity this month. ──
test('paying a carry-forward item for an employee with no prior S.emps entry still adds the money', () => {
  const { sandbox, S } = loadApp();
  const cf = { id: 'cf_test_new_emp', fromMonth: sandbox.MONTH, emp: 'Zzz No Prior Activity',
               ref: '', type: 'test', amount: 75, dept: 'MB Install Residential',
               reason: '', resolved: false, disposition: '', note: '' };
  assert.ok(!S.emps.steven.some(e => e.name === cf.emp), 'test setup: employee should not pre-exist');
  sandbox.payOutCarryForward('steven', cf);
  const emp = S.emps.steven.find(e => e.name === cf.emp);
  assert.ok(emp, 'payOutCarryForward did not create an S.emps entry for a previously-unseen employee');
  assert.equal(emp.ins, 75, 'amount was not added for the newly-created employee');
});

// ── Same root cause, the commercial-lead side: mgrForEmployee() only checked S.emps/officeMems,
// so a tech whose only current-month event is a newly-sold lead resolved to no manager at all
// and updateCommLead()'s payout call never fired. Confirmed live: Kyle Freeman and Javi Vazquez
// (real, active commercial techs) both have zero S.emps entries this month despite having
// pending commercial leads. ──
test('mgrForEmployee falls back to the roster for someone with no current-month S.emps entry', () => {
  const { sandbox, S } = loadApp();
  const rosterOnly = (S.commLeads.map(l => l.tech))
    .find(name => !S.emps.steven.some(e => e.name === name) && !S.emps.caleb.some(e => e.name === name));
  if (!rosterOnly) return; // nothing to test against in this month's data — not a failure
  // ROSTER.rows only gets populated by loadSheets()'s real Sheet fetch on app boot (this test
  // harness never calls loadSheets(), same as every other test here) — seed a matching roster
  // row directly, the same shape loadRosterFrom() produces.
  sandbox.ROSTER.rows.push({ name: rosterOnly, team: 'steven', role: 'comm_tech', eligible: true, active: true });
  assert.notEqual(sandbox.mgrForEmployee(rosterOnly), null,
    `mgrForEmployee returned null for ${rosterOnly} even with a matching roster row present`);
});

// ── Known open gap: commlead_updates create-if-missing can fabricate a paid phantom lead ─────
// Documented and worked around by hand (2026-09-03/04), not yet fixed at the code level — see
// the reliability plan's Phase 2 (pre-flight gate, phantom-lead check). This is intentionally a
// TODO test: it records the current (unwanted) behavior so Phase 2's fix has a concrete
// acceptance test to flip from failing to passing, rather than the gap silently staying
// undocumented in test form.
test.todo('an orphaned commlead_updates row with a terminal status does not fabricate a paid lead — needs Phase 2 phantom-lead check');
