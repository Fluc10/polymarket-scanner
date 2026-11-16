"""
Google-Sheets-Anbindung: schreibt Tabellen (Blaetter) komplett neu und formatiert sie.

Benoetigt:
  GOOGLE_SERVICE_ACCOUNT_JSON  (GitHub-Secret: kompletter Inhalt der JSON-Schluesseldatei)
  GOOGLE_SHEET_ID              (GitHub-Variable: ID aus dem Tabellen-Link)
Die Tabelle muss mit der E-Mail des Service-Accounts als "Bearbeiter" geteilt sein.

Eine Tabelle (Blatt) wird als dict beschrieben:
  title       Blattname
  header      Liste der Spaltenueberschriften (oder None)
  rows        Liste von Zeilen (Listen)
  widths      Spaltenbreiten in Zeichen
  link_col    Spaltenindex, der verlinkt wird (Text bleibt, Link aus links[i])
  links       Liste mit URL pro Zeile (oder "")
  num_cols    {Spaltenindex: Zahlenformat}
  posneg_cols Spalten: >0 gruen, <0 rot
  yesno_cols  Spalten: "ja" gruen, "nein" rot
  wrap_cols   Spalten mit Zeilenumbruch
  bold_rows   Zeilenindizes (0 = erste Datenzeile) fett und groesser
"""
import json
import os

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
CREDS = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

GREEN_HEAD = {"red": 0.04, "green": 0.37, "blue": 0.23}
WHITE = {"red": 1, "green": 1, "blue": 1}
LINK_BLUE = {"red": 0.04, "green": 0.37, "blue": 1}
GREEN = {"red": 0.85, "green": 0.95, "blue": 0.89}
RED = {"red": 0.97, "green": 0.84, "blue": 0.85}
DEFAULT_SHEETS = {"Sheet1", "Tabellenblatt1", "Tabellenblatt 1", "Blatt1", "Blatt 1"}


def enabled():
    return bool(SHEET_ID and CREDS)


def sheet_url():
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"


def _open():
    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(CREDS)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def _rng(sid, r0, r1, c0, c1):
    return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": c0, "endColumnIndex": c1}


def build_requests(t, sid, old_rules, index):
    """Formatierungs-Anfragen fuer ein Blatt (ohne Werte)."""
    hdr = 1 if t.get("header") else 0
    n = len(t["rows"]) + hdr
    ncols = max([len(t.get("header") or [])] + [len(r) for r in t["rows"]] + [1])
    req = []
    # alte bedingte Formatierungen entfernen (rueckwaerts, sonst verschieben sich Indizes)
    for i in reversed(range(old_rules)):
        req.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": i}})
    req.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "index": index,
                       "gridProperties": {"frozenRowCount": hdr}},
        "fields": "index,gridProperties.frozenRowCount"}})
    # Formatierung der Datenzeilen zuruecksetzen
    req.append({"repeatCell": {"range": {"sheetId": sid},
                               "cell": {"userEnteredFormat": {}},
                               "fields": "userEnteredFormat"}})
    for i, w in enumerate(t.get("widths") or []):
        req.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": int(w * 7 + 10)}, "fields": "pixelSize"}})
    if hdr:
        req.append({"repeatCell": {
            "range": _rng(sid, 0, 1, 0, ncols),
            "cell": {"userEnteredFormat": {
                "backgroundColor": GREEN_HEAD, "wrapStrategy": "WRAP", "verticalAlignment": "TOP",
                "textFormat": {"bold": True, "foregroundColor": WHITE}}},
            "fields": "userEnteredFormat(backgroundColor,wrapStrategy,verticalAlignment,textFormat)"}})
        req.append({"setBasicFilter": {"filter": {"range": _rng(sid, 0, n, 0, ncols)}}})
    for c, pattern in (t.get("num_cols") or {}).items():
        req.append({"repeatCell": {
            "range": _rng(sid, hdr, n, c, c + 1),
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat"}})
    for c in t.get("wrap_cols") or []:
        req.append({"repeatCell": {
            "range": _rng(sid, hdr, n, c, c + 1),
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})
    for r in t.get("bold_rows") or []:
        req.append({"repeatCell": {
            "range": _rng(sid, hdr + r, hdr + r + 1, 0, ncols),
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 12}}},
            "fields": "userEnteredFormat.textFormat"}})
    if n > hdr:
        for c in t.get("posneg_cols") or []:
            for typ, color in (("NUMBER_GREATER", GREEN), ("NUMBER_LESS", RED)):
                req.append({"addConditionalFormatRule": {"index": 0, "rule": {
                    "ranges": [_rng(sid, hdr, n, c, c + 1)],
                    "booleanRule": {"condition": {"type": typ, "values": [{"userEnteredValue": "0"}]},
                                    "format": {"backgroundColor": color}}}}})
        for c in t.get("yesno_cols") or []:
            for val, color in (("ja", GREEN), ("nein", RED)):
                req.append({"addConditionalFormatRule": {"index": 0, "rule": {
                    "ranges": [_rng(sid, hdr, n, c, c + 1)],
                    "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": val}]},
                                    "format": {"backgroundColor": color}}}}})
    lc, links = t.get("link_col"), t.get("links") or []
    if lc is not None and any(links):
        cells = []
        for i, row in enumerate(t["rows"]):
            text = "" if lc >= len(row) or row[lc] is None else str(row[lc])
            url = links[i] if i < len(links) else ""
            cell = {"userEnteredValue": {"stringValue": text}}
            if url:
                cell["userEnteredFormat"] = {"textFormat": {
                    "link": {"uri": url}, "foregroundColor": LINK_BLUE, "underline": True}}
            cells.append({"values": [cell]})
        req.append({"updateCells": {"range": _rng(sid, hdr, n, lc, lc + 1), "rows": cells,
                                    "fields": "userEnteredValue,userEnteredFormat.textFormat"}})
    return req


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and v != v:  # NaN
        return ""
    return v


def write_tables(tables):
    """Schreibt alle Tabellen ins Google Sheet (ueberschreibt die Blaetter komplett)."""
    sh = _open()
    meta = sh.fetch_sheet_metadata({"fields": "sheets(properties(sheetId,title),conditionalFormats)"})
    existing = {s["properties"]["title"]: s for s in meta.get("sheets", [])}
    requests = []
    for idx, t in enumerate(tables):
        ncols = max([len(t.get("header") or [])] + [len(r) for r in t["rows"]] + [1])
        nrows = len(t["rows"]) + (1 if t.get("header") else 0)
        if t["title"] in existing:
            ws = sh.worksheet(t["title"])
            old = len(existing[t["title"]].get("conditionalFormats") or [])
            if ws.row_count < nrows + 10 or ws.col_count < ncols:
                ws.resize(rows=max(ws.row_count, nrows + 50), cols=max(ws.col_count, ncols))
        else:
            ws = sh.add_worksheet(title=t["title"], rows=max(100, nrows + 50), cols=max(26, ncols))
            old = 0
        ws.clear()
        data = ([t["header"]] if t.get("header") else []) + t["rows"]
        data = [[_clean(v) for v in row] for row in data]
        if data:
            ws.update(range_name="A1", values=data, value_input_option="RAW")
        requests += build_requests(t, ws.id, old, idx)
    wanted = {t["title"] for t in tables}
    for title, s in existing.items():
        if title in DEFAULT_SHEETS and title not in wanted:
            requests.append({"deleteSheet": {"sheetId": s["properties"]["sheetId"]}})
    if requests:
        sh.batch_update({"requests": requests})
