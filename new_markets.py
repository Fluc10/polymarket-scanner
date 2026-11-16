"""
Neue-Maerkte-Alarm fuer Polymarket.

Meldet per Telegram neue Events (erstellt in den letzten NEW_MAX_AGE_HOURS
Stunden), sobald sie NEW_MIN_VOLUME Volumen erreichen.
Sport, Krypto-Preise, Wetter und wiederkehrende Maerkte werden ignoriert.
Zusaetzlich: neue Esports-Maerkte ohne Spielwetten (Roster, Transfers, Props).
Jeder neue Markt wird mit Exa (/answer) analysiert: Nachrichtenlage, eigene
Einschaetzung und Signal (unterbewertet / fair / ueberbewertet / unklar).
Ergebnis: Telegram-Nachricht + eigene Datei neue_maerkte_*.html

Zustand: .state/new_markets_state.json (bereits gemeldete Events).
"""
import csv
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import requests

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


def exa_analyze(e):
    """Fragt Exa /answer nach einer Einschaetzung. Gibt dict oder None zurueck."""
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
    body = {"query": query, "systemPrompt": EXA_SYSTEM, "outputSchema": EXA_SCHEMA}
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
                return ans
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (i + 1))
                continue
            if r.status_code == 400 and "outputSchema" in body:
                # Fallback: ohne strukturiertes Schema erneut fragen
                print("Exa lehnt Schema ab, frage ohne Schema:", r.text[:200])
                body = {"query": query, "systemPrompt": EXA_SYSTEM}
                continue
            print("Exa-Fehler:", r.status_code, r.text[:200])
            return None
        except requests.RequestException as ex:
            print("Exa-Verbindungsfehler:", ex)
            time.sleep(3 * (i + 1))
    return None


def analyze_all(events):
    """Analysiert die volumenstaerksten neuen Events (max. EXA_MAX_PER_RUN)."""
    if not EXA_API_KEY or EXA_MAX_PER_RUN <= 0:
        return
    todo = sorted(events, key=lambda e: -float(e.get("volume") or 0))[:EXA_MAX_PER_RUN]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for e, res in zip(todo, pool.map(exa_analyze, todo)):
            if res:
                e["_exa"] = res
    done = sum(1 for e in todo if e.get("_exa"))
    cost = sum(float(e["_exa"].get("kosten") or 0) for e in todo if e.get("_exa"))
    print(f"Exa: {done}/{len(todo)} analysiert, Kosten ca. ${cost:.3f}")


SIGNAL_ICON = {"unterbewertet": "🟢", "ueberbewertet": "🔴", "fair": "⚪", "unklar": "❔"}
SIGNAL_TEXT = {"unterbewertet": "unterbewertet", "ueberbewertet": "überbewertet",
               "fair": "fair bepreist", "unklar": "unklar"}


def exa_short(a):
    """Eine Zeile Exa-Fazit fuer Telegram."""
    sig = a.get("signal", "unklar")
    line = f"{SIGNAL_ICON.get(sig, '❔')} <b>Exa: {SIGNAL_TEXT.get(sig, sig)}</b>"
    if a.get("antwort") and sig != "unklar":
        try:
            line += (f" – {escape(str(a['antwort'])[:30])}: Markt {float(a.get('marktpreis_prozent')):.0f}% "
                     f"vs. Exa {float(a.get('einschaetzung_prozent')):.0f}%")
        except (TypeError, ValueError):
            line += f" – {escape(str(a['antwort'])[:30])}"
    text = a.get("begruendung") or a.get("nachrichtenlage") or ""
    if text:
        line += f"\n   {escape(str(text)[:220])}"
    return line


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
        line += "\n   " + exa_short(e["_exa"])
    return line


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
                            f'<br><small>{srcs}</small></td>')
            else:
                exa_cell = '<td data-v="9"><small>nicht analysiert</small></td>'
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
                "</tr>"
            )
    cols = ["Art", "Thema", "Event", "Volumen", "Liquidität", "Alter", "Top-Antworten (Ja-Preis)",
            "Exa-Analyse"]
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
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new_file:
            w.writeheader()
        for e in events:
            m, p, exa_ok = pick_market(e)
            if not m:
                continue
            a = e.get("_exa") or {}
            exa_p = None
            if a and exa_ok and a.get("signal") != "unklar":
                try:
                    exa_p = float(a.get("einschaetzung_prozent"))
                except (TypeError, ValueError):
                    exa_p = None
            try:
                spread = float(m.get("spread")) * 100
            except (TypeError, ValueError):
                spread = None
            mkt = round(p * 100, 1)
            w.writerow({
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
                "spread": "" if spread is None else round(spread, 1),
                "markt_prozent": mkt,
                "exa_prozent": "" if exa_p is None else round(exa_p, 1),
                "delta_pp": "" if exa_p is None else round(exa_p - mkt, 1),
                "signal": a.get("signal", "") if a else "",
                "nachrichtenlage": a.get("nachrichtenlage", "") if a else "",
                "begruendung": a.get("begruendung", "") if a else "",
                "quellen": " | ".join(q["url"] for q in a.get("quellen") or []) if a else "",
            })


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
    save_state({"initialized": True, "seen": seen})

    if not fresh:
        print(f"Keine neuen Events (geprüft: {len(events)})")
        return

    analyze_all(fresh)
    log_markets(fresh, now)

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
    send_blocks(header, blocks)

    stamp = now.astimezone(TZ).strftime("%Y-%m-%d_%H%M")
    send_document(f"neue_maerkte_{stamp}.html", build_html(groups, now, stamp),
                  f"🆕 {len(fresh)} neue Märkte – im Browser öffnen")
    print(f"{len(fresh)} neue Events gemeldet")


if __name__ == "__main__":
    main()
