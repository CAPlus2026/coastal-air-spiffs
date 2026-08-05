var SPREADSHEET_ID = '1E3p3clqh1svM_F8dOXAh7pbFUqQCUFRetyHeElpDXTI';

function getProp_(name) {
  return PropertiesService.getScriptProperties().getProperty(name);
}

// ── Access control ───────────────────────────────────────────────────────
// SHARED_KEY gates ordinary read/write verbs. Enforcement is opt-in via the
// ENFORCE_KEY script property so this ships without breaking existing callers
// (the live index.html and process_month.py) that don't send a key yet. Flip
// ENFORCE_KEY to "true" once both have been updated to send it.
function checkSharedKey_(params) {
  var required = getProp_('SHARED_KEY');
  if (!required) return true; // not configured yet — unchanged from before this change
  if (params.key === required) return true;
  return getProp_('ENFORCE_KEY') !== 'true'; // warn-only until enforcement is turned on
}

// OWNER_KEY gates destructive verbs added in later phases (starting a
// ServiceTitan run, editing the roster) — verbs that can move money or
// change who's eligible for it. Unlike SHARED_KEY there is no warn-only
// mode: once a verb calls this, it's a hard gate.
function checkOwnerKey_(params) {
  var required = getProp_('OWNER_KEY');
  if (!required) return false; // must be configured before any verb relies on this
  return params.ownerKey === required;
}

function ensureSheet_(ss, name, headers) {
  var sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    if (headers && headers.length) sheet.appendRow(headers);
  }
  return sheet;
}

function logAccess_(ss, params) {
  try {
    var sheet = ensureSheet_(ss, 'access_log', ['at', 'action', 'sheet', 'keyPresent']);
    sheet.appendRow([new Date().toISOString(), params.action || '', params.sheet || params.sheets || '', !!params.key]);
  } catch (e) {
    // logging must never break the real request
  }
}

function parseMaybeJson_(v) {
  return typeof v === 'string' ? JSON.parse(v) : v;
}

// ── Verb implementations ─────────────────────────────────────────────────
function actionGet_(ss, params) {
  var sheet = ensureSheet_(ss, params.sheet);
  var range = params.range ? sheet.getRange(params.range) : sheet.getDataRange();
  return {values: range.getValues()};
}

function actionAppend_(ss, params) {
  var sheet = ensureSheet_(ss, params.sheet);
  var values = parseMaybeJson_(params.values);
  sheet.appendRow(values);
  return {success: true};
}

function actionPut_(ss, params) {
  var sheet = ensureSheet_(ss, params.sheet);
  var values = parseMaybeJson_(params.values);
  var range = sheet.getRange(params.range);
  range.setValues(values);
  return {success: true};
}

// One round trip for several tabs at once, optionally filtered to rows whose
// first column equals `month`. Needed so index.html's boot path (~8s budget)
// can load the month baseline from the Sheet without 10+ separate requests.
function actionGetMulti_(ss, params) {
  var sheetNames = String(params.sheets || '').split(',').map(function (s) { return s.trim(); }).filter(Boolean);
  var month = params.month;
  var out = {};
  sheetNames.forEach(function (name) {
    var sheet = ensureSheet_(ss, name);
    var values = sheet.getDataRange().getValues();
    if (month && values.length) {
      var header = values[0];
      var rows = values.slice(1).filter(function (r) { return String(r[0]) === month; });
      out[name] = [header].concat(rows);
    } else {
      out[name] = values;
    }
  });
  return {tabs: out};
}

// Bulk write of a whole result table in one call — appending hundreds of rows
// one GET at a time (today's sAppend pattern) is far too slow for a runner
// writing a full month's results and burns execution quota for no reason.
//
// mode 'append'       — appends every row as-is.
// mode 'replaceMonth' — deletes existing rows whose first column equals
//                       `month`, then appends the new rows. This is how a
//                       re-run replaces a month's results without leaving
//                       stale rows from a prior run mixed in.
function actionWriteTable_(ss, params) {
  var name = params.sheet;
  var rows = parseMaybeJson_(params.rows) || [];
  var mode = params.mode || 'append';
  var headers = params.headers ? parseMaybeJson_(params.headers) : null;
  var sheet = ensureSheet_(ss, name, headers);

  if (mode === 'replaceMonth') {
    var month = params.month;
    var data = sheet.getDataRange().getValues();
    if (data.length > 1) {
      var header = data[0];
      var keep = data.slice(1).filter(function (r) { return String(r[0]) !== month; });
      sheet.clearContents();
      sheet.getRange(1, 1, 1, header.length).setValues([header]);
      if (keep.length) sheet.getRange(2, 1, keep.length, header.length).setValues(keep);
    }
  }

  if (rows.length) {
    var lastRow = sheet.getLastRow();
    sheet.getRange(lastRow + 1, 1, rows.length, rows[0].length).setValues(rows);
  }
  return {success: true, wrote: rows.length};
}
