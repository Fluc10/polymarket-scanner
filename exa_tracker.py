"""
Exa-Tracking fuer neue Polymarket-Maerkte.

Liest data/exa_log.csv (geschrieben von new_markets.py), holt die aktuellen
Preise aller protokollierten Maerkte, speichert einen Schnappschuss in
data/exa_snapshots.csv und schickt eine Excel-Datei per Telegram.

Modi (Umgebungsvariable TRACKER_MODE):
  update   - alle 3 Tage: Excel mit aktuellem Stand und Preisverlauf
  monthly  - am 1. des Monats: zusaetzlich Feedback-Bericht, ob Exa richtig lag
"""
import csv
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import requests
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

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


def get_json(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
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
        chunk = ids[k:k + 40]
        query = "&".join(f"id={i}" for i in chunk)
        for closed in ("true", "false"):
            data = get_json(f"{GAMMA}/markets?{query}&closed={closed}&limit=100") or []
            for m in data:
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
    except (ValueError, IndexError, TypeError):
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


# ---------------------------------------------------------------- Auswertung

def brier(p, o):
    return (p / 100 - o) ** 2


def simulate(r, with_spread):
    """Simulierter Gewinn eines $10-Trades in Richtung Exa. None wenn kein Signal / offen."""
    d, mkt, o = r["delta"], r["markt"], r["ergebnis"]
    if d is None or o is None or abs(d) < SIGNAL_MIN_DELTA:
        return None
    half = (r["spread"] or 0) / 2 if with_spread else 0
    if d > 0:  # Exa hoeher -> Ja kaufen
        price = min(99.0, mkt + half) / 100
        return STAKE / price * o - STAKE
    price = min(99.0, (100 - mkt) + half) / 100  # Nein kaufen
    return STAKE / price * (1 - o) - STAKE


def build_rows(log, markets, snaps):
    rows = []
    first_seen = {}
    for s in snaps:
        first_seen.setdefault(s["market_id"], s["datum"])
    for x in log:
        mid = str(x.get("market_id") or "")
        m = markets.get(mid)
        now_p = yes_price(m) * 100 if m and yes_price(m) is not None else None
        mkt = num(x.get("markt_prozent"))
        exa = num(x.get("exa_prozent"))
        delta = num(x.get("delta_pp"))
        o = outcome_of(m)
        move = None if now_p is None or mkt is None else now_p - mkt
        toward = None
        if move is not None and delta not in (None, 0) and abs(move) >= 1:
            toward = (move > 0) == (delta > 0)
        exa_right = None
        if o is not None and exa is not None and mkt is not None:
            exa_right = abs(o * 100 - exa) < abs(o * 100 - mkt)
        rows.append({
            "erfasst": x.get("erfasst_am", ""), "art": x.get("art", ""),
            "event": x.get("event", ""), "slug": x.get("event_slug", ""),
            "antwort": x.get("antwort", ""), "ende": x.get("enddatum", ""),
            "markt": mkt, "exa": exa, "delta": delta,
            "signal": SIGNAL_TEXT.get(x.get("signal", ""), x.get("signal", "")),
            "spread": num(x.get("spread")), "volumen": num(x.get("volumen")),
            "jetzt": now_p, "bewegung": move, "richtung_exa": toward,
            "status": ("aufgelöst: JA" if o == 1 else "aufgelöst: NEIN" if o == 0
                       else "geschlossen" if m and m.get("closed") else "offen"),
            "ergebnis": o, "exa_besser": exa_right,
            "nachrichten": x.get("nachrichtenlage", ""), "begruendung": x.get("begruendung", ""),
            "quellen": x.get("quellen", ""), "market_id": mid,
        })
    for r in rows:
        r["pnl"] = simulate(r, False)
        r["pnl_spread"] = simulate(r, True)
    return rows


def stats(rows):
    """Kennzahlen fuer die uebergebenen Zeilen."""
    with_exa = [r for r in rows if r["exa"] is not None]
    res = [r for r in with_exa if r["ergebnis"] is not None and r["markt"] is not None]
    sig = [r for r in res if r["delta"] is not None and abs(r["delta"]) >= SIGNAL_MIN_DELTA]
    hits = [r for r in sig if (r["delta"] > 0) == (r["ergebnis"] == 1)]
    open_moves = [r for r in with_exa if r["richtung_exa"] is not None and r["status"] == "offen"]
    pnl = [r["pnl"] for r in rows if r["pnl"] is not None]
    pnl_s = [r["pnl_spread"] for r in rows if r["pnl_spread"] is not None]
    out = {
        "maerkte": len(rows),
        "mit_exa": len(with_exa),
        "aufgeloest": len(res),
        "exa_naeher": sum(1 for r in res if r["exa_besser"]),
        "brier_exa": sum(brier(r["exa"], r["ergebnis"]) for r in res) / len(res) if res else None,
        "brier_markt": sum(brier(r["markt"], r["ergebnis"]) for r in res) / len(res) if res else None,
        "signale": len(sig),
        "treffer": len(hits),
        "pnl": sum(pnl) if pnl else 0.0,
        "pnl_spread": sum(pnl_s) if pnl_s else 0.0,
        "trades": len(pnl),
        "richtung_n": len(open_moves),
        "richtung_ok": sum(1 for r in open_moves if r["richtung_exa"]),
    }
    return out


def calibration(rows):
    buckets = [(0, 10), (10, 30), (30, 50), (50, 70), (70, 90), (90, 101)]
    res = [r for r in rows if r["exa"] is not None and r["ergebnis"] is not None]
    out = []
    for lo, hi in buckets:
        b = [r for r in res if lo <= r["exa"] < hi]
        if b:
            out.append((f"{lo}–{min(hi, 100)} %", len(b),
                        sum(r["exa"] for r in b) / len(b),
                        100 * sum(r["ergebnis"] for r in b) / len(b),
                        sum(r["markt"] for r in b) / len(b)))
        else:
            out.append((f"{lo}–{min(hi, 100)} %", 0, None, None, None))
    return out


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "–"


# ---------------------------------------------------------------- Excel

HEAD_FILL = PatternFill("solid", fgColor="0B5F3A")
HEAD_FONT = Font(color="FFFFFF", bold=True)
GREEN = PatternFill("solid", fgColor="D9F2E3")
RED = PatternFill("solid", fgColor="F8D7DA")
GREY = PatternFill("solid", fgColor="EEEEEE")


def style_header(ws, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}{ws.max_row}"


def yn(v):
    return "" if v is None else ("ja" if v else "nein")


def build_excel(rows, snaps, s_all, s_month, month_label):
    wb = Workbook()

    # --- Blatt 1: alle Maerkte
    ws = wb.active
    ws.title = "Märkte"
    cols = [("Erfasst", 16), ("Art", 10), ("Event", 48), ("Antwort", 22), ("Ende", 11),
            ("Markt bei Erfassung %", 11), ("Exa %", 9), ("Delta (Exa−Markt) PP", 11), ("Signal", 14),
            ("Spread bei Erfassung ¢", 10), ("Markt jetzt %", 10), ("Bewegung seit Erfassung PP", 12),
            ("Markt bewegt sich Richtung Exa?", 12), ("Status", 15), ("Exa näher am Ergebnis?", 12),
            ("Sim. Gewinn $10 (ohne Spread)", 12), ("Sim. Gewinn $10 (mit Spread)", 12),
            ("Nachrichtenlage (Exa)", 60), ("Begründung (Exa)", 50), ("Quellen", 40)]
    ws.append([c for c, _ in cols])
    for i, (_, w) in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for r in sorted(rows, key=lambda r: r["erfasst"], reverse=True):
        ws.append([r["erfasst"], r["art"], r["event"], r["antwort"], r["ende"],
                   r["markt"], r["exa"], r["delta"], r["signal"], r["spread"], r["jetzt"],
                   r["bewegung"], yn(r["richtung_exa"]), r["status"], yn(r["exa_besser"]),
                   r["pnl"], r["pnl_spread"], r["nachrichten"], r["begruendung"], r["quellen"]])
        row = ws.max_row
        if r["slug"]:
            c = ws.cell(row=row, column=3)
            c.hyperlink = f"https://polymarket.com/event/{r['slug']}"
            c.font = Font(color="0B5FFF", underline="single")
        for col in (18, 19, 20):
            ws.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
    for col in (6, 7, 8, 10, 11, 12):
        for cell in ws.iter_cols(min_col=col, max_col=col, min_row=2):
            for c in cell:
                c.number_format = "0.0"
    for col in (16, 17):
        for cell in ws.iter_cols(min_col=col, max_col=col, min_row=2):
            for c in cell:
                c.number_format = '+0.00 $;-0.00 $;0.00 $'
    n = ws.max_row
    if n >= 2:
        for col in ("H", "L", "P", "Q"):
            rng = f"{col}2:{col}{n}"
            ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["0"], fill=GREEN))
            ws.conditional_formatting.add(rng, CellIsRule(operator="lessThan", formula=["0"], fill=RED))
        for col in ("M", "O"):
            rng = f"{col}2:{col}{n}"
            ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"ja"'], fill=GREEN))
            ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"nein"'], fill=RED))
    style_header(ws, len(cols))

    # --- Blatt 2: Preisverlauf (eine Spalte pro Schnappschuss)
    wv = wb.create_sheet("Verlauf")
    dates = sorted({s["datum"] for s in snaps})
    by = {(s["market_id"], s["datum"]): num(s["prozent"]) for s in snaps}
    wv.append(["Event", "Antwort", "Exa %", "Markt bei Erfassung %"] + dates)
    for i, w in enumerate([48, 22, 9, 11] + [10] * len(dates), 1):
        wv.column_dimensions[get_column_letter(i)].width = w
    for r in sorted(rows, key=lambda r: r["erfasst"], reverse=True):
        wv.append([r["event"], r["antwort"], r["exa"], r["markt"]] +
                  [by.get((r["market_id"], d)) for d in dates])
    for row in wv.iter_rows(min_row=2, min_col=3):
        for c in row:
            c.number_format = "0.0"
    style_header(wv, 4 + len(dates))

    # --- Blatt 3: Auswertung
    wa = wb.create_sheet("Auswertung")
    wa.column_dimensions["A"].width = 52
    for col in "BCDE":
        wa.column_dimensions[col].width = 16

    def block(title, s):
        wa.append([title])
        wa.cell(row=wa.max_row, column=1).font = Font(bold=True, size=13)
        lines = [
            ("Erfasste Märkte", s["maerkte"]),
            ("davon mit Exa-Wahrscheinlichkeit", s["mit_exa"]),
            ("davon aufgelöst", s["aufgeloest"]),
            ("Exa näher am Ergebnis als der Markt", f'{s["exa_naeher"]} von {s["aufgeloest"]} ({pct(s["exa_naeher"], s["aufgeloest"])})'),
            ("Brier-Score Exa (kleiner = besser)", round(s["brier_exa"], 4) if s["brier_exa"] is not None else "–"),
            ("Brier-Score Markt (kleiner = besser)", round(s["brier_markt"], 4) if s["brier_markt"] is not None else "–"),
            (f"Signale mit |Delta| ≥ {SIGNAL_MIN_DELTA:.0f} PP (aufgelöst)", s["signale"]),
            ("Richtung der Signale richtig", f'{s["treffer"]} von {s["signale"]} ({pct(s["treffer"], s["signale"])})'),
            (f"Sim. Gewinn, je ${STAKE:.0f} pro Signal (ohne Spread)", round(s["pnl"], 2)),
            (f"Sim. Gewinn, je ${STAKE:.0f} pro Signal (mit Spread)", round(s["pnl_spread"], 2)),
            ("Offene Märkte: Preis bewegt sich Richtung Exa", f'{s["richtung_ok"]} von {s["richtung_n"]} ({pct(s["richtung_ok"], s["richtung_n"])})'),
        ]
        for k, v in lines:
            wa.append([k, v])
        wa.append([])

    block("Gesamt (alle Daten)", s_all)
    if s_month is not None:
        block(f"Monat {month_label}", s_month)

    wa.append(["Kalibrierung: Wenn Exa X % sagt – wie oft tritt es wirklich ein?"])
    wa.cell(row=wa.max_row, column=1).font = Font(bold=True, size=13)
    wa.append(["Exa-Bereich", "Anzahl", "Ø Exa %", "Tatsächlich %", "Ø Markt %"])
    for c in range(1, 6):
        wa.cell(row=wa.max_row, column=c).fill, wa.cell(row=wa.max_row, column=c).font = HEAD_FILL, HEAD_FONT
    for b in calibration(rows):
        wa.append([b[0], b[1]] + [None if v is None else round(v, 1) for v in b[2:]])
    wa.append([])
    wa.append(["Erklärung"])
    wa.cell(row=wa.max_row, column=1).font = Font(bold=True)
    for t in [
        "Delta = Exa-Wahrscheinlichkeit minus Marktpreis bei Erfassung (Prozentpunkte). Positiv = Exa hält die Antwort für wahrscheinlicher als der Markt.",
        "Exa näher am Ergebnis: Nach Auflösung (100 % oder 0 %) – war Exas Zahl näher dran als der Marktpreis?",
        "Brier-Score: mittlerer quadratischer Fehler der Wahrscheinlichkeiten. 0 = perfekt, 0,25 = Münzwurf. Exa sollte unter dem Markt liegen.",
        f"Sim. Gewinn: ${STAKE:.0f} in Richtung Exa gesetzt (Ja wenn Delta > 0, Nein wenn Delta < 0) zum Preis bei Erfassung. "
        "'Mit Spread' rechnet den halben Spread beim Einstieg drauf – realistischer.",
        "Kalibrierung: Bei guter Kalibrierung liegt 'Tatsächlich %' nahe an 'Ø Exa %'.",
    ]:
        wa.append([t])
        wa.cell(row=wa.max_row, column=1).alignment = Alignment(wrap_text=True)

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


# ---------------------------------------------------------------- Main

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

    # Schnappschuss speichern (einmal pro Tag)
    snaps = read_csv(SNAP_FILE)
    done = {(s["datum"], s["market_id"]) for s in snaps}
    new_snaps = []
    for mid, m in markets.items():
        p = yes_price(m)
        if p is None or (today, mid) in done:
            continue
        o = outcome_of(m)
        new_snaps.append({"datum": today, "market_id": mid, "prozent": round(p * 100, 1),
                          "geschlossen": "ja" if m.get("closed") else "nein",
                          "ergebnis": "" if o is None else o})
    if new_snaps:
        os.makedirs(os.path.dirname(SNAP_FILE), exist_ok=True)
        new_file = not os.path.exists(SNAP_FILE)
        with open(SNAP_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SNAP_FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(new_snaps)
        snaps += new_snaps

    rows = build_rows(log, markets, snaps)
    s_all = stats(rows)

    s_month, month_label = None, ""
    if MODE == "monthly":
        last_month_end = now.replace(day=1) - timedelta(days=1)
        month_label = last_month_end.strftime("%Y-%m")
        month_rows = [r for r in rows if r["erfasst"].startswith(month_label)]
        s_month = stats(month_rows)

    xlsx = build_excel(rows, snaps, s_all, s_month, month_label)
    fname = f"exa_tracking_{today}.xlsx"

    if MODE == "monthly":
        text = (f"📈 <b>Monatliches Exa-Feedback – {month_label}</b>\n\n"
                f"<b>Märkte aus {month_label}:</b>\n{fmt_stats(s_month)}\n\n"
                f"<b>Gesamt seit Start:</b>\n{fmt_stats(s_all)}")
    else:
        moved = sorted((r for r in rows if r["bewegung"] is not None and r["status"] == "offen"),
                       key=lambda r: -abs(r["bewegung"]))[:5]
        top = "\n".join(
            f"{'📈' if r['bewegung'] > 0 else '📉'} {escape(r['event'][:60])} – {escape(r['antwort'][:20])}: "
            f"{r['markt']:.0f}% → {r['jetzt']:.0f}%"
            + (f" (Exa {r['exa']:.0f}%, {'✅ Richtung Exa' if r['richtung_exa'] else '❌ gegen Exa'})"
               if r['exa'] is not None and r['richtung_exa'] is not None else "")
            for r in moved)
        text = (f"📊 <b>Exa-Tracking – {now.strftime('%d.%m.%Y')}</b>\n\n{fmt_stats(s_all)}"
                + (f"\n\n<b>Größte Bewegungen (offene Märkte):</b>\n{top}" if top else ""))
    send(text)
    send_document(fname, xlsx, "📊 Exa-Tracking – in Excel öffnen")
    print(f"{len(rows)} Märkte, {len(new_snaps)} neue Schnappschüsse, Modus {MODE}")


if __name__ == "__main__":
    main()
