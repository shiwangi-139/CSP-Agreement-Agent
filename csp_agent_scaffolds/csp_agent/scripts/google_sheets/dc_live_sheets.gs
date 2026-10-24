/**
 * DCs' live Google Sheets: every 2 hours (08:00-20:00 IST) each DC's sheet is
 * rewritten with that DC's latest CSP table from the CSP Document Agent (the
 * same content and colours as "Download DC Excel"). The sheets' published
 * links then show it to the DCs by themselves.
 *
 * One-time setup (in ANY one of your Google Sheets):
 *   1. Extensions -> Apps Script. Delete what is there, paste this whole file.
 *   2. Fill KEY (dashboard -> Form uploads -> "DCs' live Google Sheets" -> Copy
 *      key) and SHEETS (each DC's name exactly as on the dashboard, and the
 *      address-bar link of that DC's sheet, the ".../edit..." one).
 *   3. Save. Choose "setup" in the function list, press Run, Allow access.
 *      That updates every sheet once now and every 2 hours from then on.
 * Only the FIRST tab of each listed sheet is rewritten; nothing is shared,
 * created or deleted. "View -> Executions" shows each run.
 */
const SERVER = "https://indev.eko.in/csp-agreement-agent/upload";
const KEY = "PASTE-THE-KEY-HERE";
const SHEETS = [
  // { dc: "Ajay", url: "https://docs.google.com/spreadsheets/d/XXXXXXXX/edit" },
  // { dc: "Anil Sharma", url: "https://docs.google.com/spreadsheets/d/XXXXXXXX/edit" },
];
const FROM_HOUR = 8, UNTIL_HOUR = 20;           // IST
const COLOURS = { green: "#E3F4EA", yellow: "#FDF1D6", red: "#FDE2E2" };

function setup() {
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === "updateAll")
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger("updateAll").timeBased().everyHours(2).create();
  updateAllNow();
}

function updateAll() {                           // the 2-hour trigger
  const hour = Number(Utilities.formatDate(new Date(), "Asia/Kolkata", "H"));
  if (hour < FROM_HOUR || hour > UNTIL_HOUR) return;
  updateAllNow();
}

function updateAllNow() {                        // run by hand any time
  const done = [], failed = [];
  SHEETS.forEach(s => {
    try { updateOne(s); done.push(s.dc); }
    catch (e) { failed.push(s.dc + ": " + e.message); }
  });
  console.log("updated: " + (done.join(", ") || "none") + (failed.length ? " | FAILED: " + failed.join(" | ") : ""));
  if (failed.length && !done.length) throw new Error(failed.join(" | "));
}

function updateOne(s) {
  const r = UrlFetchApp.fetch(SERVER, { method: "post", muteHttpExceptions: true,
    payload: { action: "dc_sheet", key: KEY, dc_name: s.dc } });
  const d = JSON.parse(r.getContentText() || "{}");
  if (r.getResponseCode() !== 200 || !d.ok) throw new Error(d.error || ("server answered " + r.getResponseCode()));
  const sh = SpreadsheetApp.openByUrl(s.url).getSheets()[0];
  const width = d.header.length;
  const pad = row => row.concat(Array(width - row.length).fill(""));
  const values = [pad([d.title]), pad(["Last updated: " + d.updated + " · " + d.summary]), pad([d.legend]), pad([]),
                  d.header].concat(d.rows.map(row => row.map(v => v === null ? "" : String(v))));
  sh.clear();
  sh.getRange(1, 1, values.length, width).setValues(values).setWrap(true).setVerticalAlignment("top");
  sh.getRange(1, 1).setFontSize(14).setFontWeight("bold");
  sh.getRange(5, 1, 1, width).setFontWeight("bold").setFontColor("#FFFFFF").setBackground("#0092CC");
  if (d.rows.length) {
    sh.getRange(6, 1, d.rows.length, width)
      .setBackgrounds(d.fills.map(row => row.map(c => (c && COLOURS[c]) || null)));
    sh.getRange(6, 5, d.rows.length, 1).setFontWeight("bold");
  }
  sh.setFrozenRows(5);
  [11, 26, 13, 14, 34, 34, 34, 34, 14].forEach((w, i) => sh.setColumnWidth(i + 1, w * 7));
}
