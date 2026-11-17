"""
FutureSearch-Check fuer die staerksten Exa-Signale des Tages.

Einmal taeglich (Standard 18 Uhr Wien) werden aus data/exa_log.csv die neuen
Maerkte der letzten FS_HOURS Stunden genommen und die FS_PER_DAY Maerkte mit dem
groessten Exa-Delta (|Exa % - Marktpreis %|) an FutureSearch geschickt.
FutureSearch bekommt Frage, Aufloesungsregeln, Enddatum und den aktuellen
Marktpreis und liefert eine eigene Wahrscheinlichkeit mit Begruendung.

Ergebnis:
  - Telegram-Nachricht mit Markt / Exa / Exa Pro / FutureSearch
  - Protokoll data/futuresearch_log.csv (wird vom Workflow committed)
  - Blatt "FutureSearch" im Google Sheet (falls eingerichtet)
  - Guthaben-Warnung per Telegram, wenn das FutureSearch-Guthaben knapp oder leer ist
"""
import asyncio
import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import requests

import orderbook

GAMMA = "https://gamma-api.polymarket.com"
LOG_FILE = os.path.join("data", "exa_log.csv")
FS_FILE = os.path.join("data", "futuresearch_log.csv")
STATE_FILE = os.path.join("data", "futuresearch_state.json")
TZ = ZoneInfo("Europe/Vienna")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FS_KEY = os.environ.get("FUTURESEARCH_API_KEY", "")


def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


FS_PER_DAY = int(env_float("FS_PER_DAY", 2))          # Maerkte pro Tag
FS_HOURS = env_float("FS_HOURS", 24)                  # Zeitfenster rueckwirkend
FS_RUN_HOUR = int(env_float("FS_RUN_HOUR", 18))       # Uhrzeit Wien, ab der geprueft wird
FS_MIN_BALANCE = env_float("FS_MIN_BALANCE", 6)       # Warnung unter diesem Guthaben ($)
FS_EFFORT = (os.environ.get("FS_EFFORT") or "LOW").strip().upper()
FS_FORCE = (os.environ.get("FS_FORCE") or "").strip().lower() in ("1", "true", "ja", "yes")
STAKE = orderbook.TRADE_SIZE

FS_FIELDS = ["geprueft_am", "market_id", "event", "event_slug", "antwort", "frage", "erfasst_am",
             "markt_bei_erfassung", "exa_prozent", "exa_pro_prozent", "delta_pp", "markt_bei_check",
             "fs_prozent", "fs_delta_pp", "fs_richtung", "einig_mit_exa", "begruendung",
             "kosten_ca", "effort",
             # Orderbuch beim Check (Cent) - fuer die Trading Competition
             "ja_bid", "ja_ask", "nein_bid", "nein_ask", "einstieg_ja", "gefuellt_ja",
             "einstieg_nein", "gefuellt_nein"]
COMP_MIN_EDGE = env_float("COMP_MIN_EDGE", 10)


# ---------------------------------------------------------------- Hilfen

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return None if v in (None, "") else float(v)
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


def yes_price(m):
    try:
        return float(json.loads(m.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError, AttributeError):
        return None


def outcome_of(m):
    if not m or not m.get("closed"):
        return None
    p = yes_price(m)
    if p is None:
        return None
    return 1 if p >= 0.98 else 0 if p <= 0.02 else None


def send(text):
    if not TOKEN or not CHAT_ID:
        print(text)
        return
    for i in range(0, len(text), 3900):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text[i:i + 3900], "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=30)
        if r.status_code != 200:
            print("Telegram-Fehler:", r.status_code, r.text[:300])


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ---------------------------------------------------------------- Auswahl

def pick_candidates(log, done_ids, now_utc):
    """Die FS_PER_DAY Maerkte mit dem groessten |Exa-Delta| aus den letzten FS_HOURS Stunden."""
    since = now_utc - timedelta(hours=FS_HOURS)
    best = {}
    for x in log:
        try:
            t = datetime.strptime(x.get("erfasst_am", ""), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        d = num(x.get("delta_pp"))
        mid = str(x.get("market_id") or "")
        if t < since or d is None or not mid or mid in done_ids:
            continue
        if mid not in best or abs(d) > abs(num(best[mid].get("delta_pp")) or 0):
            best[mid] = x
    return sorted(best.values(), key=lambda x: -abs(num(x.get("delta_pp")) or 0))


def build_question(x, m):
    q = (m.get("question") or "").strip()
    if not q:
        ans = x.get("antwort") or "Ja"
        q = x.get("event", "") if ans in ("", "Ja") else f'{x.get("event", "")} – Antwort: {ans}'
    return q


# ---------------------------------------------------------------- FutureSearch

async def fs_balance():
    from futuresearch.billing import get_billing_balance
    return (await get_billing_balance()).current_balance_dollars


async def fs_forecast(rows):
    from pandas import DataFrame
    from futuresearch.ops import forecast
    from futuresearch.generated.models.forecast_effort_level import ForecastEffortLevel
    level = ForecastEffortLevel.HIGH if FS_EFFORT == "HIGH" else ForecastEffortLevel.LOW
    result = await forecast(input=DataFrame(rows), forecast_type="binary", effort_level=level)
    if getattr(result, "error", None):
        print("FutureSearch-Hinweis:", result.error)
    return result.data


def balance_or_none():
    try:
        return asyncio.run(fs_balance())
    except Exception as ex:
        print("Guthaben nicht abrufbar:", ex)
        return None


def is_balance_error(ex):
    try:
        from futuresearch.errors import FuturesearchInsufficientBalanceError
        if isinstance(ex, FuturesearchInsufficientBalanceError):
            return True
    except ImportError:
        pass
    text = str(ex).lower()
    return "insufficient" in text or "balance" in text or "402" in text


def comp_line(prob, book):
    """Telegram-Zeile: wuerde FutureSearch in der Trading Competition handeln?"""
    if prob is None:
        return "🏁 Competition: kein Trade (keine Wahrscheinlichkeit)"
    best = None
    for side, fair in (("ja", prob), ("nein", 100 - prob)):
        entry, filled = book.get(f"einstieg_{side}"), book.get(f"gefuellt_{side}")
        if entry is None or not filled:
            continue
        edge = fair - entry
        if best is None or edge > best[1]:
            best = (side, edge, entry, min(orderbook.TRADE_SIZE, filled))
    if best is None:
        return "🏁 Competition: kein Trade (kein Orderbuch)"
    side, edge, entry, filled = best
    if edge < COMP_MIN_EDGE:
        return f"🏁 Competition: kein Trade (Edge {edge:+.1f} ¢ < {COMP_MIN_EDGE:.0f} ¢)"
    return (f"🏁 Competition-Trade: {'Ja' if side == 'ja' else 'Nein'} @ {entry:.1f}¢ "
            f"für {filled:.0f} $ (Edge +{edge:.1f} ¢)")


# ---------------------------------------------------------------- Google Sheet

def sheet_table(fs_rows, markets):
    rows, links = [], []
    for x in sorted(fs_rows, key=lambda r: r.get("geprueft_am", ""), reverse=True):
        m = markets.get(str(x.get("market_id")))
        o = outcome_of(m)
        p = yes_price(m) if m else None
        mkt_c, exa, fs = num(x.get("markt_bei_check")), num(x.get("exa_prozent")), num(x.get("fs_prozent"))
        pro = num(x.get("exa_pro_prozent"))
        closest = ""
        if o is not None:
            cand = {"Markt": mkt_c, "Exa": exa, "Exa Pro": pro, "FutureSearch": fs}
            cand = {k: v for k, v in cand.items() if v is not None}
            if cand:
                closest = min(cand, key=lambda k: abs(o * 100 - cand[k]))
        side = x.get("fs_richtung") or ""
        gain = None
        if o is not None and side and mkt_c:
            price = mkt_c if side == "ja" else 100 - mkt_c
            won = o if side == "ja" else 1 - o
            gain = round(STAKE / (price / 100) * won - STAKE, 2) if price > 0 else None
        rows.append([x.get("geprueft_am", ""), x.get("event", ""), x.get("antwort", ""),
                     num(x.get("markt_bei_erfassung")), exa, pro, mkt_c, fs, num(x.get("fs_delta_pp")),
                     {"ja": "Ja", "nein": "Nein"}.get(side, ""), x.get("einig_mit_exa", ""),
                     None if p is None else round(p * 100, 1),
                     "aufgelöst: JA" if o == 1 else "aufgelöst: NEIN" if o == 0 else
                     ("geschlossen" if m and m.get("closed") else "offen"),
                     closest, gain, num(x.get("kosten_ca")), x.get("begruendung", "")])
        links.append(f"https://polymarket.com/event/{x.get('event_slug')}" if x.get("event_slug") else "")
    money = '+0.00" $";-0.00" $";0.00" $"'
    return {
        "title": "FutureSearch",
        "header": ["Geprüft (Wien)", "Event", "Antwort", "Markt bei Erfassung %", "Exa %", "Exa Pro %",
                   "Markt bei Check %", "FutureSearch %", "FS Delta (FS−Markt) PP", "FS Richtung",
                   "Einig mit Exa?", "Markt jetzt %", "Status", "Am nächsten am Ergebnis",
                   f"Gewinn {STAKE:.0f} $ in FS-Richtung (Preis bei Check)", "Kosten ca. $",
                   "Begründung (FutureSearch)"],
        "rows": rows,
        "widths": [16, 48, 22, 11, 9, 9, 11, 12, 12, 10, 10, 10, 15, 16, 16, 10, 90],
        "link_col": 1, "links": links,
        "num_cols": {3: "0.0", 4: "0.0", 5: "0.0", 6: "0.0", 7: "0.0", 8: "+0.0;-0.0;0.0",
                     11: "0.0", 14: money, 15: "0.00"},
        "posneg_cols": [8, 14], "yesno_cols": [10], "wrap_cols": [16],
    }


def write_sheet(fs_rows):
    if not (os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")):
        return ""
    try:
        import gsheet
        markets = {}
        for x in fs_rows:
            mid =
