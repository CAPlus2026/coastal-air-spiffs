function doGet(e) {
  return handleRequest(e);
}

function doPost(e) {
  return handleRequest(e);
}

function mergeParams_(e) {
  var params = {};
  if (e && e.parameter) {
    for (var k in e.parameter) params[k] = e.parameter[k];
  }
  // A JSON POST body lets the runner send a whole result table without the
  // URL-length limits a query string would hit (see actionWriteTable_).
  if (e && e.postData && e.postData.type === 'application/json' && e.postData.contents) {
    try {
      var body = JSON.parse(e.postData.contents);
      for (var k2 in body) params[k2] = body[k2];
    } catch (err) {
      // malformed body — fall back to whatever query params we have
    }
  }
  return params;
}

function handleRequest(e) {
  var output = ContentService.createTextOutput();
  output.setMimeType(ContentService.MimeType.JSON);
  var params = mergeParams_(e);

  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    logAccess_(ss, params);

    if (!checkSharedKey_(params)) {
      output.setContent(JSON.stringify({error: 'invalid or missing key'}));
      return output;
    }

    var action = params.action;
    var result;
    if (action === 'get') result = actionGet_(ss, params);
    else if (action === 'append') result = actionAppend_(ss, params);
    else if (action === 'put') result = actionPut_(ss, params);
    else if (action === 'getMulti') result = actionGetMulti_(ss, params);
    else if (action === 'writeTable') result = actionWriteTable_(ss, params);
    else result = {error: 'unknown action: ' + action};

    output.setContent(JSON.stringify(result));
  } catch (err) {
    output.setContent(JSON.stringify({error: err.toString()}));
  }

  return output;
}
