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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import requests

import gsheet
import orderbook

GAMMA = "https://gamma-api.polymarket.com"
LOG_FILE = os.path.join("data", "exa_log.csv")
SNAP_FILE = os.path.join("data", "exa_snapshots.csv")
SNAP_FIELDS = ["datum", "market_id", "prozent", "geschlossen", "ergebnis",
               "ja_bid", "ja_ask", "nein_bid", "nein_ask", "einstieg_ja", "gefuellt_ja",
               "einstieg_nein", "gefuellt_nein"]
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
STAKE = orderbook.TRADE_SIZE  # simulierter Einsatz pro Trade in $ (Variable TRADE_SIZE, Standard 100)


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
    """Tages-Schnappschuss: Mittelpreis + Orderbuch (nur offene Maerkte)."""
    done = {(x["datum"], x["market_id"]) for x in snaps}
    todo = [(mid, m) for mid, m in markets.items()
            if yes_price(m) is not None and (today, mid) not in done]
    open_ms = [m for _, m in todo if not m.get("closed")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        books = dict(zip((str(m.get("id")) for m in open_ms), pool.map(orderbook.market_book, open_ms)))
    new = []
    for mid, m in todo:
        o = outcome_of(m)
        b = books.get(mid) or {}
        row = {"datum": today, "market_id": mid, "prozent": round(yes_price(m) * 100, 1),
               "geschlossen": "ja" if m.get("closed") else "nein",
               "ergebnis": "" if o is None else o}
        for k in SNAP_FIELDS[5:]:
            row[k] = "" if b.get(k) is None else b.get(k)
        new.append(row)
    if new:
        orderbook.csv_append(SNAP_FILE, SNAP_FIELDS, new)
    return snaps + new, len(new)


# ---------------------------------------------------------------- Auswertung

def brier(p, o):
    return (p / 100 - o) ** 2


def pnl(stake, price_c, won):
    """Gewinn eines Kaufs fuer `stake` $ zum Preis `price_c` Cent, Ausgang won (1/0)."""
    if price_c is None or price_c <= 0 or won is None or not stake:
        return None
    return round(stake / (price_c / 100) * won - stake, 2)


def snap_book(x):
    return {k: num(x.get(k)) for k in SNAP_FIELDS[5:]}


def build_rows(log, markets, snaps):
    by_market = {}
    for x in sorted(snaps, key=lambda x: x["datum"]):
        by_market.setdefault(x["market_id"], []).append(x)
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

        side = x.get("richtung") or ("ja" if (delta or 0) > 0 else "nein" if (delta or 0) < 0 else "")
        won = None if o is None or not side else (o if side == "ja" else 1 - o)
        is_signal = delta is not None and abs(delta) >= SIGNAL_MIN_DELTA and side
        # Variante 1: theoretisch zum Mittelpreis bei Erfassung
        mid_price = None if mkt is None or not side else (mkt if side == "ja" else 100 - mkt)
        # Variante 2: sofort, echter Einstieg aus dem Orderbuch bei Erfassung
        entry = num(x.get("einstieg"))
        filled = num(x.get(f"gefuellt_{side}")) if side else None
        stake_now = min(STAKE, filled) if filled else None
        # Variante 3: erster Tag, an dem der Markt in Exas Richtung handelbar war
        cap_book = {k: num(x.get(k)) for k in SNAP_FIELDS[5:]}
        first = None
        if side:
            if x.get("handelbar") == "ja":
                first = (x.get("erfasst_am", "")[:10], entry)
            else:
                for sn in by_market.get(mid, []):
                    if sn["datum"] < x.get("erfasst_am", "")[:10]:
                        continue
                    ok, _ = orderbook.tradable(snap_book(sn), side)
                    if ok:
                        first = (sn["datum"], num(sn.get(f"einstieg_{side}")))
                        break
        # Ausstieg jetzt: bester Bid in Exas Richtung aus dem letzten Schnappschuss
        last = (by_market.get(mid) or [None])[-1]
        exit_bid = num(last.get(f"{side}_bid")) if last and side else None
        mtm = None
        if stake_now and entry and exit_bid is not None and o is None:
            mtm = round(stake_now / (entry / 100) * exit_bid / 100 - stake_now, 2)
        elif stake_now and entry and won is not None:
            mtm = pnl(stake_now, entry, won)

        r = {
            "erfasst": x.get("erfasst_am", ""), "art": x.get("art", ""),
            "event": x.get("event", ""), "slug": x.get("event_slug", ""),
            "antwort": x.get("antwort", ""), "ende": x.get("enddatum", ""),
            "markt": mkt, "exa": exa, "delta": delta,
            "signal": SIGNAL_TEXT.get(x.get("signal", ""), x.get("signal", "")),
            "richtung": {"ja": "Ja", "nein": "Nein"}.get(side, ""),
            "ja_ba": _ba(cap_book.get("ja_bid"), cap_book.get("ja_ask")),
            "nein_ba": _ba(cap_book.get("nein_bid"), cap_book.get("nein_ask")),
            "spread": num(x.get("spread")), "einstieg": entry, "gefuellt": filled,
            "edge": num(x.get("edge_pp")),
            "handelbar": ("ja" if x.get("handelbar") == "ja" else
                          (x.get("handelbar_grund") or "nein") if x.get("handelbar") else ""),
            "handelbar_seit": "" if not side else (
                f"{first[0]} @ {first[1]:.1f}¢" if first and first[1] is not None else
                "bei Erfassung" if first else
                "noch nicht" if not (m and m.get("closed")) else "nie (Markt geschlossen)"),
            "jetzt": now_p, "bewegung": move, "richtung_exa": toward,
            "ausstieg": exit_bid, "mtm": mtm,
            "status": ("aufgelöst: JA" if o == 1 else "aufgelöst: NEIN" if o == 0
                       else "geschlossen" if m and m.get("closed") else "offen"),
            "ergebnis": o, "exa_besser": exa_right,
            "pnl_mitte": pnl(STAKE, mid_price, won) if is_signal else None,
            "pnl_sofort": pnl(stake_now, entry, won) if is_signal else None,
            "pnl_handelbar": pnl(STAKE, first[1], won) if is_signal and first and first[1] else None,
            "nachrichten": x.get("nachrichtenlage", ""), "begruendung": x.get("begruendung", ""),
            "quellen": x.get("quellen", ""), "market_id": mid,
        }
        rows.append(r)
    return rows


def _ba(bid, ask):
    if bid is None and ask is None:
        return ""
    f = lambda v: "–" if v is None else f"{v:.0f}"
    return f"{f(bid)} / {f(ask)}"


def stats(rows):
    with_exa = [r for r in rows if r["exa"] is not None]
    res = [r for r in with_exa if r["ergebnis"] is not None and r["markt"] is not None]
    sig = [r for r in res if r["delta"] is not None and abs(r["delta"]) >= SIGNAL_MIN_DELTA]
    hits = [r for r in sig if (r["delta"] > 0) == (r["ergebnis"] == 1)]
    open_moves = [r for r in with_exa if r["richtung_exa"] is not None and r["status"] == "offen"]
    sig_all = [r for r in with_exa if r["delta"] is not None and abs(r["delta"]) >= SIGNAL_MIN_DELTA]

    def total(key):
        v = [r[key] for r in rows if r.get(key) is not None]
        return (round(sum(v), 2), len(v))

    return {
        "maerkte": len(rows), "mit_exa": len(with_exa), "aufgeloest": len(res),
        "exa_naeher": sum(1 for r in res if r["exa_besser"]),
        "brier_exa": sum(brier(r["exa"], r["ergebnis"]) for r in res) / len(res) if res else None,
        "brier_markt": sum(brier(r["markt"], r["ergebnis"]) for r in res) / len(res) if res else None,
        "signale": len(sig), "treffer": len(hits),
        "signale_alle": len(sig_all),
        "handelbar_bei_erfassung": sum(1 for r in sig_all if r["handelbar"] == "ja"),
        "spaeter_handelbar": sum(1 for r in sig_all if r["handelbar"] not in ("", "ja")
                                 and r["handelbar_seit"][:1].isdigit()),
        "pnl_mitte": total("pnl_mitte"), "pnl_sofort": total("pnl_sofort"),
        "pnl_handelbar": total("pnl_handelbar"),
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

    T = f"{STAKE:.0f} $"
    maerkte = {
        "title": "Märkte",
        "header": ["Erfasst (UTC)", "Art", "Event", "Antwort", "Ende",
                   "Markt-Mitte bei Erfassung %", "Exa %", "Delta (Exa−Markt) PP", "Signal",
                   "Richtung (Exa)", "Ja Bid / Ask ¢", "Nein Bid / Ask ¢", "Spread Ja ¢",
                   f"Einstieg ¢ ({T}, ≤{orderbook.FILL_WINDOW:.0f}¢ Tiefe)", "Gefüllt $",
                   "Edge nach Einstieg PP", "Handelbar bei Erfassung", "Handelbar seit",
                   "Markt-Mitte jetzt %", "Bewegung seit Erfassung PP", "Markt bewegt sich Richtung Exa?",
                   "Ausstieg jetzt (Bid) ¢", f"Wert bei Verkauf jetzt $ ({T} sofort)",
                   "Status", "Exa näher am Ergebnis?",
                   f"Gewinn {T} theoretisch (Mitte)", f"Gewinn {T} sofort (Orderbuch)",
                   f"Gewinn {T} ab handelbar",
                   "Nachrichtenlage (Exa)", "Begründung (Exa)", "Quellen"],
        "rows": [[r["erfasst"], r["art"], r["event"], r["antwort"], r["ende"], r["markt"], r["exa"],
                  r["delta"], r["signal"], r["richtung"], r["ja_ba"], r["nein_ba"], r["spread"],
                  r["einstieg"], r["gefuellt"], r["edge"], r["handelbar"], r["handelbar_seit"],
                  r1(r["jetzt"]), r["bewegung"], yn(r["richtung_exa"]), r["ausstieg"], r["mtm"],
                  r["status"], yn(r["exa_besser"]), r["pnl_mitte"], r["pnl_sofort"], r["pnl_handelbar"],
                  r["nachrichten"], r["begruendung"], r["quellen"]] for r in rows],
        "widths": [16, 10, 48, 22, 11, 11, 9, 11, 14, 9, 11, 11, 9, 13, 10, 11, 22, 20,
                   10, 12, 12, 10, 13, 15, 12, 13, 13, 13, 60, 50, 40],
        "link_col": 2, "links": links,
        "num_cols": {5: "0.0", 6: "0.0", 7: "+0.0;-0.0;0.0", 12: "0.0", 13: "0.0", 14: "0.00",
                     15: "+0.0;-0.0;0.0", 18: "0.0", 19: "+0.0;-0.0;0.0", 21: "0.0",
                     22: '+0.00" $";-0.00" $";0.00" $"', 25: '+0.00" $";-0.00" $";0.00" $"',
                     26: '+0.00" $";-0.00" $";0.00" $"', 27: '+0.00" $";-0.00" $";0.00" $"'},
        "posneg_cols": [7, 15, 19, 22, 25, 26, 27], "yesno_cols": [16, 20, 24], "wrap_cols": [28, 29, 30],
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
            ["davon mit
