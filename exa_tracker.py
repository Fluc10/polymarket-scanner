"""
Exa-Tracking fuer neue Polymarket-Maerkte.

Liest data/exa_log.csv (geschrieben von new_markets.py), holt die aktuellen
Preise aller protokollierten Maerkte, speichert einen Tages-Schnappschuss in
data/exa_snapshots.csv und aktualisiert das Google Sheet (Blaetter
"Märkte", "Verlauf", "Auswertung"). Ohne Google-Zugang wird stattdessen eine
Excel-Datei per Telegram geschickt.

Modi (Umgebungsvariable TRACKER_MODE):
  update   - taeglich: Sheet aktualisieren + kurze Zusammenfassung per Telegram
  monthly  - am 1. des Monats: zusaetzlich Feedback-Bericht fuer den Vormonat
sync_sheet() wird auch von new_markets.py aufgerufen (ohne Telegram, ohne Schnappschuss).
"""
import csv
import io
import json
import os
import time
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import requests

import gsheet

GAMMA = "https://gamma-api.polymarket.com"
LOG_FILE = os.path.join("data", "exa_log.csv")
SNAP_FILE = os.path.join("data", "exa_snapshots.csv")
SNAP_FIELDS = ["datum", "market_id", "prozent", "geschlossen", "ergebnis"]
TZ = ZoneInfo("Europe/Vienna")
SIGNAL_TEXT = {"unterbewertet": "unterbewertet", "ueberbewertet": "überbewertet",
               "fair": "fair", "unklar": "unklar"}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MODE = (os.environ.get("TRACKER_MODE") or "update").strip().lower()


def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Ab welchem Abstand (Prozentpunkte) zwischen Exa und Markt ein Signal als "Trade" zaehlt
SIGNAL_MIN_DELTA = env_float("TRACKER_MIN_DELTA", 5)
STAKE = 10.0  # simulierter Einsatz pro Signal in $


# ---------------------------------------------------------------- Daten

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def get_json(url, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return None


def fetch_markets(ids):
    """Aktueller Stand aller Maerkte: {id: market}. Offene und geschlossene getrennt abfragen."""
    ids = sorted({str(i) for i in ids if i})
    found = {}
    for k in range(0, len(ids), 40):
        query = "&".join(f"id={i}" for i in ids[k:k + 40])
        for closed in ("true", "false"):
            for m in get_json(f"{GAMMA}/markets?{query}&closed={closed}&limit=100") or []:
                found[str(m.get("id"))] = m
    for i in ids:  # Fallback einzeln
        if i not in found:
            m = get_json(f"{GAMMA}/markets/{i}")
            if isinstance(m, dict) and m.get("id"):
                found[i] = m
    return found


def yes_price(m):
    try:
        return float(json.loads(m.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError, AttributeError):
        return None


def outcome_of(m):
    """1 = Antwort ist eingetreten, 0 = nicht eingetreten, None = noch offen."""
    if not m or not m.get("closed"):
        return None
    p = yes_price(m)
    if p is None:
        return None
    if p >= 0.98:
        return 1
    if p <= 0.02:
        return 0
    return None  # geschlossen, aber (noch) nicht eindeutig aufgeloest


def save_snapshot(markets, snaps, today):
    done = {(s["datum"], s["market_id"]) for s in snaps}
    new = []
    for mid, m in markets.items():
        p = yes_price(m)
        if p is None or (today, mid) in done:
            continue
        o = outcome_of(m)
        new.append({"datum": today, "market_id": mid, "prozent": round(p * 100, 1),
                    "geschlossen": "ja" if m.get("closed") else "nein",
                    "ergebnis": "" if o is None else o})
    if new:
        os.makedirs(os.path.dirname(SNAP_FILE), exist_ok=True)
        new_file = not os.path.exists(SNAP_FILE)
        with open(SNAP_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SNAP_FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(new)
    return snaps + new, len(new)


# ---------------------------------------------------------------- Auswertung

def brier(p, o):
    return (p / 100 - o) ** 2


def simulate(r, with_spread):
    """Simulierter Gewinn eines $10-Trades in Richtung Exa. None wenn kein Signal / offen."""
    d, mkt, o = r["delta"], r["markt"], r["ergebnis"]
    if d is None or o is None or mkt is None or abs(d) < SIGNAL_MIN_DELTA:
        return None
    half = (r["spread"] or 0) / 2 if with_spread else 0
    if d > 0:  # Exa hoeher -> Ja kaufen
        price = min(99.0, mkt + half) / 100
        return STAKE / price * o - STAKE
    price = min(99.0, (100 - mkt) + half) / 100  # Nein kaufen
    return STAKE / price * (1 - o) - STAKE


def build_rows(log, markets):
    rows = []
    for x in log:
        mid = str(x.get("market_id") or "")
        m = markets.get(mid)
        p = yes_price(m) if m else None
        now_p = None if p is None else p * 100
        mkt, exa, delta = num(x.get("markt_prozent")), num(x.get("exa_prozent")), num(x.get("delta_pp"))
        o = outcome_of(m)
        move = None if now_p is None or mkt is None else round(now_p - mkt, 1)
        toward = None
        if move is not None and delta not in (None, 0) and abs(move) >= 1:
            toward = (move > 0) == (delta > 0)
        exa_right = None
        if o is not None and exa is not None and mkt is not None:
            exa_right = abs(o * 100 - exa) < abs(o * 100 - mkt)
        r = {
            "erfasst": x.get("erfasst_am", ""), "art": x.get("art", ""),
            "event": x.get("event", ""), "slug": x.get("event_slug", ""),
            "antwort": x.get("antwort", ""), "ende": x.get("enddatum", ""),
            "markt": mkt, "exa": exa, "delta": delta,
            "signal": SIGNAL_TEXT.get(x.get("signal", ""), x.get("signal", "")),
            "spread": num(x.get("spread")), "jetzt": now_p, "bewegung": move,
            "richtung_exa": toward,
            "status": ("aufgelöst: JA" if o == 1 else "aufgelöst: NEIN" if o == 0
                       else "geschlossen" if m and m.get("closed") else "offen"),
            "ergebnis": o, "exa_besser": exa_right,
            "nachrichten": x.get("nachrichtenlage", ""), "begruendung": x.get("begruendung", ""),
            "quellen": x.get("quellen", ""), "market_id": mid,
        }
        r["pnl"] = simulate(r, False)
        r["pnl_spread"] = simulate(r, True)
        rows.append(r)
    return rows


def stats(rows):
    with_exa = [r for r in rows if r["exa"] is not None]
    res = [r for r in with_exa if r["ergebnis"] is not None and r["markt"] is not None]
    sig = [r for r in res if r["delta"] is not None and abs(r["delta"]) >= SIGNAL_MIN_DELTA]
    hits = [r for r in sig if (r["delta"] > 0) == (r["ergebnis"] == 1)]
    open_moves = [r for r in with_exa if r["richtung_exa"] is not None and r["status"] == "offen"]
    pnl = [r["pnl"] for r in rows if r["pnl"] is not None]
    pnl_s = [r["pnl_spread"] for r in rows if r["pnl_spread"] is not None]
    return {
        "maerkte": len(rows), "mit_exa": len(with_exa), "aufgeloest": len(res),
        "exa_naeher": sum(1 for r in res if r["exa_besser"]),
        "brier_exa": sum(brier(r["exa"], r["ergebnis"]) for r in res) / len(res) if res else None,
        "brier_markt": sum(brier(r["markt"], r["ergebnis"]) for r in res) / len(res) if res else None,
        "signale": len(sig), "treffer": len(hits),
        "pnl": sum(pnl) if pnl else 0.0, "pnl_spread": sum(pnl_s) if pnl_s else 0.0, "trades": len(pnl),
        "richtung_n": len(open_moves), "richtung_ok": sum(1 for r in open_moves if r["richtung_exa"]),
    }


def calibration(rows):
    res = [r for r in rows if r["exa"] is not None and r["ergebnis"] is not None]
    out = []
    for lo, hi in [(0, 10), (10, 30), (30, 50), (50, 70), (70, 90), (90, 101)]:
        b = [r for r in res if lo <= r["exa"] < hi]
        label = f"{lo}–{min(hi, 100)} %"
        if b:
            out.append([label, len(b), round(sum(r["exa"] for r in b) / len(b), 1),
                        round(100 * sum(r["ergebnis"] for r in b) / len(b), 1),
                        round(sum(r["markt"] for r in b) / len(b), 1)])
        else:
            out.append([label, 0, None, None, None])
    return out


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "–"


def months(now):
    cur = now.strftime("%Y-%m")
    prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return cur, prev


# ---------------------------------------------------------------- Tabellen

def yn(v):
    return "" if v is None else ("ja" if v else "nein")


def r1(v, d=1):
    return None if v is None else round(v, d)


def tables(rows, snaps, now):
    rows = sorted(rows, key=lambda r: r["erfasst"], reverse=True)
    links = [f"https://polymarket.com/event/{r['slug']}" if r["slug"] else "" for r in rows]

    maerkte = {
        "title": "Märkte",
        "header": ["Erfasst (UTC)", "Art", "Event", "Antwort", "Ende", "Markt bei Erfassung %", "Exa %",
                   "Delta (Exa−Markt) PP", "Signal", "Spread bei Erfassung ¢", "Markt jetzt %",
                   "Bewegung seit Erfassung PP", "Markt bewegt sich Richtung Exa?", "Status",
                   "Exa näher am Ergebnis?", "Sim. Gewinn $10 (ohne Spread)", "Sim. Gewinn $10 (mit Spread)",
                   "Nachrichtenlage (Exa)", "Begründung (Exa)", "Quellen"],
        "rows": [[r["erfasst"], r["art"], r["event"], r["antwort"], r["ende"], r["markt"], r["exa"],
                  r["delta"], r["signal"], r["spread"], r1(r["jetzt"]), r["bewegung"], yn(r["richtung_exa"]),
                  r["status"], yn(r["exa_besser"]), r1(r["pnl"], 2), r1(r["pnl_spread"], 2),
                  r["nachrichten"], r["begruendung"], r["quellen"]] for r in rows],
        "widths": [16, 10, 48, 22, 11, 11, 9, 11, 14, 10, 10, 12, 12, 15, 12, 12, 12, 60, 50, 40],
        "link_col": 2, "links": links,
        "num_cols": {5: "0.0", 6: "0.0", 7: "+0.0;-0.0;0.0", 9: "0.0", 10: "0.0", 11: "+0.0;-0.0;0.0",
                     15: '+0.00" $";-0.00" $";0.00" $"', 16: '+0.00" $";-0.00" $";0.00" $"'},
        "posneg_cols": [7, 11, 15, 16], "yesno_cols": [12, 14], "wrap_cols": [17, 18, 19],
    }

    dates = sorted({s["datum"] for s in snaps})
    by = {(s["market_id"], s["datum"]): num(s["prozent"]) for s in snaps}
    verlauf = {
        "title": "Verlauf",
        "header": ["Event", "Antwort", "Exa %", "Markt bei Erfassung %"] + dates,
        "rows": [[r["event"], r["antwort"], r["exa"], r["markt"]] +
                 [by.get((r["market_id"], d)) for d in dates] for r in rows],
        "widths": [48, 22, 9, 11] + [10] * len(dates),
        "link_col": 0, "links": links,
        "num_cols": {c: "0.0" for c in range(2, 4 + len(dates))},
    }

    cur, prev = months(now)
    a_rows, bold = [], []

    def block(title, rs):
        s = stats(rs)
        bold.append(len(a_rows))
        a_rows.append([title])
        a_rows.extend([
            ["Erfasste Märkte", s["maerkte"]],
            ["davon mit Exa-Wahrscheinlichkeit", s["mit_exa"]],
            ["davon aufgelöst", s["aufgeloest"]],
            ["Exa näher am Ergebnis als der Markt",
             f'{s["exa_naeher"]} von {s["aufgeloest"]} ({pct(s["exa_naeher"], s["aufgeloest"])})'],
            ["Brier-Score Exa (kleiner = besser)", r1(s["brier_exa"], 4) if s["brier_exa"] is not None else "–"],
            ["Brier-Score Markt (kleiner = besser)", r1(s["brier_markt"], 4) if s["brier_markt"] is not None else "–"],
            [f"Signale mit |Delta| ≥ {SIGNAL_MIN_DELTA:.0f} PP (aufgelöst)", s["signale"]],
            ["Richtung der Signale richtig", f'{s["treffer"]} von {s["signale"]} ({pct(s["treffer"], s["signale"])})'],
            [f"Sim. Gewinn, je ${STAKE:.0f} pro Signal (ohne Spread)", round(s["pnl"], 2)],
            [f"Sim. Gewinn, je ${STAKE:.0f} pro Signal (mit Spread)", round(s["pnl_spread"], 2)],
            ["Offene Märkte: Preis bewegt sich Richtung Exa",
             f'{s["richtung_ok"]} von {s["richtung_n"]} ({pct(s["richtung_ok"], s["richtung_n"])})'],
            [],
        ])

    block(f"Gesamt (Stand {now.strftime('%d.%m.%Y %H:%M')})", rows)
    block(f"Laufender Monat {cur}", [r for r in rows if r["erfasst"].startswith(cur)])
    block(f"Vormonat {prev}", [r for r in rows if r["erfasst"].startswith(prev)])
    bold.append(len(a_rows))
    a_rows.append(["Kalibrierung: Wenn Exa X % sagt – wie oft tritt es wirklich ein?"])
    bold.append(len(a_rows))
    a_rows.append(["Exa-Bereich", "Anzahl", "Ø Exa %", "Tatsächlich %", "Ø Markt %"])
    a_rows.extend(calibration(rows))
    a_rows.append([])
    bold.append(len(a_rows))
    a_rows.append(["Erklärung"])
    for t in [
        "Delta = Exa-Wahrscheinlichkeit minus Marktpreis bei Erfassung (Prozentpunkte). Positiv = Exa hält die Antwort für wahrscheinlicher als der Markt.",
        "Exa näher am Ergebnis: Nach Auflösung (100 % oder 0 %) – war Exas Zahl näher dran als der Marktpreis?",
        "Brier-Score: mittlerer quadratischer Fehler der Wahrscheinlichkeiten. 0 = perfekt, 0,25 = Münzwurf. Exa sollte unter dem Markt liegen.",
        f"Sim. Gewinn: ${STAKE:.0f} in Richtung Exa gesetzt (Ja wenn Delta > 0, Nein wenn Delta < 0) zum Preis bei Erfassung. "
        "'Mit Spread' rechnet den halben Spread beim Einstieg drauf – realistischer.",
        "Kalibrierung: Bei guter Kalibrierung liegt 'Tatsächlich %' nahe an 'Ø Exa %'.",
    ]:
        a_rows.append([t])
    auswertung = {"title": "Auswertung", "header": None, "rows": a_rows,
                  "widths": [60, 18, 12, 14, 12], "bold_rows": bold, "wrap_cols": [0]}
    return [maerkte, verlauf, auswertung]


# ---------------------------------------------------------------- Excel (Ersatz ohne Google)

def build_excel(tabs):
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    head_fill, head_font = PatternFill("solid", fgColor="0B5F3A"), Font(color="FFFFFF", bold=True)
    green, red = PatternFill("solid", fgColor="D9F2E3"), PatternFill("solid", fgColor="F8D7DA")
    wb = Workbook()
    wb.remove(wb.active)
    for t in tabs:
        ws = wb.create_sheet(t["title"])
        hdr = 1 if t.get("header") else 0
        if hdr:
            ws.append(t["header"])
        for row in t["rows"]:
            ws.append(row)
        for i, w in enumerate(t.get("widths") or [], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        n, ncols = ws.max_row, ws.max_column
        if hdr:
            for c in range(1, ncols + 1):
                cell = ws.cell(row=1, column=c)
                cell.fill, cell.font = head_fill, head_font
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}{n}"
        for c, fmt in (t.get("num_cols") or {}).items():
            for r in range(hdr + 1, n + 1):
                ws.cell(row=r, column=c + 1).number_format = fmt.replace('" $"', ' $')
        for c in t.get("wrap_cols") or []:
            for r in range(hdr + 1, n + 1):
                ws.cell(row=r, column=c + 1).alignment = Alignment(wrap_text=True, vertical="top")
        for r in t.get("bold_rows") or []:
            for c in range(1, ncols + 1):
                ws.cell(row=hdr + r + 1, column=c).font = Font(bold=True, size=12)
        if n > hdr:
            for c in t.get("posneg_cols") or []:
                rng = f"{get_column_letter(c + 1)}{hdr + 1}:{get_column_letter(c + 1)}{n}"
                ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["0"], fill=green))
                ws.conditional_formatting.add(rng, CellIsRule(operator="lessThan", formula=["0"], fill=red))
            for c in t.get("yesno_cols") or []:
                rng = f"{get_column_letter(c + 1)}{hdr + 1}:{get_column_letter(c + 1)}{n}"
                ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"ja"'], fill=green))
                ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"nein"'], fill=red))
        lc = t.get("link_col")
        if lc is not None:
            for i, url in enumerate(t.get("links") or []):
                if url:
                    cell = ws.cell(row=hdr + i + 1, column=lc + 1)
                    cell.hyperlink = url
                    cell.font = Font(color="0B5FFF", underline="single")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- Telegram

def send(text):
    if not TOKEN or not CHAT_ID:
        print(text)
        return
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=30)
    if r.status_code != 200:
        print("Telegram-Fehler:", r.status_code, r.text[:300])


def send_document(filename, content, caption=""):
    if not TOKEN or not CHAT_ID:
        with open(filename, "wb") as f:
            f.write(content)
        print("Datei gespeichert:", filename)
        return
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                      data={"chat_id": CHAT_ID, "caption": caption},
                      files={"document": (filename, content,
                                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                      timeout=60)
    if r.status_code != 200:
        print("Telegram-Fehler:", r.status_code, r.text[:300])


def fmt_stats(s):
    lines = [f"Märkte: {s['maerkte']} · mit Exa: {s['mit_exa']} · aufgelöst: {s['aufgeloest']}"]
    if s["aufgeloest"]:
        lines.append(f"Exa näher am Ergebnis: <b>{s['exa_naeher']}/{s['aufgeloest']}</b> ({pct(s['exa_naeher'], s['aufgeloest'])})")
        lines.append(f"Brier Exa {s['brier_exa']:.3f} vs. Markt {s['brier_markt']:.3f} (kleiner = besser)")
    if s["signale"]:
        lines.append(f"Signale ≥{SIGNAL_MIN_DELTA:.0f} PP richtig: <b>{s['treffer']}/{s['signale']}</b> ({pct(s['treffer'], s['signale'])})")
        lines.append(f"Sim. Gewinn ({s['trades']} × ${STAKE:.0f}): {s['pnl']:+.2f} $ · mit Spread: <b>{s['pnl_spread']:+.2f} $</b>")
    if s["richtung_n"]:
        lines.append(f"Offene Märkte bewegen sich Richtung Exa: {s['richtung_ok']}/{s['richtung_n']} ({pct(s['richtung_ok'], s['richtung_n'])})")
    return "\n".join(lines)


# ---------------------------------------------------------------- Ablauf

def sync_sheet():
    """Nur das Google Sheet neu schreiben (fuer new_markets.py). Kein Telegram, kein Schnappschuss."""
    if not gsheet.enabled():
        return
    log = read_csv(LOG_FILE)
    if not log:
        return
    markets = fetch_markets(x.get("market_id") for x in log)
    rows = build_rows(log, markets)
    gsheet.write_tables(tables(rows, read_csv(SNAP_FILE), datetime.now(TZ)))
    print(f"Google Sheet aktualisiert ({len(rows)} Märkte)")


def main():
    if os.environ.get("GITHUB_ACTIONS") and (not TOKEN or not CHAT_ID):
        raise SystemExit("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt – Secrets prüfen.")
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    log = read_csv(LOG_FILE)
    if not log:
        send("📊 <b>Exa-Tracking</b>\nNoch keine Märkte protokolliert – der Neue-Märkte-Alarm füllt das Protokoll automatisch.")
        return

    markets = fetch_markets(x.get("market_id") for x in log)
    snaps, n_new = save_snapshot(markets, read_csv(SNAP_FILE), today)
    rows = build_rows(log, markets)
    tabs = tables(rows, snaps, now)

    sheet_ok = False
    if gsheet.enabled():
        try:
            gsheet.write_tables(tabs)
            sheet_ok = True
        except Exception as ex:  # Sheet-Fehler soll den Bericht nicht verhindern
            print("Google-Sheet-Fehler:", ex)

    cur, prev = months(now)
    if MODE == "monthly":
        text = (f"📈 <b>Monatliches Exa-Feedback – {prev}</b>\n\n"
                f"<b>Märkte aus {prev}:</b>\n{fmt_stats(stats([r for r in rows if r['erfasst'].startswith(prev)]))}\n\n"
                f"<b>Gesamt seit Start:</b>\n{fmt_stats(stats(rows))}")
    else:
        moved = sorted((r for r in rows if r["bewegung"] is not None and r["status"] == "offen"),
                       key=lambda r: -abs(r["bewegung"]))[:5]
        top = "\n".join(
            f"{'📈' if r['bewegung'] > 0 else '📉'} {escape(r['event'][:60])} – {escape(r['antwort'][:20])}: "
            f"{r['markt']:.0f}% → {r['jetzt']:.0f}%"
            + (f" (Exa {r['exa']:.0f}%, {'✅ Richtung Exa' if r['richtung_exa'] else '❌ gegen Exa'})"
               if r["exa"] is not None and r["richtung_exa"] is not None else "")
            for r in moved if abs(r["bewegung"]) >= 3)
        text = (f"📊 <b>Exa-Tracking – {now.strftime('%d.%m.%Y')}</b>\n\n{fmt_stats(stats(rows))}"
                + (f"\n\n<b>Größte Bewegungen (offene Märkte):</b>\n{top}" if top else ""))
    if sheet_ok:
        text += f'\n\n📄 <a href="{gsheet.sheet_url()}">Google Sheet öffnen</a>'
    send(text)
    if not sheet_ok:
        send_document(f"exa_tracking_{today}.xlsx", build_excel(tabs), "📊 Exa-Tracking – in Excel öffnen")
    print(f"{len(rows)} Märkte, {n_new} neue Schnappschüsse, Modus {MODE}, Sheet {'ok' if sheet_ok else 'aus'}")


if __name__ == "__main__":
    main()
