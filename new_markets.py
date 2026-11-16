"""
Neue-Maerkte-Alarm fuer Polymarket.

Meldet per Telegram neue Events (erstellt in den letzten NEW_MAX_AGE_HOURS
Stunden), sobald sie NEW_MIN_VOLUME Volumen erreichen.
Sport, Krypto-Preise, Wetter und wiederkehrende Maerkte werden ignoriert.
Zusaetzlich: neue Esports-Maerkte ohne Spielwetten (Roster, Transfers, Props).
Ergebnis: Telegram-Nachricht + eigene Datei neue_maerkte_*.html

Zustand: .state/new_markets_state.json (bereits gemeldete Events).
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import requests

GAMMA = "https://gamma-api.polymarket.com"
STATE_FILE = os.path.join(".state", "new_markets_state.json")
TZ = ZoneInfo("Europe/Vienna")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


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


de
