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
  // Tolerance, not equality: sums of cents-valued floats drift by ~1e-14 depending on the month's data.
  assert.ok(Math.abs((after - before) - 123) < 0.005, `manual add changed the total by ${after - before}, expected 123`);
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

// ── Incident: an approved bonus was double-counted in every total (found 2026-09-04) ─────────
// buildRows()'s "Approved bonuses" block does `empMap[b.name].total+=b.amount` — r.total already
// includes the bonus. Several render functions then ALSO added r.bonusAmt on top when computing
// a displayed total (teamTotal, grandTotal(), rOutput()'s rowTotal), silently doubling every
// approved bonus. Confirmed live: Rich Smith's real $408.07 commercial commission displayed and
// totaled as $816.14 the first time it was approved.
test('an approved bonus contributes its amount exactly once to buildRows() and grandTotal()', () => {
  const { sandbox, S } = loadApp();
  const bonus = S.bonuses.find(b => !b.approved) || S.bonuses[0];
  assert.ok(bonus, 'fixture has no bonuses to test against');
  const before = sandbox.grandTotal();
  bonus.approved = true;
  const row = sandbox.buildRows().find(r => r.name === bonus.name);
  assert.ok(row, 'approved bonus did not produce a row in buildRows()');
  assert.equal(row.bonusAmt, bonus.amount);
  const after = sandbox.grandTotal();
  assert.ok(Math.abs((after - before) - bonus.amount) < 0.005,
    `approving a $${bonus.amount} bonus changed grandTotal() by $${after - before} — should be exactly once`);
});

// ── Known open gap: commlead_updates create-if-missing can fabricate a paid phantom lead ─────
// Documented and worked around by hand (2026-09-03/04), not yet fixed at the code level — see
// the reliability plan's Phase 2 (pre-flight gate, phantom-lead check). This is intentionally a
// TODO test: it records the current (unwanted) behavior so Phase 2's fix has a concrete
// acceptance test to flip from failing to passing, rather than the gap silently staying
// undocumented in test form.
test.todo('an orphaned commlead_updates row with a terminal status does not fabricate a paid lead — needs Phase 2 phantom-lead check');

// ── Incident: totals climbed on their own while a run was queued (found 2026-10-02) ──────────
// startRunStatusPolling() calls loadSheets() every 30s, and the roster edit buttons call it
// again too — but loadSheets() REPLAYS the Sheet logs on top of whatever S already holds (it
// was only ever designed to run once, on a pristine page load). Every extra call re-pushed every
// manual add (e.g. Jenny's $770 CCS Bonus) and re-applied every Sold & Completed commercial lead
// payout, so "Est. total spiffs" ratcheted upward every 30 seconds ($12k -> $30k in a few
// minutes) while no data had changed anywhere.
test('calling loadSheets() repeatedly does not inflate totals', async () => {
  const { sandbox, S } = loadApp();
  const month = sandbox.MONTH;
  const lead = S.commLeads[0];
  assert.ok(lead, 'fixture has no commercial leads to test against');
  const tabs = {
    manual_adds: [['month', 'mgr', 'employee', 'reason', 'amount', 'dept', 'by', 'at', 'status', 'id'],
      [month, 'jenny', 'Jenny Miller', 'CCS Bonus', 770, 'MB Residential Service', 'billy', '2026-09-04T00:00:00Z', 'added', 'ma_test_jenny'],
      [month, 'steven', S.emps.steven[0].name, 'Lead Stage 2', 75, 'MB Install Residential', 'steven', '2026-09-04T00:00:00Z', 'added', 'ma_test_steven']],
    commlead_updates: [['month', 'id', 'tech', 'customer', 'job', 'status', 'spiff', 'payMonth', 'ts'],
      [month, lead.id, lead.tech, lead.customer, lead.job, 'Sold & Completed', 100, month, '2026-09-04T00:00:00Z', '', '', '']],
  };
  sandbox.fetch = async (url) => {
    const u = String(url);
    return { json: async () => (u.includes('action=getMulti') ? { tabs } : { values: [] }) };
  };
  await sandbox.loadSheets();
  const once = sandbox.grandTotal();
  const manualsOnce = JSON.stringify(S.manuals);
  await sandbox.loadSheets();
  await sandbox.loadSheets();
  assert.equal(sandbox.grandTotal(), once, 'grandTotal() changed on a repeat loadSheets() with identical Sheet data');
  assert.equal(JSON.stringify(S.manuals), manualsOnce, 'S.manuals gained duplicate entries on a repeat loadSheets()');
  assert.ok(once > 0);
});

test('concurrent loadSheets() calls do not interleave their replays', async () => {
  const { sandbox, S } = loadApp();
  const month = sandbox.MONTH;
  const tabs = {
    manual_adds: [['h'], [month, 'jenny', 'Jenny Miller', 'CCS Bonus', 770, 'MB Residential Service', 'billy', 'x', 'added', 'ma_conc']],
  };
  sandbox.fetch = async (url) => {
    const u = String(url);
    return { json: async () => (u.includes('action=getMulti') ? { tabs } : { values: [] }) };
  };
  await sandbox.loadSheets();
  const once = JSON.stringify(S.manuals);
  await Promise.all([sandbox.loadSheets(), sandbox.loadSheets(), sandbox.loadSheets()]);
  assert.equal(JSON.stringify(S.manuals), once);
});

test('refreshRunStatus() updates the run status without touching payout totals', async () => {
  const { sandbox, S } = loadApp();
  const before = sandbox.grandTotal();
  sandbox.fetch = async () => ({ json: async () => ({ tabs: { run_requests: [
    ['month', 'status', 'by', 'at', 'msg'],
    ['2026-09-01T04:00:00.000Z', 'running', 'system', '2026-10-02T18:00:00Z', 'Started by GitHub Actions'] ] } }) });
  await sandbox.refreshRunStatus();
  assert.equal(S.runStatus.month, 'Sep 2026');
  assert.equal(S.runStatus.status, 'running');
  assert.equal(sandbox.grandTotal(), before);
});

// ── Lead Stage 2 evidence (carry_forward_evidence.py -> cf_evidence tab) ─────────────────────
test('cf_evidence rows load by item id, drive the badge, and never change totals', async () => {
  const { sandbox, S } = loadApp();
  const cf = S.carryForward.find(c => !c.resolved);
  assert.ok(cf, 'fixture has no pending carry-forward item');
  const before = sandbox.grandTotal();
  const tabs = { cf_evidence: [['month', 'id', 'emp', 'verdict', 'summary', 'jobs', 'checkedAt'],
    [sandbox.MONTH, cf.id, cf.emp, 'install_completed', 'PAY Stage 2 — installation job 777 (Installation) completed 2026-09-12.', '[]', 'x']] };
  sandbox.fetch = async (url) => ({ json: async () => (String(url).includes('action=getMulti') ? { tabs } : { values: [] }) });
  await sandbox.loadSheets();
  await sandbox.loadSheets(); // idempotent, same as every other replay
  assert.equal(S.cfEvidence[cf.id].verdict, 'install_completed');
  assert.match(sandbox.evidenceBadge(cf), /PAY Stage 2/);
  assert.match(sandbox.rEvidenceCard(), /Lead Stage 2/);
  assert.ok(!sandbox.rEvidenceCard().includes(cf.emp), 'the Overview is counts only — the work is on the managers pages');
  assert.equal(sandbox.grandTotal(), before, 'evidence alone must never move money');
});

test('no cf_evidence rows means no Overview card and no badge', async () => {
  const { sandbox, S } = loadApp();
  sandbox.fetch = async () => ({ json: async () => ({ tabs: {}, values: [] }) });
  await sandbox.loadSheets();
  assert.equal(sandbox.rEvidenceCard(), '');
  assert.equal(sandbox.evidenceBadge(S.carryForward[0]), '');
});

test('cf_evidence loads every data row even when the tab has no header row', async () => {
  const { sandbox, S } = loadApp();
  const [a, b] = S.carryForward.filter(c => !c.resolved);
  const row = (c) => [sandbox.MONTH, c.id, c.emp, 'no_install_found', 'x', '[]', 'y'];
  sandbox.fetch = async (url) => ({ json: async () => (String(url).includes('action=getMulti')
    ? { tabs: { cf_evidence: [row(a), row(b)] } } : { values: [] }) });
  await sandbox.loadSheets();
  assert.ok(S.cfEvidence[a.id], 'first row was dropped as if it were a header');
  assert.ok(S.cfEvidence[b.id]);
});

// ── Bulk actions on the Lead Stage 2 evidence ────────────────────────────────────────────────
async function bulkSetup() {
  const { sandbox, S } = loadApp();
  // loadApp() leaves the app's own boot-time loadSheets() in flight; it finishes by resetting S to
  // the baseline, which would wipe what a test sets up. Let it (and a second, serialized one) settle first.
  await sandbox.loadSheets();
  const mine = S.carryForward.filter(c => !c.resolved && S.emps.steven.some(e => e.name === c.emp));
  assert.ok(mine.length >= 5, `fixture needs >=5 pending steven items, has ${mine.length}`);
  const [a, b, dep, mpf, wait] = mine;
  const ev = (verdict, summary = 'x') => ({ emp: '', verdict, summary, checkedAt: '' });
  S.cfEvidence = {
    [a.id]: ev('install_completed', 'PAY Stage 2 — installation job 1 (Installation) completed 2026-09-01.'),
    [b.id]: ev('install_completed', 'PAY Stage 2 — installation job 2 (Installation) completed 2026-09-02.'),
    [dep.id]: ev('departed', 'Gone is no longer employed — don\'t pay.'),
    [mpf.id]: ev('paid_on_mpf', 'Master Pay File shows Stage 2 already.'),
    [wait.id]: ev('no_install_found', 'No estimate or installation job yet.'),
  };
  const writes = [];
  sandbox.fetch = async (url) => {
    const u = new URL(String(url), 'http://x');
    if (u.searchParams.get('action') === 'append') writes.push([u.searchParams.get('sheet'), JSON.parse(u.searchParams.get('values'))]);
    return { json: async () => ({ values: [], tabs: {} }) };
  };
  return { sandbox, S, a, b, dep, mpf, wait, writes };
}

test('bulk pay adds exactly the recommended leads once and logs one paid row each', async () => {
  const { sandbox, S, a, b, dep, mpf, wait, writes } = await bulkSetup();
  const before = sandbox.grandTotal();
  await sandbox.bulkPayCarryForward('steven');
  assert.ok(Math.abs(sandbox.grandTotal() - before - (a.amount + b.amount)) < 0.005);
  assert.deepEqual(writes.map(w => [w[0], w[1][2], w[1][7]]),
    [['carry_forward_resolutions', a.id, 'paid'], ['carry_forward_resolutions', b.id, 'paid']]);
  assert.ok(a.resolved && b.resolved && !dep.resolved && !mpf.resolved && !wait.resolved);
  assert.match(a.note, /^Install completed — installation job 1/);
  await sandbox.bulkPayCarryForward('steven'); // nothing left to pay -> no-op, no double payment
  assert.equal(writes.length, 2);
});

test('bulk clear marks departed / already-paid leads dead and never pays them', async () => {
  const { sandbox, dep, mpf, wait, writes } = await bulkSetup();
  const before = sandbox.grandTotal();
  await sandbox.bulkClearCarryForward('steven');
  assert.equal(sandbox.grandTotal(), before, 'clearing must not move money');
  assert.deepEqual(writes.map(w => [w[1][2], w[1][7]]), [[dep.id, 'dead'], [mpf.id, 'dead']]);
  assert.ok(dep.resolved && mpf.resolved && !wait.resolved);
});

test('bulk review only touches leads that are not ready, and never resolves them', async () => {
  const { sandbox, wait, a, writes } = await bulkSetup();
  await sandbox.bulkReviewCarryForward('steven');
  assert.deepEqual(writes.map(w => [w[1][2], w[1][7]]), [[wait.id, 'reviewed']]);
  assert.ok(wait.reviewedThisMonth && !wait.resolved && !a.resolved);
});

test('a cancelled confirm changes nothing', async () => {
  const { sandbox, a, writes } = await bulkSetup();
  sandbox.confirm = () => false;
  const before = sandbox.grandTotal();
  await sandbox.bulkPayCarryForward('steven');
  assert.equal(writes.length, 0); assert.ok(!a.resolved); assert.equal(sandbox.grandTotal(), before);
});

test('the ready-to-apply panel only appears for conclusive leads nobody has applied yet', async () => {
  const { sandbox } = await bulkSetup();
  const panel = sandbox.cfActionPanel('steven');
  assert.match(panel, /PAY Stage 2 — 2 installed/);
  assert.match(panel, /bulkPayCarryForward\('steven'\)/);
  assert.match(panel, /Clear without paying — 2/);
  assert.equal(sandbox.cfActionPanel('caleb'), '', "another manager sees nothing for steven's team");
  assert.match(sandbox.cfTeamBanner('steven'), /need/);
});

// ── Auto-verified payouts (carry_forward_evidence.apply_resolutions) flow into the pay numbers ──
async function autoSetup() {
  const { sandbox, S, elements } = loadApp();
  await sandbox.loadSheets();
  const mine = S.carryForward.filter(c => !c.resolved && S.emps.steven.some(e => e.name === c.emp));
  const [a, b, wait] = mine;
  const res = (c, disp, note) => [sandbox.MONTH, 'steven', c.id, c.emp, c.ref, c.type, c.amount, disp, note, 'ts'];
  const tabs = {
    carry_forward_resolutions: [['month', 'mgr', 'id', 'emp', 'ref', 'type', 'amount', 'disposition', 'note', 'ts'],
      res(a, 'paid', 'Auto-verified from ServiceTitan project — install 1 completed 2026-09-01.')],
    cf_evidence: [['month', 'id', 'emp', 'verdict', 'summary', 'jobs', 'checkedAt'],
      [sandbox.MONTH, a.id, a.emp, 'install_completed', 'PAY Stage 2 — x', '[]', 't'],
      [sandbox.MONTH, wait.id, wait.emp, 'no_install_found', "Don't pay Stage 2 yet — nothing yet.", '[]', 't']],
  };
  sandbox.fetch = async (url) => ({ json: async () => (String(url).includes('action=getMulti') ? { tabs } : { values: [] }) });
  return { sandbox, S, elements, a, b, wait };
}

test('an auto-verified payout is counted in the pay numbers exactly once, however many times it reloads', async () => {
  const { sandbox, S, a } = await autoSetup();
  const base = sandbox.grandTotal();
  await sandbox.loadSheets();
  const once = sandbox.grandTotal();
  await sandbox.loadSheets(); await sandbox.loadSheets();
  assert.ok(Math.abs(once - base - 75) < 0.005, `expected +$75, got ${once - base}`);
  assert.equal(sandbox.grandTotal(), once);
  const live = S.carryForward.find(c => c.id === a.id);
  assert.ok(live.resolved && live.disposition === 'paid' && live.auto === true);
});

test("a manager's Undo of an auto-verified payout removes it from the pay numbers", async () => {
  const { sandbox, S, a } = await autoSetup();
  await sandbox.loadSheets();
  const paid = sandbox.grandTotal();
  const live = S.carryForward.find(c => c.id === a.id);
  await sandbox.undoCarryForward('steven', live.id);
  assert.ok(Math.abs(paid - sandbox.grandTotal() - 75) < 0.005);
});

test('the manager page groups leads: needs decision / verified & handled / waiting', async () => {
  const { sandbox, S, elements, a, wait } = await autoSetup();
  await sandbox.loadSheets();
  sandbox.rFlags('steven');
  const html = elements['__view__'].innerHTML;
  assert.match(html, /1 verified &amp; included in pay \(\$75\.00\)/);
  assert.match(html, /Verified &amp; handled \(1\)/);
  assert.match(html, /Waiting on a sale or install \(1\) — nothing to do/);
  assert.match(html, /Needs your decision \(\d+\)/);
  const g = sandbox.cfGroups(S.carryForward.filter(c => S.emps.steven.some(e => e.name === c.emp)));
  assert.equal(g.paid.length, 1);
  assert.ok(g.waiting.some(c => c.id === wait.id) && !g.decide.some(c => c.id === wait.id));
  assert.ok(!g.decide.some(c => c.id === a.id));
});

test("departed-employee clearances show on Billy's Overview as cleared, not as work", async () => {
  const { sandbox, S, a } = await autoSetup();
  await sandbox.loadSheets();
  const live = S.carryForward.find(c => c.id === a.id);
  live.disposition = 'dead'; live.note = 'Auto-cleared — x is no longer employed'; live.auto = true;
  assert.match(sandbox.rEvidenceCard(), /1 cleared/);
});
