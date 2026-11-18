"""
Neue-Maerkte-Alarm fuer Polymarket.

Meldet per Telegram neue Events (erstellt in den letzten NEW_MAX_AGE_HOURS
Stunden), sobald sie NEW_MIN_VOLUME Volumen erreichen.
Sport, Krypto-Preise, Wetter und wiederkehrende Maerkte werden ignoriert.
Zusaetzlich: neue Esports-Maerkte ohne Spielwetten (Roster, Transfers, Props).
Jeder neue Markt wird mit Exa (/answer) analysiert - Hauptmodell und Vergleichsmodell
(EXA_MODELS, Standard "exa,exa-pro"): Nachrichtenlage, eigene
Einschaetzung und Signal (unterbewertet / fair / ueberbewertet / unklar).
Ergebnis: Telegram-Nachricht + eigene Datei neue_maerkte_*.html

Zustand: .state/new_markets_state.json (bereits gemeldete Events).
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import requests

import araistotle
import orderbook

GAMMA = "https://gamma-api.polymarket.com"
STATE_FILE = os.path.join(".state", "new_markets_state.json")
# Dauerhaftes Protokoll im Repo (wird vom Workflow committed) fuer exa_tracker.py
LOG_FILE = os.path.join("data", "exa_log.csv")
TZ = ZoneInfo("Europe/Vienna")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EXA_API_KEY = os.environ.get("EXA_API_KEY", "")


def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_list(name, default):
    raw = os.environ.get(name, "") or default
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


MIN_VOLUME = env_float("NEW_MIN_VOLUME", 5000)
MAX_AGE_HOURS = env_float("NEW_MAX_AGE_HOURS", 168)
# Esports-Nebenmaerkte (Roster, Transfers, Turniersieger, Props) - keine Spielwetten
ESPORTS_MIN_VOLUME = env_float("NEW_ESPORTS_MIN_VOLUME", 1000)
ESPORTS_MAX_AGE_HOURS = env_float("NEW_ESPORTS_MAX_AGE_HOURS", 168)
# Exa-Analyse: wie viele neue Maerkte pro Lauf maximal analysiert werden (Kostenbremse)
EXA_MAX_PER_RUN = int(env_float("EXA_MAX_PER_RUN", 20))
# Exa-Modelle: erstes = Hauptmodell (Signal, Einstieg), zweites = Vergleichsmodell.
# Beide bewerten dieselbe Antwort, damit sie im Tracking direkt verglichen werden koennen.
EXA_MODELS = env_list("EXA_MODELS", "exa,exa-pro")
EXA_MODEL_MAIN = EXA_MODELS[0] if EXA_MODELS else "exa"
EXA_MODEL_PRO = EXA_MODELS[1] if len(EXA_MODELS) > 1 else ""
MATCH_RE = re.compile(r"\bvs\.?\b|game \d+ winner|map \d+ winner", re.I)
EXCLUDE_TAGS = env_list(
    "NEW_EXCLUDE_TAGS",
    "sports,games,esports,crypto-prices,up-or-down,recurring,weather,hide-from-new",
)
# Zu allgemeine Tags, die fuer die Themen-Gruppierung nicht zaehlen
GENERIC_TAGS = {
    "politics", "tech", "pop-culture", "culture", "world", "global", "ai", "business",
    "finance", "geopolitics", "celebrities", "big-tech", "united-states", "us", "trump",
    "economy", "music", "movies", "elections", "global-elections", "breaking-news",
    "sports", "games", "esports", "hide-from-new", "props",
}
EXCLUDE_TITLE = re.compile(r"\bvs\.?\b|up or down|price of .* on|above .* on|highest temperature", re.I)

STOPWORDS = {
    "will", "who", "what", "which", "when", "where", "how", "the", "and", "for", "with", "by",
    "before", "after", "end", "new", "next", "many", "much", "be", "in", "of", "on", "to",
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "2026", "2027", "2028", "yes", "no", "out", "win",
}


# ---------------------------------------------------------------- API

def get_json(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"API nicht erreichbar: {url}")


def parse_time(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_new_events(cutoff, min_volume, tag=None):
    """Offene Events ab min_volume, neueste zuerst, bis zum Cutoff-Zeitpunkt."""
    events, cursor = [], None
    for _ in range(50):
        params = {"closed": "false", "limit": 100, "order": "createdAt",
                  "ascending": "false", "volume_min": int(min_volume)}
        if tag:
            params["tag_slug"] = tag
        if cursor:
            params["after_cursor"] = cursor
        data = get_json(f"{GAMMA}/events/keyset", params)
        page = data.get("events") or []
        events.extend(page)
        cursor = data.get("next_cursor")
        if not cursor or not page:
            break
        last = parse_time(page[-1].get("createdAt"))
        if last and last < cutoff:
            break
    return events


def tag_slugs(e):
    return {str(t.get("slug") or "").lower() for t in (e.get("tags") or [])} - {""}


def is_junk(e):
    tags = tag_slugs(e)
    tags |= {str(t.get("label") or "").lower() for t in (e.get("tags") or [])}
    if tags & set(EXCLUDE_TAGS):
        return True
    return bool(EXCLUDE_TITLE.search(e.get("title") or ""))


# ---------------------------------------------------------------- Exa-Analyse

EXA_SCHEMA = {
    "type": "object",
    "properties": {
        "nachrichtenlage": {
            "type": "string",
            "description": "2-3 Saetze auf Deutsch: was die neuesten Nachrichten zu dieser Frage sagen, mit Datum.",
        },
        "antwort": {
            "type": "string",
            "description": "Die Antwortoption, bei der der Marktpreis am deutlichsten von deiner Einschaetzung abweicht (bei Ja/Nein-Maerkten: 'Ja').",
        },
        "marktpreis_prozent": {
            "type": "number",
            "description": "Aktueller Marktpreis dieser Antwort in Prozent (aus der Frage uebernommen).",
        },
        "einschaetzung_prozent": {
            "type": "number",
            "description": "Deine Wahrscheinlichkeit fuer diese Antwort in Prozent, 0-100, nur auf Basis der Quellen.",
        },
        "signal": {
            "type": "string",
            "description": "Genau eines dieser Woerter: unterbewertet, fair, ueberbewertet, unklar. Ist die Antwort im Markt zu billig (unterbewertet), passend (fair) oder zu teuer (ueberbewertet)? 'unklar' wenn die Quellen nicht reichen.",
        },
        "begruendung": {
            "type": "string",
            "description": "1-2 Saetze auf Deutsch, warum.",
        },
    },
    "required": ["nachrichtenlage", "antwort", "marktpreis_prozent",
                 "einschaetzung_prozent", "signal", "begruendung"],
}

EXA_SYSTEM = (
    "Du analysierst Prognosemaerkte auf Polymarket. Suche die neuesten, verlaesslichen "
    "Nachrichten (bevorzugt die letzten 14 Tage) zur Frage. Beachte genau die Aufloesungsregeln "
    "und die Frist. Sei nuechtern: wenn es keine klaren neuen Informationen gibt, setze signal "
    "auf 'unklar' oder 'fair'. Antworte auf Deutsch."
)


def market_prices(e, n=8):
    rows = []
    for m in e.get("markets") or []:
        if m.get("closed"):
            continue
        try:
            p = float(json.loads(m.get("outcomePrices") or "[]")[0])
        except (ValueError, IndexError, TypeError):
            continue
        rows.append((p, m.get("groupItemTitle") or "Ja"))
    rows.sort(reverse=True)
    return "; ".join(f"{name}: {p * 100:.0f}%" for p, name in rows[:n])


def exa_analyze(e, model=None, focus=None):
    """Fragt Exa /answer nach einer Einschaetzung. Gibt dict oder None zurueck.
    model: Exa-Modell (exa, exa-pro, ...). focus: Antwort, die gezielt bewertet werden soll."""
    rules = re.sub(r"\s+", " ", e.get("description") or "")[:1200]
    end = (e.get("endDate") or "")[:10]
    query = (
        f"Polymarket-Frage: \"{e.get('title')}\"\n"
        f"Frist/Enddatum: {end or 'unbekannt'}\n"
        f"Aktuelle Marktpreise (Ja-Preis je Antwort): {market_prices(e)}\n"
        f"Aufloesungsregeln: {rules}\n"
        "Was sagen die neuesten Nachrichten? Welche Antwort ist im Vergleich zu den Quellen "
        "am ehesten falsch bepreist?"
    )
    if focus:
        query += (f"\nBewerte gezielt die Antwort \"{focus}\" und trage genau diese Antwort "
                  "im Feld 'antwort' ein.")
    model = model or EXA_MODEL_MAIN
    body = {"query": query, "systemPrompt": EXA_SYSTEM, "outputSchema": EXA_SCHEMA, "model": model}
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
                        ans = {"nachrichtenlage": ans, "signal": "unklar"}
                ans = ans or {}
                sig = str(ans.get("signal", "unklar")).lower().replace("ü", "ue").strip()
                ans["signal"] = sig if sig in SIGNAL_ICON else "unklar"
                ans["quellen"] = [
                    {"title": c.get("title") or c.get("url"), "url": c.get("url"),
                     "date": (c.get("publishedDate") or "")[:10]}
                    for c in (data.get("citations") or [])[:3] if c.get("url")
                ]
                ans["kosten"] = (data.get("costDollars") or {}).get("total")
                ans["model"] = model
                ans["focus"] = focus or ""
                return ans
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (i + 1))
                continue
            if r.status_code == 400 and "outputSchema" in body:
                # Fallback: ohne strukturiertes Schema erneut fragen
                print("Exa lehnt Schema ab, frage ohne Schema:", r.text[:200])
                body = {"query": query, "systemPrompt": EXA_SYSTEM, "model": model}
                continue
            print("Exa-Fehler:", r.status_code, r.text[:200])
            return None
        except requests.RequestException as ex:
            print("Exa-Verbindungsfehler:", ex)
            time.sleep(3 * (i + 1))
    return None


def analyze_all(events):
    """Analysiert die volumenstaerksten neuen Events (max. EXA_MAX_PER_RUN).
    Schritt 1: Hauptmodell waehlt die am ehesten falsch bepreiste Antwort.
    Schritt 2: Vergleichsmodell bewertet genau diese Antwort (gleiche Basis fuer den Vergleich)."""
    if not EXA_API_KEY or EXA_MAX_PER_RUN <= 0:
        return
    todo = sorted(events, key=lambda e: -float(e.get("volume") or 0))[:EXA_MAX_PER_RUN]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for e, res in zip(todo, pool.map(lambda x: exa_analyze(x, EXA_MODEL_MAIN), todo)):
            if res:
                e["_exa"] = res
    if EXA_MODEL_PRO:
        def run_pro(x):
            m, _, _ = pick_market(x)
            if not m:
                return None
            focus = m.get("groupItemTitle") if len(open_markets(x)) > 1 else None
            return exa_analyze(x, EXA_MODEL_PRO, focus)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for e, res in zip(todo, pool.map(run_pro, todo)):
                if res:
                    e["_exa_pro"] = res
    for key, name in (("_exa", EXA_MODEL_MAIN), ("_exa_pro", EXA_MODEL_PRO)):
        if not name:
            continue
        done = [e[key] for e in todo if e.get(key)]
        cost = sum(float(a.get("kosten") or 0) for a in done)
        print(f"Exa {name}: {len(done)}/{len(todo)} analysiert, Kosten ca. ${cost:.3f}")


SIGNAL_ICON = {"unterbewertet": "🟢", "ueberbewertet": "🔴", "fair": "⚪", "unklar": "❔"}
SIGNAL_TEXT = {"unterbewertet": "unterbewertet", "ueberbewertet": "überbewertet",
               "fair": "fair bepreist", "unklar": "unklar"}


def model_label(model):
    return {"exa": "Exa", "exa-pro": "Exa Pro", "exa-fast": "Exa Fast",
            "exa-research": "Exa Research"}.get(model or "", model or "Exa")


def exa_short(a, label="Exa", reason_len=220):
    """Eine Zeile Exa-Fazit fuer Telegram."""
    sig = a.get("signal", "unklar")
    line = f"{SIGNAL_ICON.get(sig, '❔')} <b>{label}: {SIGNAL_TEXT.get(sig, sig)}</b>"
    if a.get("antwort") and sig != "unklar":
        try:
            line += (f" – {escape(str(a['antwort'])[:30])}: Markt {float(a.get('marktpreis_prozent')):.0f}% "
                     f"vs. {label} {float(a.get('einschaetzung_prozent')):.0f}%")
        except (TypeError, ValueError):
            line += f" – {escape(str(a['antwort'])[:30])}"
    text = a.get("begruendung") or a.get("nachrichtenlage") or ""
    if text and reason_len:
        line += f"\n   {escape(str(text)[:reason_len])}"
    return line


def name_match(m, answer):
    a = str(answer or "").strip().lower()
    name = str(m.get("groupItemTitle") or "").strip().lower()
    return bool(a and name and (name == a or name in a or a in name))


def exa_prob(e, key="_exa"):
    """Exa-Wahrscheinlichkeit (Prozent) fuer die getrackte Antwort - nur wenn sie dazu passt."""
    m, p, ok = e.get("_pick") or pick_market(e)
    a = e.get(key) or {}
    if not m or not a or a.get("signal") == "unklar":
        return None
    if key != "_exa":
        ok = len(open_markets(e)) <= 1 or name_match(m, a.get("antwort")) or bool(a.get("focus"))
    if not ok:
        return None
    try:
        return float(a.get("einschaetzung_prozent"))
    except (TypeError, ValueError):
        return None


def exa_direction(e, key="_exa"):
    """'ja' / 'nein' je nach Exa-Delta auf der getrackten Antwort, sonst None."""
    m, p, _ = e.get("_pick") or pick_market(e)
    exa_p = exa_prob(e, key)
    if exa_p is None:
        return None, None
    d = exa_p - p * 100
    if abs(d) < 0.5:
        return None, exa_p
    return ("ja" if d > 0 else "nein"), exa_p


def trade_info(e, key="_exa"):
    """Einstieg in Exas Richtung aus dem Orderbuch: dict oder None."""
    b = e.get("_book")
    side, exa_p = exa_direction(e, key)
    if not b or not side:
        return None
    entry = b.get(f"einstieg_{side}")
    ok, why = orderbook.tradable(b, side)
    fair = exa_p if side == "ja" else 100 - exa_p
    return {"richtung": side, "einstieg": entry, "gefuellt": b.get(f"gefuellt_{side}"),
            "spread": orderbook.spread(b, side), "handelbar": ok, "grund": why,
            "edge": None if entry is None else round(fair - entry, 1)}


def attach_books(events):
    """Waehlt pro Event die getrackte Antwort und holt deren Orderbuch."""
    for e in events:
        e["_pick"] = pick_market(e)
    todo = [e for e in events if e["_pick"][0]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for e, b in zip(todo, pool.map(lambda x: orderbook.market_book(x["_pick"][0]), todo)):
            e["_book"] = b


def trade_short(e, key="_exa"):
    """Telegram-Zeile mit echtem Einstiegspreis."""
    t = trade_info(e, key)
    if not t:
        return ""
    side = "Ja" if t["richtung"] == "ja" else "Nein"
    if t["einstieg"] is None:
        return f"💧 {side}: kein Orderbuch"
    flag = "✅ handelbar" if t["handelbar"] else f"⚠️ {t['grund']}"
    edge = "" if t["edge"] is None else f" · Edge nach Einstieg <b>{t['edge']:+.1f} PP</b>"
    return (f"💧 Einstieg {side} {t['einstieg']:.1f}¢ für {orderbook.TRADE_SIZE:.0f}$"
            f" (Spread {t['spread']:.0f}¢){edge} · {flag}")


def agreement(e):
    """Vergleich Haupt- vs. Vergleichsmodell auf derselben Antwort."""
    s1, p1 = exa_direction(e, "_exa")
    s2, p2 = exa_direction(e, "_exa_pro")
    if p1 is None or p2 is None:
        return ""
    diff = p2 - p1
    if s1 and s1 == s2:
        return f"🤝 Beide Modelle einig: {'Ja' if s1 == 'ja' else 'Nein'} kaufen (Abstand {diff:+.0f} PP)"
    if s1 and s2:
        return f"⚔️ Modelle uneinig ({model_label(EXA_MODEL_MAIN)} {p1:.0f}% vs. {model_label(EXA_MODEL_PRO)} {p2:.0f}%)"
    return ""


# ---------------------------------------------------------------- Gruppierung

def keywords(title):
    words = re.findall(r"[A-Z][A-Za-z0-9'\.\-]{2,}", title or "")
    return {w.strip(".'").lower() for w in words} - STOPWORDS


def group_events(events):
    """Fasst Events zum selben Thema zusammen: mindestens 2 gemeinsame
    Schluesselwoerter im Titel (z. B. 'Jensen Huang') oder mindestens
    2 gemeinsame spezifische Tags (z. B. 'faa' + 'outage')."""
    parent = list(range(len(events)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    kws = [keywords(e.get("title")) for e in events]
    tgs = [tag_slugs(e) - GENERIC_TAGS for e in events]
    for i in range(len(events)):
        for j in range(i + 1, len(events)):
            if len(kws[i] & kws[j]) >= 2 or len(tgs[i] & tgs[j]) >= 2:
                parent[find(i)] = find(j)
    groups = {}
    for i, e in enumerate(events):
        groups.setdefault(find(i), []).append(e)
    out = [sorted(g, key=lambda e: -float(e.get("volume") or 0)) for g in groups.values()]
    return sorted(out, key=lambda g: -sum(float(e.get("volume") or 0) for e in g))


def topic_name(group):
    """Name des Themas: das spezifische Tag, das in den meisten Events der Gruppe vorkommt."""
    counts, labels = {}, {}
    for e in group:
        for t in e.get("tags") or []:
            slug = str(t.get("slug") or "").lower()
            if not slug or slug in GENERIC_TAGS:
                continue
            counts[slug] = counts.get(slug, 0) + 1
            labels[slug] = t.get("label") or slug
    if not counts:
        return "Thema"
    best = max(counts, key=lambda s: (counts[s], len(labels[s])))
    return labels[best]


# ---------------------------------------------------------------- Ausgabe

def money(v):
    v = float(v or 0)
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    return f"${v / 1000:.0f}k" if v >= 1000 else f"${v:.0f}"


def top_answers(e, n=3):
    rows = []
    for m in e.get("markets") or []:
        if m.get("closed"):
            continue
        try:
            p = float(json.loads(m.get("outcomePrices") or "[]")[0])
        except (ValueError, IndexError, TypeError):
            continue
        rows.append((p, m.get("groupItemTitle") or ""))
    if len(rows) == 1:
        return f"Ja {rows[0][0] * 100:.0f}%"
    rows.sort(reverse=True)
    return " · ".join(f"{escape(name[:25])} {p * 100:.0f}%" for p, name in rows[:n])


def event_line(e, now):
    created = parse_time(e.get("createdAt"))
    age_h = (now - created).total_seconds() / 3600 if created else 0
    age = f"{age_h:.0f}h alt" if age_h < 48 else f"{age_h / 24:.0f} Tage alt"
    line = (f'<a href="https://polymarket.com/event/{e.get("slug", "")}">{escape(e.get("title") or "")}</a>\n'
            f'   {money(e.get("volume"))} Vol · {money(e.get("liquidity"))} Liq · {age}\n'
            f'   {top_answers(e)}')
    if e.get("_exa"):
        line += "\n   " + exa_short(e["_exa"], model_label(EXA_MODEL_MAIN))
    if e.get("_exa_pro"):
        line += "\n   " + exa_short(e["_exa_pro"], model_label(EXA_MODEL_PRO), reason_len=150)
        agree = agreement(e)
        if agree:
            line += "\n   " + agree
    ts = trade_short(e)
    if ts:
        line += "\n   " + ts
    if e.get("_ara"):
        line += "\n   " + exa_short(e["_ara"], "ArAIstotle", reason_len=150)
        ta = trade_short(e, "_ara")
        if ta:
            line += "\n   " + ta
    elif e.get("_ara_pending"):
        line += "\n   ⏳ ArAIstotle-Analyse läuft – Ergebnis kommt in die Tabelle"
    return line


def pro_html(e):
    a = e.get("_exa_pro")
    if not a:
        return ""
    sig = a.get("signal", "unklar")
    try:
        nums = f" – {float(a.get('einschaetzung_prozent')):.0f}%"
    except (TypeError, ValueError):
        nums = ""
    ag = agreement(e)
    return (f'<hr><b>{SIGNAL_ICON.get(sig, "❔")} {model_label(EXA_MODEL_PRO)}: '
            f'{SIGNAL_TEXT.get(sig, sig)}</b>{nums}'
            f'<br><small><i>{escape(str(a.get("begruendung", "")))}</i></small>'
            + (f"<br><small>{escape(ag)}</small>" if ag else ""))


def build_html(groups, now, stamp):
    """Sortier- und durchsuchbare HTML-Tabelle aller neuen Maerkte (eigene Datei)."""
    rows = []
    for g in groups:
        topic = topic_name(g) if len(g) > 1 else ""
        for e in g:
            created = parse_time(e.get("createdAt"))
            age_h = (now - created).total_seconds() / 3600 if created else 0
            kind = "Esports" if e.get("_esports") else "Allgemein"
            url = f'https://polymarket.com/event/{e.get("slug", "")}'
            vol = float(e.get("volume") or 0)
            liq = float(e.get("liquidity") or 0)
            a = e.get("_exa")
            if a:
                sig = a.get("signal", "unklar")
                order = {"unterbewertet": 0, "ueberbewertet": 1, "fair": 2, "unklar": 3}.get(sig, 3)
                try:
                    nums = (f"<br>{escape(str(a.get('antwort', ''))[:40])}: Markt "
                            f"{float(a.get('marktpreis_prozent')):.0f}% → Exa "
                            f"{float(a.get('einschaetzung_prozent')):.0f}%")
                except (TypeError, ValueError):
                    nums = ""
                srcs = " ".join(
                    f'<a href="{escape(q["url"])}" target="_blank">[{escape(q.get("date") or "Quelle")}]</a>'
                    for q in a.get("quellen") or [])
                exa_cell = (f'<td data-v="{order}"><b>{SIGNAL_ICON.get(sig, "❔")} '
                            f'{SIGNAL_TEXT.get(sig, sig)}</b>{nums}'
                            f'<br><small>{escape(str(a.get("nachrichtenlage", "")))}</small>'
                            f'<br><small><i>{escape(str(a.get("begruendung", "")))}</i></small>'
                            f'<br><small>{srcs}</small>{pro_html(e)}</td>')
            else:
                exa_cell = '<td data-v="9"><small>nicht analysiert</small></td>'
            t = trade_info(e)
            if t and t["einstieg"] is not None:
                side = "Ja" if t["richtung"] == "ja" else "Nein"
                trade_cell = (f'<td data-v="{t["edge"] if t["edge"] is not None else -999}">'
                              f'{side} {t["einstieg"]:.1f}¢ · Spread {t["spread"]:.0f}¢<br>'
                              f'Edge {t["edge"]:+.1f} PP<br>'
                              f'{"✅ handelbar" if t["handelbar"] else "⚠️ " + escape(t["grund"])}</td>')
            else:
                trade_cell = '<td data-v="-999"><small>–</small></td>'
            rows.append(
                "<tr>"
                f"<td>{kind}</td>"
                f"<td>{escape(topic)}</td>"
                f'<td><a href="{escape(url)}" target="_blank">{escape(e.get("title") or "")}</a></td>'
                f'<td class="n" data-v="{vol:.0f}">{vol:,.0f} $</td>'
                f'<td class="n" data-v="{liq:.0f}">{liq:,.0f} $</td>'
                f'<td class="n" data-v="{age_h:.1f}">{age_h:.0f} h</td>'
                f"<td>{top_answers(e)}</td>"
                f"{exa_cell}"
                f"{trade_cell}"
                "</tr>"
            )
    cols = ["Art", "Thema", "Event", "Volumen", "Liquidität", "Alter", "Top-Antworten (Ja-Preis)",
            "Exa-Analyse", f"Einstieg ({orderbook.TRADE_SIZE:.0f} $, ≤{orderbook.FILL_WINDOW:.0f}¢)"]
    head = "".join(f'<th onclick="sortBy({i})">{c}</th>' for i, c in enumerate(cols))
    return f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Neue Märkte {stamp}</title><style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#fafafa}}
input{{padding:8px;width:100%;max-width:400px;margin-bottom:12px;font-size:15px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:14px}}
th,td{{padding:6px 10px;border-bottom:1px solid #e5e5e5;text-align:left;vertical-align:top}}
th{{background:#0b5f3a;color:#fff;cursor:pointer;position:sticky;top:0}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
tr:hover{{background:#f0fff6}} a{{color:#0b5fff;text-decoration:none}}
p{{color:#444;font-size:14px;max-width:900px}}</style></head><body>
<h2>Neue Märkte – {stamp} – {len(rows)} Events</h2>
<p>Allgemein: neu in den letzten {MAX_AGE_HOURS:.0f} h, ab {money(MIN_VOLUME)} Volumen (ohne Sport, Krypto-Preise, Wetter, wiederkehrende Märkte).
Esports: neu in den letzten {ESPORTS_MAX_AGE_HOURS / 24:.0f} Tagen, ab {money(ESPORTS_MIN_VOLUME)} Volumen, keine Spielwetten.
Exa-Analyse: 🟢 unterbewertet · 🔴 überbewertet · ⚪ fair · ❔ unklar – automatische KI-Einschätzung, immer selbst prüfen.
Spaltenkopf anklicken zum Sortieren.</p>
<input id="q" placeholder="Suchen …" oninput="filter()">
<table id="t"><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>
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


def send_document(filename, content, caption=""):
    if not TOKEN or not CHAT_ID:
        with open(filename, "wb") as f:
            f.write(content)
        print("Datei gespeichert:", filename)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendDocument",
        data={"chat_id": CHAT_ID, "caption": caption},
        files={"document": (filename, content, "text/html")},
        timeout=60,
    )
    if r.status_code != 200:
        print("Telegram-Fehler:", r.status_code, r.text[:300])


def send(text):
    if not TOKEN or not CHAT_ID:
        print(text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=30,
    )
    if r.status_code != 200:
        print("Telegram-Fehler:", r.status_code, r.text[:300])


def send_blocks(header, blocks):
    msg = header
    for b in blocks:
        if len(msg) + len(b) + 2 > 3800:
            send(msg)
            msg = header + " (Fortsetzung)"
        msg += "\n\n" + b
    if blocks:
        send(msg)


# ---------------------------------------------------------------- Protokoll fuer Tracking

LOG_FIELDS = [
    "erfasst_am", "event_id", "event_slug", "event", "art", "market_id", "antwort", "enddatum",
    "volumen", "liquiditaet", "spread", "markt_prozent", "exa_prozent", "delta_pp", "signal",
    "nachrichtenlage", "begruendung", "quellen",
    # Orderbuch bei Erfassung (Cent) - Einstieg = Durchschnittspreis fuer TRADE_SIZE $ innerhalb FILL_WINDOW
    "ja_bid", "ja_ask", "nein_bid", "nein_ask", "einstieg_ja", "gefuellt_ja", "einstieg_nein",
    "gefuellt_nein", "richtung", "einstieg", "edge_pp", "handelbar", "handelbar_grund", "trade_size",
    # Vergleichsmodell (z. B. exa-pro) auf derselben Antwort
    "modell", "modell_pro", "exa_pro_prozent", "delta_pro_pp", "signal_pro", "begruendung_pro",
    "richtung_pro", "einstieg_pro", "edge_pro_pp", "handelbar_pro", "kosten_exa", "kosten_pro",
    # ArAIstotle (Facticity) auf derselben Antwort
    "ara_prozent", "ara_markt_prozent", "delta_ara_pp", "signal_ara", "begruendung_ara",
    "richtung_ara", "einstieg_ara", "edge_ara_pp", "handelbar_ara", "ara_rri", "ara_credits",
]


def open_markets(e):
    out = []
    for m in e.get("markets") or []:
        if m.get("closed"):
            continue
        try:
            p = float(json.loads(m.get("outcomePrices") or "[]")[0])
        except (ValueError, IndexError, TypeError):
            continue
        out.append((m, p))
    return out


def pick_market(e):
    """Welche Antwort wird getrackt: die von Exa genannte, sonst die einzige / teuerste.
    Rueckgabe: (market, preis, exa_passt) - exa_passt=True, wenn Exas Antwort zu diesem Markt gehoert."""
    mk = open_markets(e)
    if not mk:
        return None, None, False
    if len(mk) == 1:
        return mk[0][0], mk[0][1], True
    a = str((e.get("_exa") or {}).get("antwort") or "").strip().lower()
    if a and a not in ("ja", "yes"):
        for m, p in mk:
            name = str(m.get("groupItemTitle") or "").strip().lower()
            if name and (name == a or name in a or a in name):
                return m, p, True
    m, p = max(mk, key=lambda x: x[1])
    return m, p, False


def log_markets(events, now):
    """Haengt alle neuen Maerkte (mit oder ohne Exa-Analyse) an data/exa_log.csv an."""
    rows = []
    for e in events:
        m, p, _ = e.get("_pick") or pick_market(e)
        if not m:
            continue
        a = e.get("_exa") or {}
        exa_p = exa_prob(e, "_exa")
        a2 = e.get("_exa_pro") or {}
        pro_p = exa_prob(e, "_exa_pro")
        t2 = trade_info(e, "_exa_pro") or {}
        b = e.get("_book") or {}
        sp = orderbook.spread(b, "ja") if b else None
        if sp is None:
            try:
                sp = float(m.get("spread")) * 100
            except (TypeError, ValueError):
                sp = None
        mkt = round(p * 100, 1)
        t = trade_info(e) or {}
        rows.append({
            "erfasst_am": now.strftime("%Y-%m-%d %H:%M"),
            "event_id": e.get("id"),
            "event_slug": e.get("slug", ""),
            "event": e.get("title", ""),
            "art": "Esports" if e.get("_esports") else "Allgemein",
            "market_id": m.get("id"),
            "antwort": m.get("groupItemTitle") or "Ja",
            "enddatum": (m.get("endDate") or e.get("endDate") or "")[:10],
            "volumen": round(float(e.get("volume") or 0)),
            "liquiditaet": round(float(e.get("liquidity") or 0)),
            "spread": "" if sp is None else round(sp, 1),
            "markt_prozent": mkt,
            "exa_prozent": "" if exa_p is None else round(exa_p, 1),
            "delta_pp": "" if exa_p is None else round(exa_p - mkt, 1),
            "signal": a.get("signal", "") if a else "",
            "nachrichtenlage": a.get("nachrichtenlage", "") if a else "",
            "begruendung": a.get("begruendung", "") if a else "",
            "quellen": " | ".join(q["url"] for q in a.get("quellen") or []) if a else "",
            **{k: ("" if b.get(k) is None else b.get(k)) for k in
               ("ja_bid", "ja_ask", "nein_bid", "nein_ask", "einstieg_ja", "gefuellt_ja",
                "einstieg_nein", "gefuellt_nein")},
            "richtung": t.get("richtung", ""),
            "einstieg": "" if t.get("einstieg") is None else t["einstieg"],
            "edge_pp": "" if t.get("edge") is None else t["edge"],
            "handelbar": "" if not t else ("ja" if t["handelbar"] else "nein"),
            "handelbar_grund": t.get("grund", ""),
            "trade_size": orderbook.TRADE_SIZE,
            "modell": a.get("model", "") if a else "",
            "modell_pro": a2.get("model", "") if a2 else "",
            "exa_pro_prozent": "" if pro_p is None else round(pro_p, 1),
            "delta_pro_pp": "" if pro_p is None else round(pro_p - mkt, 1),
            "signal_pro": a2.get("signal", "") if a2 else "",
            "begruendung_pro": a2.get("begruendung", "") if a2 else "",
            "richtung_pro": t2.get("richtung", ""),
            "einstieg_pro": "" if t2.get("einstieg") is None else t2["einstieg"],
            "edge_pro_pp": "" if t2.get("edge") is None else t2["edge"],
            "handelbar_pro": "" if not t2 else ("ja" if t2["handelbar"] else "nein"),
            "kosten_exa": "" if not a or a.get("kosten") is None else a.get("kosten"),
            "kosten_pro": "" if not a2 or a2.get("kosten") is None else a2.get("kosten"),
            **ara_fields(e, mkt),
        })
    orderbook.csv_append(LOG_FILE, LOG_FIELDS, rows)


def ara_fields(e, mkt):
    """Log-Spalten fuer ArAIstotle."""
    a = e.get("_ara") or {}
    p = exa_prob(e, "_ara")
    t = trade_info(e, "_ara") or {}
    blank = lambda v: "" if v is None else v
    return {
        "ara_prozent": "" if p is None else round(p, 1),
        "ara_markt_prozent": blank(a.get("marktpreis_prozent")),
        "delta_ara_pp": "" if p is None else round(p - mkt, 1),
        "signal_ara": a.get("signal", "") if a else "",
        "begruendung_ara": a.get("begruendung", "") if a else "",
        "richtung_ara": t.get("richtung", ""),
        "einstieg_ara": blank(t.get("einstieg")),
        "edge_ara_pp": blank(t.get("edge")),
        "handelbar_ara": "" if not t else ("ja" if t["handelbar"] else "nein"),
        "ara_rri": blank(a.get("rri")),
        "ara_credits": blank(a.get("credits")),
    }


def run_araistotle(events, state):
    """ArAIstotle fuer jede neue Antwort (dieselbe wie Exa). Neue Analysen, die bis
    zum Ende der Wartezeit nicht fertig sind, werden im naechsten Lauf nachgetragen."""
    if not araistotle.enabled():
        return {}
    items = []
    for e in sorted(events, key=lambda x: -float(x.get("volume") or 0)):
        m = (e.get("_pick") or (None,))[0]
        if m and m.get("id"):
            items.append((str(e["id"]), str(m["id"]), m.get("groupItemTitle") or "Ja"))
    res, info = araistotle.analyze(items)
    by_id = {str(e["id"]): e for e in events}
    for key, a in res.items():
        e = by_id[key]
        _, price, _ = e.get("_pick") or (None, None, None)
        if price is not None:
            set_ara_signal(a, price * 100)
        e["_ara"] = a
    pend = state.setdefault("ara_pending", {})
    now_iso = datetime.now(timezone.utc).isoformat()
    waiting = set(info.get("pending_keys") or [])
    for key, mid, name in items:
        if key in waiting:
            by_id[key]["_ara_pending"] = True
            pend[mid] = {"name": name, "seit": now_iso}
    return info


def set_ara_signal(a, mkt):
    """Signal aus ArAIstotle-% vs. Marktpreis (falls Facticity keinen Marktpreis mitliefert)."""
    if a.get("marktpreis_prozent") is None:
        a["marktpreis_prozent"] = round(mkt, 1)
    if a.get("signal") in ("offen", "unklar", None, ""):
        d = a["einschaetzung_prozent"] - mkt
        a["signal"] = "fair" if abs(d) < 2 else ("unterbewertet" if d > 0 else "ueberbewertet")


def fill_pending_araistotle(state):
    """Im letzten Lauf angestossene ArAIstotle-Analysen abholen und im Protokoll nachtragen."""
    pend = state.get("ara_pending") or {}
    if not pend or not araistotle.enabled():
        return {}
    limit = datetime.now(timezone.utc) - timedelta(days=2)
    pend = {k: v for k, v in pend.items() if (parse_time(v.get("seit")) or limit) > limit}
    res, info = araistotle.analyze([(mid, mid, v.get("name") or "Ja") for mid, v in pend.items()])
    if res:
        import csv
        try:
            with open(LOG_FILE, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fields, rows = reader.fieldnames or LOG_FIELDS, list(reader)
        except FileNotFoundError:
            fields, rows = LOG_FIELDS, []
        for k in LOG_FIELDS:
            if k not in fields:
                fields.append(k)
        for r in rows:
            a = res.get(str(r.get("market_id")))
            if not a or r.get("ara_prozent"):
                continue
            mkt = float(r.get("markt_prozent") or 0)
            set_ara_signal(a, mkt)
            book = {k: orderbook_num(r.get(k)) for k in
                    ("ja_bid", "ja_ask", "nein_bid", "nein_ask", "einstieg_ja", "gefuellt_ja",
                     "einstieg_nein", "gefuellt_nein")}
            fake = {"_pick": ({"id": r.get("market_id")}, mkt / 100, True), "_ara": a,
                    "_book": book if book.get("einstieg_ja") is not None or book.get("einstieg_nein") is not None
                    else None, "markets": [{}]}
            r.update(ara_fields(fake, mkt))
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"ArAIstotle: {len(res)} Analysen aus dem letzten Lauf nachgetragen")
    state["ara_pending"] = {k: v for k, v in pend.items() if k not in res}
    return info


def orderbook_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- State

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


# ---------------------------------------------------------------- Main

def main():
    if os.environ.get("GITHUB_ACTIONS") and (not TOKEN or not CHAT_ID):
        raise SystemExit("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt – Secrets prüfen.")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    events = [
        e for e in fetch_new_events(cutoff, MIN_VOLUME)
        if (parse_time(e.get("createdAt")) or now) >= cutoff
        and float(e.get("volume") or 0) >= MIN_VOLUME
        and not is_junk(e)
    ]

    # Esports: alles ausser Spielwetten (Roster, Transfers, Turniersieger, Props, Awards)
    if ESPORTS_MIN_VOLUME > 0:
        es_cutoff = now - timedelta(hours=ESPORTS_MAX_AGE_HOURS)
        known_ids = {str(e["id"]) for e in events}
        for e in fetch_new_events(es_cutoff, ESPORTS_MIN_VOLUME, tag="esports"):
            if (str(e["id"]) in known_ids
                    or (parse_time(e.get("createdAt")) or now) < es_cutoff
                    or float(e.get("volume") or 0) < ESPORTS_MIN_VOLUME
                    or MATCH_RE.search(e.get("title") or "")):
                continue
            e["_esports"] = True
            events.append(e)

    state = load_state()
    seen = state.get("seen", {})
    first_run = not state.get("initialized")

    fresh = [e for e in events if str(e["id"]) not in seen]
    for e in fresh:
        seen[str(e["id"])] = now.isoformat()

    # Gemeldete Events 30 Tage merken
    keep_after = now - timedelta(days=30)
    seen = {k: v for k, v in seen.items() if (parse_time(v) or now) >= keep_after}
    state.update({"initialized": True, "seen": seen})
    save_state(state)

    # ArAIstotle-Analysen aus dem letzten Lauf nachtragen (sind inzwischen fertig)
    ara_info = {}
    try:
        ara_info = fill_pending_araistotle(state)
    except Exception as ex:
        print("ArAIstotle-Nachtrag fehlgeschlagen:", ex)
    save_state(state)

    if not fresh:
        print(f"Keine neuen Events (geprüft: {len(events)})")
        return

    analyze_all(fresh)
    attach_books(fresh)
    try:
        ara_info = run_araistotle(fresh, state) or ara_info
    except Exception as ex:
        print("ArAIstotle-Fehler:", ex)
    save_state(state)
    log_markets(fresh, now)

    # Google Sheet sofort mit den neuen Maerkten aktualisieren (Fehler stoppen den Alarm nicht)
    sheet_link = ""
    if os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        try:
            import exa_tracker
            exa_tracker.sync_sheet()
            sheet_link = f'\n📄 <a href="{exa_tracker.gsheet.sheet_url()}">Tracking-Tabelle öffnen</a>'
        except Exception as ex:
            print("Google-Sheet-Fehler:", ex)

    groups = group_events(fresh)
    blocks = []
    for g in groups:
        if len(g) == 1:
            icon = "🎮" if g[0].get("_esports") else "🆕"
            blocks.append(icon + " " + event_line(g[0], now))
        else:
            total = sum(float(e.get("volume") or 0) for e in g)
            icon = "🎮" if all(e.get("_esports") for e in g) else "🧵"
            head = f"{icon} <b>{escape(topic_name(g))}: {len(g)} neue Märkte</b> ({money(total)} gesamt)"
            blocks.append(head + "\n" + "\n".join("• " + event_line(e, now) for e in g[:6]))

    now_local = now.astimezone(TZ).strftime("%d.%m. %H:%M")
    title = "Neue Märkte der letzten" if first_run else "Neue Märkte"
    header = (f"🆕 <b>{title} {MAX_AGE_HOURS:.0f}h</b> ({now_local})" if first_run
              else f"🆕 <b>{title}</b> ab {money(MIN_VOLUME)} Volumen ({now_local})")
    credits = ara_info.get("credits_left")
    if ara_info.get("fehler") and "credit" in ara_info["fehler"].lower():
        header += "\n🚨 <b>Facticity-Credits leer</b> – ArAIstotle analysiert nicht mehr. Aufladen auf app.facticity.ai"
    elif credits is not None and credits < araistotle.LOW_CREDITS:
        header += f"\n⚠️ Facticity-Credits fast leer: noch {credits:.0f} (neue Analyse = 10)"
    elif ara_info.get("fehler") and not any(e.get("_ara") for e in fresh):
        header += f"\n⚠️ ArAIstotle nicht erreichbar: {escape(ara_info['fehler'][:150])}"
    send_blocks(header + sheet_link, blocks)

    stamp = now.astimezone(TZ).strftime("%Y-%m-%d_%H%M")
    send_document(f"neue_maerkte_{stamp}.html", build_html(groups, now, stamp),
                  f"🆕 {len(fresh)} neue Märkte – im Browser öffnen")
    print(f"{len(fresh)} neue Events gemeldet")


if __name__ == "__main__":
    main()
