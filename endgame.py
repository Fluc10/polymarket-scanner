"""
Endspurt-Scanner fuer Polymarket.

Sucht Maerkte, die in ENDGAME_MIN_DAYS bis ENDGAME_MAX_DAYS Tagen ablaufen, deren Ja-Preis
zwischen ENDGAME_P_LOW und ENDGAME_P_HIGH Prozent liegt (also noch keine Seite ueber 85 ¢)
und die noch nicht aufgeloest oder von UMA vorgeschlagen sind. Ausgeschlossen: Sportspiele,
Esports-Spiele, Krypto-Preise, Wetter, Anzahl Tweets/Posts, Schiffsverkehr durch Kanaele/Meerengen.

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
Google Sheet: Blaetter "Endspurt-Märkte" (alle aktuellen Maerkte im Fenster),
"Endspurt-Statistik" und - falls Exa an ist - "Endspurt" (Exa-Analysen).
Telegram: Zusammenfassung + neue Maerkte seit dem letzten Lauf + HTML-Datei mit allen Maerkten.
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
EXA_MAX = int(env("ENDGAME_EXA_MAX", 0))           # Maerkte pro Lauf mit Exa-Pruefung (0 = aus)
REEVAL_PP = env("ENDGAME_REEVAL_PP", 10)            # neu pruefen, wenn Preis so stark bewegt
REEVAL_HOURS = env("ENDGAME_REEVAL_HOURS", 12)
MIN_EDGE = env("COMP_MIN_EDGE", 10)
MODELS = [x.strip().lower() for x in (os.environ.get("EXA_MODELS") or "exa,exa-pro").split(",") if x.strip()]
MODEL_MAIN = MODELS[0] if MODELS else "exa"
MODEL_PRO = MODELS[1] if len(MODELS) > 1 else ""

EXCLUDE_TAGS = {"games", "crypto-prices", "up-or-down", "weather", "hide-from-new", "tweet-markets"}
GAME_RE = re.compile(r"\bvs\.?\b|\bv\.\s|game \d|map \d|o/u|over/under|spread|handicap|moneyline|"
                     r"total (goals|points|kills|runs|maps)|both teams to score|first blood|"
                     r"win on \d{4}-\d\d-\d\d|end in a draw", re.I)
CRYPTO_RE = re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|doge|dogecoin|bnb|hype)\b.*"
                       r"\b(price|above|below|reach|hit|dip|close)\b|price of ", re.I)
WEATHER_RE = re.compile(r"temperature|precipitation|rainfall|snowfall|°[cf]\b", re.I)
# Auf Wunsch ausgeschlossen: Anzahl Tweets/Posts und Schiffsverkehr durch Kanaele/Meerengen
TOPIC_RE = re.compile(r"#\s*(tweets|posts)|truth social posts|\btweets? (from|between)|"
                      r"\bpost \d+\s*[-–]\s*\d+|\bpost \d+\+|"
                      r"ships? (transit|traffic|pass)|ship traffic|vessels? (transit|pass)|"
                      r"transits? (of|through) the (suez|panama|bab|strait|canal)", re.I)

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
                or WEATHER_RE.search(title) or TOPIC_RE.search(title))


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
                  "Tatsächlich gewonnen %", "Differenz PP", "Ø Gewinn je 100 $ auf Favorit", "Fazit"])
    first_detail = len(srows)
    srows.extend(calib_rows(pts))
    srows.append([])
    ex = exa_calibration(log, results)
    if ex:
        bold.append(len(srows))
        srows.append(["Wer lag besser? Brier-Score bei der ersten Prüfung (kleiner = besser)"])
        bold.append(len(srows))
        srows.append(["Quelle", "Märkte", "Brier-Score"])
        srows.extend(ex)
        srows.append([])
    bold.append(len(srows))
    srows.append(["Erklärung"])
    for t in [
        "Favorit = die Seite (Ja oder Nein), die zu diesem Zeitpunkt über 50 % steht. Favoritenpreis 85 % heißt: der Markt glaubt zu 85 %, dass der Favorit gewinnt.",
        "Tatsächlich gewonnen % sollte bei einem fairen Markt etwa dem Ø Preis entsprechen. Liegt es klar darüber, sind Favoriten in dieser Phase systematisch zu billig.",
        "Ø Gewinn je 100 $: wenn man in jedem dieser Märkte 100 $ zum Mittelpreis auf den Favoriten gesetzt hätte (ohne Spread). Positiv = Edge.",
        "Jeder Markt wird ab dem ersten Fund täglich weiter beobachtet, auch wenn er später aus dem Bereich 15–85 % läuft – so entsteht keine Verzerrung durch Aussortieren.",
        "Aussagekräftig ab etwa 30 Märkten pro Zeile; darunter ist es Zufall.",
    ]:
        srows.append([t])
    stats_tab = {"title": "Endspurt-Statistik", "header": None, "rows": srows, "bold_rows": bold,
                 "widths": [22, 16, 14, 12, 18, 12, 22, 34], "wrap_cols": [],
                 "posneg_cols": [5, 6]}
    return [analysen, stats_tab]


def num_field(m, *keys):
    for k in keys:
        try:
            v = m.get(k)
            if v not in (None, ""):
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def market_rows(cands, now, new_ids):
    rows = []
    for m in sorted(cands, key=lambda m: days_left(m, now)):
        ev = (m.get("events") or [{}])[0]
        p = yes_price(m) * 100
        bid, ask = num_field(m, "bestBid"), num_field(m, "bestAsk")
        rows.append({
            "id": str(m.get("id")), "neu": str(m.get("id")) in new_ids,
            "frage": m.get("question") or ev.get("title") or "", "slug": ev.get("slug") or m.get("slug") or "",
            "kategorie": category(m), "ablauf": (m.get("endDate") or "")[:16].replace("T", " "),
            "tage": days_left(m, now), "ja": p, "fav": "Ja" if p >= 50 else "Nein", "favp": max(p, 100 - p),
            "spread": None if bid is None or ask is None else round((ask - bid) * 100, 1),
            "vol": num_field(m, "volumeNum", "volume") or 0, "liq": num_field(m, "liquidityNum", "liquidity") or 0,
            "vol24": num_field(m, "volume24hr") or 0,
        })
    return rows


def list_tab(rows):
    return {
        "title": "Endspurt-Märkte",
        "header": ["Neu", "Frage", "Kategorie", "Ablauf (UTC)", "Tage bis Ende", "Ja %", "Favorit",
                   "Favoritenpreis %", "Spread ¢", "Volumen $", "Volumen 24h $", "Liquidität $"],
        "rows": [["🆕" if r["neu"] else "", r["frage"], r["kategorie"], r["ablauf"], round(r["tage"], 1),
                  round(r["ja"], 1), r["fav"], round(r["favp"], 1), r["spread"], round(r["vol"]),
                  round(r["vol24"]), round(r["liq"])] for r in rows],
        "links": [f"https://polymarket.com/event/{r['slug']}" if r["slug"] else "" for r in rows],
        "link_col": 1,
        "widths": [5, 70, 18, 16, 10, 8, 8, 12, 9, 12, 12, 12],
        "num_cols": {4: "0.0", 5: "0.0", 7: "0.0", 8: "0.0", 9: "#,##0", 10: "#,##0", 11: "#,##0"},
    }


def money(v):
    return f"${v / 1_000_000:.1f}M" if v >= 1_000_000 else f"${v / 1000:.0f}k" if v >= 1000 else f"${v:.0f}"


def build_html(rows, stamp):
    trs = []
    for r in rows:
        url = f"https://polymarket.com/event/{r['slug']}"
        sp = "" if r["spread"] is None else f"{r['spread']:.0f} ¢"
        trs.append(
            f"<tr><td>{'🆕' if r['neu'] else ''}</td>"
            f'<td><a href="{escape(url)}" target="_blank">{escape(r["frage"])}</a></td>'
            f"<td>{escape(r['kategorie'])}</td>"
            f'<td class="n" data-v="{r["tage"]:.2f}">{r["tage"]:.1f}</td>'
            f'<td class="n" data-v="{r["ja"]:.1f}">{r["ja"]:.0f} %</td>'
            f'<td class="n" data-v="{r["favp"]:.1f}">{r["fav"]} {r["favp"]:.0f} %</td>'
            f'<td class="n" data-v="{r["spread"] if r["spread"] is not None else 999}">{sp}</td>'
            f'<td class="n" data-v="{r["vol"]:.0f}">{money(r["vol"])}</td>'
            f'<td class="n" data-v="{r["vol24"]:.0f}">{money(r["vol24"])}</td>'
            f'<td class="n" data-v="{r["liq"]:.0f}">{money(r["liq"])}</td></tr>')
    cols = ["Neu", "Frage", "Kategorie", "Tage bis Ende", "Ja", "Favorit", "Spread", "Volumen",
            "Vol. 24h", "Liquidität"]
    head = "".join(f'<th onclick="sortBy({i})">{c}</th>' for i, c in enumerate(cols))
    return f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Endspurt {stamp}</title><style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#fafafa}}
input{{padding:8px;width:100%;max-width:400px;margin-bottom:12px;font-size:15px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:14px}}
th,td{{padding:6px 10px;border-bottom:1px solid #e5e5e5;text-align:left;vertical-align:top}}
th{{background:#0b5f3a;color:#fff;cursor:pointer;position:sticky;top:0}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
tr:hover{{background:#f0fff6}} a{{color:#0b5fff;text-decoration:none}} p{{color:#444;font-size:14px}}</style></head><body>
<h2>Endspurt – {stamp} – {len(rows)} Märkte</h2>
<p>Märkte mit Ablauf in {MIN_DAYS:.0f}–{MAX_DAYS:.0f} Tagen, Ja-Preis {P_LOW:.0f}–{P_HIGH:.0f} %, ab {money(MIN_VOLUME)} Volumen,
nicht aufgelöst oder vorgeschlagen. Ohne Sportspiele, Esports-Spiele, Krypto-Preise, Wetter, Tweet-/Post-Anzahl, Schiffsverkehr. Spaltenkopf anklicken zum Sortieren.</p>
<input id="q" placeholder="Suchen …" oninput="filter()">
<table id="t"><thead><tr>{head}</tr></thead><tbody>{''.join(trs)}</tbody></table>
<script>
let dir={{}};
function sortBy(c){{const tb=document.querySelector('#t tbody');const rows=[...tb.rows];dir[c]=!dir[c];
rows.sort((a,b)=>{{const x=a.cells[c],y=b.cells[c];
const vx=x.dataset.v!==undefined?parseFloat(x.dataset.v):x.innerText.toLowerCase();
const vy=y.dataset.v!==undefined?parseFloat(y.dataset.v):y.innerText.toLowerCase();
return (vx>vy?1:vx<vy?-1:0)*(dir[c]?-1:1);}});rows.forEach(r=>tb.appendChild(r));}}
function filter(){{const q=document.getElementById('q').value.toLowerCase();
for(const r of document.querySelectorAll('#t tbody tr'))r.style.display=r.innerText.toLowerCase().includes(q)?'':'none';}}
</script></body></html>""".encode("utf-8")


def send_html(filename, content, caption):
    if not et.TOKEN or not et.CHAT_ID:
        with open(filename, "wb") as f:
            f.write(content)
        print("Datei gespeichert:", filename)
        return
    r = requests.post(f"https://api.telegram.org/bot{et.TOKEN}/sendDocument",
                      data={"chat_id": et.CHAT_ID, "caption": caption},
                      files={"document": (filename, content, "text/html")}, timeout=60)
    if r.status_code != 200:
        print("Telegram-Fehler:", r.status_code, r.text[:300])


def order_sheets():
    """Endspurt-Blaetter vor die Competition-Blaetter sortieren."""
    sh = gsheet._open()
    meta = sh.fetch_sheet_metadata({"fields": "sheets(properties(sheetId,title,index))"})
    sheets = sorted(meta.get("sheets", []), key=lambda s: s["properties"]["index"])
    titles = [s["properties"]["title"] for s in sheets]
    ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheets}
    ours = [t for t in ("Endspurt-Märkte", "Endspurt", "Endspurt-Statistik") if t in ids]
    comp = [t for t in titles if t.startswith("Competition")]
    rest = [t for t in titles if t not in ours and t not in comp]
    req = [{"updateSheetProperties": {"properties": {"sheetId": ids[t], "index": i}, "fields": "index"}}
           for i, t in enumerate(rest + ours + comp)]
    sh.batch_update({"requests": req})


def signal_block(m, row, now):
    e1, e2 = et.num(row["edge_pp"]), et.num(row["edge_pro_pp"])
    use_pro = e2 is not None and (e1 is None or e2 > e1)
    side = row["richtung_pro"] if use_pro else row["richtung"]
    edge = e2 if use_pro else e1
    entry = et.num(row["einstieg_pro"]) if use_pro else et.num(row["einstieg"])
    filled = et.num(row.get(f"gefuellt_{side}")) if side else None
    sp = orderbook.spread({k: et.num(row.get(k)) for k in BOOK_KEYS}, side) if side else None
    ok = side and sp is not None and sp <= orderbook.MAX_SPREAD and (filled or 0) >= orderbook.TRADE_SIZE - 0.01
    lines = [f'🎯 <a href="https://polymarket.com/event/{row["event_slug"]}">{escape(row["frage"][:110])}</a>'
             f" – endet in {float(row['tage_bis_ende']):.1f} Tagen"]
    t = f"   Markt Ja {float(row['markt_prozent']):.0f}%"
    if row["exa_prozent"]:
        t += f" · Exa {float(row['exa_prozent']):.0f}% ({STATUS_TEXT.get(row['exa_status'], '')})"
    if row["exa_pro_prozent"]:
        t += f" · Exa Pro {float(row['exa_pro_prozent']):.0f}%"
    lines.append(t)
    if side and entry is not None:
        lines.append(f"   💧 Einstieg {'Ja' if side == 'ja' else 'Nein'} {entry:.1f}¢ für "
                     f"{min(orderbook.TRADE_SIZE, filled or 0):.0f}$ (Spread {sp or 0:.0f}¢) · "
                     f"Edge <b>{edge:+.1f}¢</b> ({'Exa Pro' if use_pro else 'Exa'}) · "
                     f"{'✅ handelbar' if ok else '⚠️ dünnes Orderbuch'}")
    why = row["exa_ereignis"] or row["exa_begruendung"]
    if why:
        lines.append(f"   <i>{escape(why[:220])}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------- Ablauf

def main():
    if os.environ.get("GITHUB_ACTIONS") and (not et.TOKEN or not et.CHAT_ID):
        raise SystemExit("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt – Secrets prüfen.")
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M")

    cands = candidates(now)
    print(f"{len(cands)} Kandidaten im Fenster")

    # 1) Neue Maerkte fuer die Statistik registrieren
    known = {x["market_id"] for x in et.read_csv(MARKETS_FILE)}
    new = []
    for m in cands:
        mid = str(m.get("id"))
        if mid in known:
            continue
        ev = (m.get("events") or [{}])[0]
        new.append({"erfasst_am": stamp, "market_id": mid, "event": ev.get("title", ""),
                    "event_slug": ev.get("slug", ""), "frage": m.get("question", ""),
                    "antwort": m.get("groupItemTitle") or "Ja", "enddatum": (m.get("endDate") or "")[:16],
                    "volumen": round(float(m.get("volumeNum") or m.get("volume") or 0)),
                    "kategorie": category(m), "erster_preis": round(yes_price(m) * 100, 1)})
    if new:
        orderbook.csv_append(MARKETS_FILE, MARKET_FIELDS, new)
    tracked = et.read_csv(MARKETS_FILE)

    # 2) Exa-Pruefung (neu oder stark bewegt), naechster Ablauf zuerst
    state = load_state()
    todo = sorted([m for m in cands if needs_check(m, state, now)],
                  key=lambda m: (str(m.get("id")) in state, days_left(m, now)))[:EXA_MAX if EXA_API_KEY else 0]
    log_rows = []
    if todo:
        with ThreadPoolExecutor(max_workers=4) as pool:
            main_res = list(pool.map(lambda m: exa_ask(m, MODEL_MAIN, now), todo))
            pro_res = list(pool.map(lambda m: exa_ask(m, MODEL_PRO, now), todo)) if MODEL_PRO else [None] * len(todo)
            books = list(pool.map(lambda m: orderbook.market_book(m) or {}, todo))
        for m, a, a2, b in zip(todo, main_res, pro_res, books):
            mid = str(m.get("id"))
            state[mid] = {"p": round(yes_price(m) * 100, 1), "t": now.isoformat()}
            p1, p2 = prob_of(a), prob_of(a2)
            d1, d2 = decide(p1, b), decide(p2, b)
            ev = (m.get("events") or [{}])[0]
            log_rows.append({
                "erfasst_am": stamp, "market_id": mid, "event": ev.get("title", ""),
                "event_slug": ev.get("slug", ""), "frage": m.get("question", ""),
                "antwort": m.get("groupItemTitle") or "Ja", "enddatum": (m.get("endDate") or "")[:16],
                "tage_bis_ende": round(days_left(m, now), 2),
                "volumen": round(float(m.get("volumeNum") or m.get("volume") or 0)),
                "markt_prozent": round(yes_price(m) * 100, 1),
                "exa_prozent": "" if p1 is None else p1, "exa_status": (a or {}).get("status", ""),
                "exa_ereignis": (a or {}).get("schluesselereignis", ""),
                "exa_begruendung": (a or {}).get("begruendung", ""),
                "exa_nachrichtenlage": (a or {}).get("nachrichtenlage", ""),
                "exa_quellen": " | ".join((a or {}).get("quellen") or []),
                "exa_pro_prozent": "" if p2 is None else p2, "exa_pro_status": (a2 or {}).get("status", ""),
                "exa_pro_begruendung": (a2 or {}).get("begruendung", ""),
                **{k: ("" if b.get(k) is None else b.get(k)) for k in BOOK_KEYS},
                "richtung": d1["seite"] if d1 else "", "einstieg": d1["einstieg"] if d1 else "",
                "edge_pp": d1["edge"] if d1 else "",
                "richtung_pro": d2["seite"] if d2 else "", "einstieg_pro": d2["einstieg"] if d2 else "",
                "edge_pro_pp": d2["edge"] if d2 else "",
                "kosten_exa": (a or {}).get("kosten") or "", "kosten_pro": (a2 or {}).get("kosten") or "",
            })
        orderbook.csv_append(LOG_FILE, LOG_FIELDS, log_rows)
        save_state(state)

    # 3) Tagespreise + Ergebnisse fuer alle beobachteten Maerkte
    results = et.read_csv(RESULTS_FILE)
    done = {r["market_id"] for r in results}
    open_ids = [x["market_id"] for x in tracked if x["market_id"] not in done]
    markets_now = et.fetch_markets(open_ids) if open_ids else {}
    prices = et.read_csv(PRICES_FILE)
    have = {(x["datum"], x["market_id"]) for x in prices}
    new_prices, new_results = [], []
    for mid in open_ids:
        m = markets_now.get(mid)
        if not m:
            continue
        o = et.outcome_of(m)
        if o is not None:
            new_results.append({"market_id": mid, "ergebnis": o, "erkannt_am": stamp})
            continue
        if excluded(m):  # nachtraeglich ausgeschlossene Themen nicht weiter beobachten
            continue
        p, d = yes_price(m), days_left(m, now)
        if p is None or d is None or (today, mid) in have or m.get("closed"):
            continue
        new_prices.append({"datum": today, "market_id": mid, "tage_bis_ende": round(d, 2),
                           "ja_prozent": round(p * 100, 1), "uma_status": m.get("umaResolutionStatus") or ""})
    if new_prices:
        orderbook.csv_append(PRICES_FILE, PRICE_FIELDS, new_prices)
    if new_results:
        orderbook.csv_append(RESULTS_FILE, RESULT_FIELDS, new_results)

    # 4) Orderbuch-Schnappschuss fuer analysierte offene Maerkte (Bewertung in der Competition)
    log = et.read_csv(LOG_FILE)
    snaps = et.read_csv(et.SNAP_FILE)
    have_snap = {(s["datum"], s["market_id"]) for s in snaps}
    snap_ids = sorted({x["market_id"] for x in log} - done - {r["market_id"] for r in new_results})
    snap_ms = [markets_now[i] for i in snap_ids if i in markets_now and (today, i) not in have_snap
               and not markets_now[i].get("closed")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        sb = list(pool.map(lambda m: orderbook.market_book(m) or {}, snap_ms))
    snap_rows = [{"datum": today, "market_id": str(m.get("id")), "prozent": round(yes_price(m) * 100, 1),
                  "geschlossen": "nein", "ergebnis": "",
                  **{k: ("" if b.get(k) is None else b.get(k)) for k in BOOK_KEYS}} for m, b in zip(snap_ms, sb)]
    for r in new_results:
        if (today, r["market_id"]) not in have_snap and r["market_id"] in {x["market_id"] for x in log}:
            snap_rows.append({"datum": today, "market_id": r["market_id"], "prozent": 100 if r["ergebnis"] else 0,
                              "geschlossen": "ja", "ergebnis": r["ergebnis"]})
    if snap_rows:
        orderbook.csv_append(et.SNAP_FILE, et.SNAP_FIELDS, snap_rows)

    # 5) Google Sheet
    url = ""
    results = et.read_csv(RESULTS_FILE)
    mrows = market_rows(cands, now, {x["market_id"] for x in new})
    analysen, stats_tab = sheet_tabs(log, markets_now, et.read_csv(PRICES_FILE), results, now.astimezone(et.TZ))
    tabs = [list_tab(mrows)] + ([analysen] if log else []) + [stats_tab]
    if gsheet.enabled():
        try:
            gsheet.write_tables(tabs)
            order_sheets()
            url = gsheet.sheet_url()
        except Exception as ex:
            print("Google-Sheet-Fehler:", ex)

    # 6) Telegram: Zusammenfassung, neue Maerkte, HTML-Datei mit allen
    local = now.astimezone(et.TZ)
    signals = [r for r in log_rows
               if (et.num(r["edge_pp"]) or -99) >= MIN_EDGE or (et.num(r["edge_pro_pp"]) or -99) >= MIN_EDGE]
    signals.sort(key=lambda r: -max(et.num(r["edge_pp"]) or -99, et.num(r["edge_pro_pp"]) or -99))
    fresh = [r for r in mrows if r["neu"]]
    head = (f"🎯 <b>Endspurt {local:%d.%m. %H:%M}</b>\n"
            f"<b>{len(cands)} Märkte</b> laufen in {MIN_DAYS:.0f}–{MAX_DAYS:.0f} Tagen ab "
            f"(Ja {P_LOW:.0f}–{P_HIGH:.0f} %, ab {money(MIN_VOLUME)} Vol.) · {len(fresh)} neu"
            + (f" · {len(new_results)} aufgelöst" if new_results else ""))
    if log_rows:
        head += f" · {len(log_rows)} mit Exa geprüft, {len(signals)} Signale ≥ {MIN_EDGE:.0f} ¢"
    lines = []
    for r in fresh[:30]:
        lines.append(f'• <a href="https://polymarket.com/event/{r["slug"]}">{escape(r["frage"][:90])}</a>\n'
                     f'   Ja {r["ja"]:.0f} % · endet in {r["tage"]:.1f} Tagen · {money(r["vol"])} Vol.'
                     + (f' · Spread {r["spread"]:.0f} ¢' if r["spread"] is not None else ""))
    if len(fresh) > 30:
        lines.append(f"… und {len(fresh) - 30} weitere in der Datei")
    msg = head + ("\n\n<b>Neu im Fenster:</b>\n" + "\n".join(lines) if lines else "")
    blocks = [signal_block(None, r, now) for r in signals[:10]]
    if blocks:
        msg += "\n\n<b>Exa-Signale:</b>\n\n" + "\n\n".join(blocks)
    if url:
        msg += f'\n\n📄 <a href="{url}">Endspurt-Märkte + Statistik im Google Sheet</a>'
    chunk = ""
    for line in msg.split("\n"):  # in Teilen senden, ohne HTML-Zeilen zu zerschneiden
        if len(chunk) + len(line) + 1 > 3900:
            et.send(chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        et.send(chunk)
    if mrows:
        stamp_l = local.strftime("%Y-%m-%d_%H%M")
        send_html(f"endspurt_{stamp_l}.html", build_html(mrows, stamp_l),
                  f"🎯 {len(mrows)} Märkte, Ablauf in {MIN_DAYS:.0f}–{MAX_DAYS:.0f} Tagen – im Browser öffnen")
    if not url:
        et.send_document(f"endspurt_{today}.xlsx", et.build_excel(tabs), "🎯 Endspurt – in Excel öffnen")
    print(f"{len(cands)} Kandidaten, {len(new)} neu, {len(log_rows)} geprüft, {len(signals)} Signale")


if __name__ == "__main__":
    main()