function doGet(e) {
  return handleRequest(e);
}

function doPost(e) {
  return handleRequest(e);
}

function handleRequest(e) {
  var output = ContentService.createTextOutput();
  output.setMimeType(ContentService.MimeType.JSON);
  
  try {
    var ss = SpreadsheetApp.openById('1E3p3clqh1svM_F8dOXAh7pbFUqQCUFRetyHeElpDXTI');
    var params = e.parameter;
    var action = params.action;
    
    if (action === 'get') {
      var sheet = ss.getSheetByName(params.sheet);
      var range = params.range ? sheet.getRange(params.range) : sheet.getDataRange();
      output.setContent(JSON.stringify({values: range.getValues()}));
    } else if (action === 'append') {
      var sheet = ss.getSheetByName(params.sheet);
      var values = JSON.parse(params.values);
      sheet.appendRow(values);
      output.setContent(JSON.stringify({success: true}));
    } else if (action === 'put') {
      var sheet = ss.getSheetByName(params.sheet);
      var values = JSON.parse(params.values);
      var range = sheet.getRange(params.range);
      range.setValues(values);
      output.setContent(JSON.stringify({success: true}));
    }
  } catch(err) {
    output.setContent(JSON.stringify({error: err.toString()}));
  }
  
  return output;
}