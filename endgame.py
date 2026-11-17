"""
Endspurt-Scanner fuer Polymarket.

Sucht Maerkte, die in ENDGAME_MIN_DAYS bis ENDGAME_MAX_DAYS Tagen ablaufen, deren Ja-Preis
zwischen ENDGAME_P_LOW und ENDGAME_P_HIGH Prozent liegt (also noch keine Seite ueber 85 ¢)
und die noch nicht aufgeloest oder von UMA vorgeschlagen sind. Ausgeschlossen: Sportspiele,
Esports-Spiele, Krypto-Preise, Wetter.

1. Exa + Exa Pro pruefen, ob das Ergebnis laut aktuellen Nachrichten schon (nahezu) feststeht,
   obwohl der Preis es noch nicht zeigt. Signale mit echtem Einstieg (100 $, Fuellung <= 5 ¢)
   gehen per Telegram raus und laufen in der Trading Competition mit.
2. Kalibrierung: Fuer ALLE gefundenen Maerkte wird taeglich der Preis gespeichert (auch wenn er
   spaeter aus dem Band laeuft) und nach Ablauf das Ergebnis. Daraus entsteht die Statistik,
   wie oft z. B. ein 85-%-Favorit 2-3 Tage vor Ende wirklich gewinnt.

Dateien (vom Workflow committed):
  data/endgame_markets.csv   - alle beobachteten Maerkte
  data/endgame_prices.csv    - Tagespreise je Markt mit Tagen bis Ablauf
  data/endgame_results.csv   - Ergebnisse nach Ablauf
  data/endgame_log.csv       - Exa-Analysen inkl. Orderbuch und Edge
  data/endgame_state.json    - wann welcher Markt zuletzt analysiert wurde
Google Sheet: Blaetter "Endspurt" und "Endspurt-Statistik".
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape

import requests

import exa_tracker as et
import gsheet
import orderbook

GAMMA = "https://gamma-api.polymarket.com"
MARKETS_FILE = os.path.join("data", "endgame_markets.csv")
PRICES_FILE = os.path.join("data", "endgame_prices.csv")
RESULTS_FILE = os.path.join("data", "endgame_results.csv")
LOG_FILE = os.path.join("data", "endgame_log.csv")
STATE_FILE = os.path.join("data", "endgame_state.json")

EXA_API_KEY = os.environ.get("EXA_API_KEY", "")
env = et.env_float
MIN_DAYS = env("ENDGAME_MIN_DAYS", 2)
MAX_DAYS = env("ENDGAME_MAX_DAYS", 7)
MIN_VOLUME = env("ENDGAME_MIN_VOLUME", 2000)
P_LOW = env("ENDGAME_P_LOW", 15)
P_HIGH = env("ENDGAME_P_HIGH", 85)
EXA_MAX = int(env("ENDGAME_EXA_MAX", 15))          # Maerkte pro Lauf mit Exa-Pruefung
REEVAL_PP = env("ENDGAME_REEVAL_PP", 10)            # neu pruefen, wenn Preis so stark bewegt
REEVAL_HOURS = env("ENDGAME_REEVAL_HOURS", 12)
MIN_EDGE = env("COMP_MIN_EDGE", 10)
MODELS = [x.strip().lower() for x in (os.environ.get("EXA_MODELS") or "exa,exa-pro").split(",") if x.strip()]
MODEL_MAIN = MODELS[0] if MODELS else "exa"
MODEL_PRO = MODELS[1] if len(MODELS) > 1 else ""

EXCLUDE_TAGS = {"games", "crypto-prices", "up-or-down", "weather", "hide-from-new"}
GAME_RE = re.compile(r"\bvs\.?\b|\bv\.\s|game \d|map \d|o/u|over/under|spread|handicap|moneyline|"
                     r"total (goals|points|kills|runs|maps)|both teams to score|first blood|"
                     r"win on \d{4}-\d\d-\d\d|end in a draw", re.I)
CRYPTO_RE = re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|doge|dogecoin|bnb|hype)\b.*"
                       r"\b(price|above|below|reach|hit|dip|close)\b|price of ", re.I)
WEATHER_RE = re.compile(r"temperature|precipitation|rainfall|snowfall|°[cf]\b", re.I)

MARKET_FIELDS = ["erfasst_am", "market_id", "event", "event_slug", "frage", "antwort", "enddatum",
                 "volumen", "kategorie", "erster_preis"]
PRICE_FIELDS = ["datum", "market_id", "tage_bis_ende", "ja_prozent", "uma_status"]
RESULT_FIELDS = ["market_id", "ergebnis", "erkannt_am"]
BOOK_KEYS = ["ja_bid", "ja_ask", "nein_bid", "nein_ask", "einstieg_ja", "gefuellt_ja",
             "einstieg_nein", "gefuellt_nein"]
LOG_FIELDS = ["erfasst_am", "market_id", "event", "event_slug", "frage", "antwort", "enddatum",
              "tage_bis_ende", "volumen", "markt_prozent",
              "exa_prozent", "exa_status", "exa_ereignis", "exa_begruendung", "exa_nachrichtenlage",
              "exa_quellen", "exa_pro_prozent", "exa_pro_status", "exa_pro_begruendung",
              *BOOK_KEYS, "richtung", "einstieg", "edge_pp", "richtung_pro", "einstieg_pro",
              "edge_pro_pp", "kosten_exa", "kosten_pro"]

STATUS_TEXT = {"bereits_eingetreten": "bereits eingetreten", "praktisch_sicher_ja": "praktisch sicher JA",
               "praktisch_sicher_nein": "praktisch sicher NEIN", "offen": "offen", "unklar": "unklar"}


# ---------------------------------------------------------------- Polymarket

def get_json(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            print("Gamma:", r.status_code, r.text[:200])
            return None
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return None


def fetch_window(now):
    """Offene Maerkte mit Ablauf im Fenster und Mindestvolumen."""
    params = {"closed": "false", "limit": 100, "include_tag": "true",
              "volume_num_min": int(MIN_VOLUME),
              "end_date_min": (now + timedelta(days=MIN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end_date_max": (now + timedelta(days=MAX_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    out, cursor = [], None
    for _ in range(60):
        p = dict(params, **({"after_cursor": cursor} if cursor else {}))
        data = get_json(f"{GAMMA}/markets/keyset", p)
        if data is None:
            break
        page = data if isinstance(data, list) else (data.get("markets") or data.get("data") or [])
        out.extend(page)
        cursor = None if isinstance(data, list) else data.get("next_cursor")
        if not cursor or not page:
            return out
    if out:
        return out
    # Fallback: klassische Seitenabfrage
    for offset in range(0, 3000, 100):
        page = get_json(f"{GAMMA}/markets", dict(params, offset=offset)) or []
        out.extend(page)
        if len(page) < 100:
            break
    return out


def yes_price(m):
    return et.yes_price(m)


def tags_of(m):
    tags = set()
    for src in [m] + list(m.get("events") or []):
        for t in src.get("tags") or []:
            tags.add(str(t.get("slug") or "").lower())
            tags.add(str(t.get("label") or "").lower())
    return tags - {""}


def category(m):
    generic = {"politics", "world", "global", "pop-culture", "culture", "business", "tech", "all",
               "featured", "trending", "breaking-news", "economy"}
    for src in list(m.get("events") or []) + [m]:
        for t in src.get("tags") or []:
            s = str(t.get("slug") or "").lower()
            if s and s not in generic:
                return t.get("label") or s
    return ""


def title_of(m):
    ev = (m.get("events") or [{}])[0]
    return f"{ev.get('title') or ''} {m.get('question') or ''}"


def excluded(m):
    tags = tags_of(m)
    title = title_of(m)
    if tags & EXCLUDE_TAGS:
        return True
    if tags & {"sports", "esports"} and GAME_RE.search(title):
        return True
    return bool(GAME_RE.search(m.get("question") or "") or CRYPTO_RE.search(title)
                or WEATHER_RE.search(title))


def days_left(m, now):
    end = m.get("endDate") or ((m.get("events") or [{}])[0].get("endDate"))
    try:
        t = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t - now).total_seconds() / 86400


def candidates(now):
    out = []
    for m in fetch_window(now):
        p = yes_price(m)
        d = days_left(m, now)
        status = str(m.get("umaResolutionStatus") or "").lower()
        if (p is None or d is None or m.get("closed") or m.get("acceptingOrders") is False
                or status in ("proposed", "disputed", "resolved")):
            continue
        if not (MIN_DAYS <= d <= MAX_DAYS) or not (P_LOW <= p * 100 <= P_HIGH):
            continue
        if float(m.get("volumeNum") or m.get("volume") or 0) < MIN_VOLUME or excluded(m):
            continue
        out.append(m)
    return out


# ---------------------------------------------------------------- Exa

SCHEMA = {
    "type": "object",
    "properties": {
        "nachrichtenlage": {"type": "string",
                            "description": "2-3 Saetze auf Deutsch: neueste Nachrichten zur Frage, mit Datum."},
        "einschaetzung_prozent": {"type": "number",
                                  "description": "Deine Wahrscheinlichkeit in Prozent (0-100), dass der Markt mit JA aufgeloest wird."},
        "status": {"type": "string",
                   "description": "Genau eines: bereits_eingetreten, praktisch_sicher_ja, praktisch_sicher_nein, offen, unklar."},
        "schluesselereignis": {"type": "string",
                               "description": "Das entscheidende Ereignis mit Datum, falls es eines gibt, sonst leer."},
        "begruendung": {"type": "string", "description": "1-2 Saetze auf Deutsch, warum."},
    },
    "required": ["nachrichtenlage", "einschaetzung_prozent", "status", "schluesselereignis", "begruendung"],
}
SYSTEM = (
    "Du pruefst Polymarket-Maerkte kurz vor Ablauf. Ziel: erkennen, ob das Ergebnis laut neuesten, "
    "verlaesslichen Nachrichten schon feststeht oder nahezu sicher ist, obwohl der Marktpreis das noch "
    "nicht zeigt. Halte dich strikt an Aufloesungsregeln, Quelle und Frist. Sei nuechtern: ohne klare "
    "Belege ist der Status 'offen' oder 'unklar'. Antworte auf Deutsch."
)


def exa_ask(m, model, now):
    p = yes_price(m) * 100
    d = days_left(m, now)
    rules = re.sub(r"\s+", " ", m.get("description") or "")[:1500]
    query = (f"Polymarket-Frage: \"{m.get('question')}\"\n"
             f"Ablauf: {(m.get('endDate') or '')[:16]} UTC (in {d:.1f} Tagen)\n"
             f"Aktueller Preis JA: {p:.0f}%\n"
             f"Aufloesungsregeln: {rules}\n"
             f"Ist das Ergebnis aufgrund aktueller Nachrichten (vor allem der letzten Tage) bereits "
             f"eingetreten oder nahezu sicher, obwohl der Markt erst bei {p:.0f}% steht?")
    body = {"query": query, "systemPrompt": SYSTEM, "outputSchema": SCHEMA, "model": model}
    for i in range(3):
        try:
            r = requests.post("https://api.exa.ai/answer", json=body, timeout=90,
                              headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"})
            if r.status_code == 200:
                data = r.json()
                ans = data.get("answer")
                if isinstance(ans, str):
                    try:
                        ans = json.loads(ans)
                    except ValueError:
                        ans = {"nachrichtenlage": ans, "status": "unklar"}
                ans = ans or {}
                st = str(ans.get("status", "unklar")).lower().replace("ü", "ue").replace(" ", "_").strip()
                ans["status"] = st if st in STATUS_TEXT else "unklar"
                ans["quellen"] = [c.get("url") for c in (data.get("citations") or [])[:3] if c.get("url")]
                ans["kosten"] = (data.get("costDollars") or {}).get("total")
                return ans
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (i + 1))
                continue
            if r.status_code == 400 and "outputSchema" in body:
                print("Exa lehnt Schema ab, frage ohne Schema:", r.text[:200])
                body = {"query": query, "systemPrompt": SYSTEM, "model": model}
                continue
            print("Exa-Fehler:", r.status_code, r.text[:200])
            return None
        except requests.RequestException as ex:
            print("Exa-Verbindungsfehler:", ex)
            time.sleep(3 * (i + 1))
    return None


def prob_of(a):
    if not a or a.get("status") == "unklar":
        return None
    try:
        v = float(a.get("einschaetzung_prozent"))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, v))


def decide(prob, book):
    """Seite mit der groessten Edge nach echtem Einstieg (wie in der Competition)."""
    if prob is None or not book:
        return None
    best = None
    for side, fair in (("ja", prob), ("nein", 100 - prob)):
        entry, filled = book.get(f"einstieg_{side}"), book.get(f"gefuellt_{side}")
        if entry is None or not filled:
            continue
        edge = round(fair - entry, 1)
        if best is None or edge > best["edge"]:
            best = {"seite": side, "einstieg": entry, "gefuellt": min(orderbook.TRADE_SIZE, filled),
                    "edge": edge}
    return best


# ---------------------------------------------------------------- Zustand

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


def needs_check(m, state, now):
    s = state.get(str(m.get("id")))
    if not s:
        return True
    try:
        last = datetime.fromisoformat(s["t"])
    except (KeyError, ValueError):
        return True
    moved = abs(yes_price(m) * 100 - float(s.get("p", 0)))
    return moved >= REEVAL_PP and (now - last) >= timedelta(hours=REEVAL_HOURS)


# ---------------------------------------------------------------- Statistik

BUCKETS = [(50, 60), (60, 70), (70, 75), (75, 80), (80, 85), (85, 90), (90, 95), (95, 100.01)]
DAY_GROUPS = [("6–7 Tage", 5.5, 99), ("4–5 Tage", 3.5, 5.5), ("2–3 Tage", 1.5, 3.5), ("0–1 Tage", -1, 1.5)]


def day_group(d):
    for name, lo, hi in DAY_GROUPS:
        if lo <= d < hi:
            return name
    return None


def bucket_name(lo, hi):
    return f"{lo}–{min(hi, 100):.0f} %"


def calibration_points(prices, results):
    """Je Markt und Tagesgruppe ein Punkt: Favoritenpreis und ob der Favorit gewonnen hat."""
    res = {r["market_id"]: int(float(r["ergebnis"])) for r in results if r.get("ergebnis") in ("0", "1")}
    latest = {}
    for x in sorted(prices, key=lambda x: x["datum"]):
        mid = x["market_id"]
        if mid not in res:
            continue
        d, p = et.num(x.get("tage_bis_ende")), et.num(x.get("ja_prozent"))
        g = None if d is None else day_group(d)
        if g is None or p is None:
            continue
        latest[(mid, g)] = p  # spaeteste Beobachtung je Gruppe
    pts = []
    for (mid, g), p in latest.items():
        fav_yes = p >= 50
        fav = p if fav_yes else 100 - p
        won = res[mid] == 1 if fav_yes else res[mid] == 0
        pts.append({"market_id": mid, "gruppe": g, "fav": fav, "won": won})
    return pts


def calib_rows(pts):
    rows = []
    for g, _, _ in DAY_GROUPS + [("alle Zeitpunkte", 0, 0)]:
        for lo, hi in BUCKETS:
            b = [x for x in pts if lo <= x["fav"] < hi and (g == "alle Zeitpunkte" or x["gruppe"] == g)]
            if not b:
                continue
            avg = sum(x["fav"] for x in b) / len(b)
            hit = 100 * sum(1 for x in b if x["won"]) / len(b)
            gain = sum(100 / (x["fav"] / 100) * x["won"] - 100 for x in b) / len(b)
            rows.append([g, bucket_name(lo, hi), len(b), round(avg, 1), round(hit, 1),
                         round(hit - avg, 1), round(gain, 2),
                         "Favorit gewinnt öfter als bepreist" if hit - avg >= 3 else
                         "Favorit gewinnt seltener als bepreist" if hit - avg <= -3 else "passt etwa"])
    return rows


def matrix_rows(pts):
    out = []
    for lo, hi in BUCKETS:
        row = [bucket_name(lo, hi)]
        for g, _, _ in DAY_GROUPS:
            b = [x for x in pts if lo <= x["fav"] < hi and x["gruppe"] == g]
            row.append(f"{100 * sum(1 for x in b if x['won']) / len(b):.0f} % (n={len(b)})" if b else "–")
        out.append(row)
    return out


def exa_calibration(log, results):
    res = {r["market_id"]: int(float(r["ergebnis"])) for r in results if r.get("ergebnis") in ("0", "1")}
    seen, rows = set(), []
    for x in log:  # erste Analyse je Markt
        if x["market_id"] in seen or x["market_id"] not in res:
            continue
        seen.add(x["market_id"])
        rows.append((x, res[x["market_id"]]))
    out = []
    for key, name in (("markt_prozent", "Markt"), ("exa_prozent", "Exa"), ("exa_pro_prozent", "Exa Pro")):
        v = [(et.num(x.get(key)), o) for x, o in rows if et.num(x.get(key)) is not None]
        if v:
            br = sum((p / 100 - o) ** 2 for p, o in v) / len(v)
            out.append([name, len(v), round(br, 4)])
    return out


# ---------------------------------------------------------------- Sheet + Telegram

def sheet_tabs(log, markets_now, prices, results, now):
    res = {r["market_id"]: r["ergebnis"] for r in results}
    latest = {}
    for x in log:
        latest[x["market_id"]] = x
    rows, links = [], []
    for x in sorted(latest.values(), key=lambda x: x.get("enddatum", "")):
        m = markets_now.get(x["market_id"])
        p_now = yes_price(m) * 100 if m and yes_price(m) is not None else None
        erg = res.get(x["market_id"], "")
        status = "aufgelöst: JA" if erg == "1" else "aufgelöst: NEIN" if erg == "0" else (
            "geschlossen" if m and m.get("closed") else "offen")
        rows.append([x["erfasst_am"], x["frage"], x["enddatum"], et.num(x["tage_bis_ende"]),
                     et.num(x["markt_prozent"]), et.num(x["exa_prozent"]),
                     STATUS_TEXT.get(x["exa_status"], x["exa_status"]), et.num(x["exa_pro_prozent"]),
                     STATUS_TEXT.get(x["exa_pro_status"], x["exa_pro_status"]),
                     {"ja": "Ja", "nein": "Nein"}.get(x["richtung"], ""), et.num(x["einstieg"]),
                     et.num(x["edge_pp"]), et.num(x["edge_pro_pp"]),
                     None if p_now is None else round(p_now, 1), status,
                     x["exa_ereignis"], x["exa_begruendung"], x["exa_pro_begruendung"]])
        links.append(f"https://polymarket.com/event/{x['event_slug']}" if x.get("event_slug") else "")
    analysen = {
        "title": "Endspurt",
        "header": ["Geprüft (UTC)", "Frage", "Ablauf", "Tage bis Ende bei Prüfung", "Markt Ja %", "Exa %",
                   "Exa Status", "Exa Pro %", "Exa Pro Status", "Richtung (Exa)", "Einstieg ¢",
                   "Edge Exa ¢", "Edge Exa Pro ¢", "Markt Ja jetzt %", "Ergebnis",
                   "Schlüsselereignis", "Begründung Exa", "Begründung Exa Pro"],
        "rows": rows, "links": links, "link_col": 1,
        "widths": [16, 55, 12, 10, 9, 8, 18, 9, 18, 9, 9, 9, 10, 10, 15, 40, 60, 60],
        "num_cols": {3: "0.0", 4: "0.0", 5: "0.0", 7: "0.0", 10: "0.0", 11: "+0.0;-0.0;0.0",
                     12: "+0.0;-0.0;0.0", 13: "0.0"},
        "posneg_cols": [11, 12], "wrap_cols": [15, 16, 17],
    }

    pts = calibration_points(prices, results)
    tracked = len({x["market_id"] for x in prices})
    srows, bold = [], []
    bold.append(len(srows))
    srows.append([f"📊 Endspurt-Statistik – Stand {now:%d.%m.%Y %H:%M}"])
    srows.append([f"Beobachtete Märkte: {tracked} · davon aufgelöst: {len({p['market_id'] for p in pts})} · "
                  f"Datenpunkte: {len(pts)}"])
    srows.append(["Frage: Wenn der Favorit X Tage vor Ablauf bei Y % steht – wie oft gewinnt er wirklich?"])
    srows.append([])
    bold.append(len(srows))
    srows.append(["Übersicht: tatsächliche Trefferquote des Favoriten (n = Anzahl Märkte)"])
    bold.append(len(srows))
    srows.append(["Favoritenpreis"] + [g for g, _, _ in DAY_GROUPS])
    srows.extend(matrix_rows(pts))
    srows.append([])
    bold.append(len(srows))
    srows.append(["Details"])
    bold.append(len(srows))
    srows.append(["Zeitpunkt vor Ablauf", "Favoritenpreis", "Anzahl Märkte", "Ø Preis %",
                  "Tatsächlich gewonnen %", "Diff