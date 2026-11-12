"""
Polymarket "Vergessene Märkte"-Scanner
--------------------------------------
Sucht offene Polymarket-Märkte, die kaum noch beachtet werden – also Märkte,
in denen neue Nachrichten womöglich noch nicht eingepreist sind.

Ablauf:
  1) Harte Filter (Polymarket-Marktdaten):
       - Preis zwischen PRICE_MIN und PRICE_MAX (Default 5–95 %)
       - Liquidität der Antwort mind. MIN_LIQUIDITY (Default 5.000 $)
       - mind. MIN_MARKET_VOLUME Gesamtvolumen (Default 2.000 $), damit der Preis
         irgendwann von echten Tradern gebildet wurde und nicht nur von einem Bot
       - Ende in MIN_DAYS_LEFT bis MAX_DAYS_LEFT Tagen (Default 3–120)
       - ohne Sport, Esports, Krypto-Kurse, Wetter, wiederkehrende Märkte
  2) Für jede übrige Antwort werden die echten Trades abgefragt (Polymarket
     Data-API) → Volumen der letzten 7 und 30 Tage, Preisänderung über 7 Tage.
  3) Vernachlässigungs-Score 0–100 (höher = mehr vergessen):
       25 %  Abkühlung: Wochenvolumen vs. Wochenschnitt der letzten 30 Tage
       25 %  Ruhe: wie wenig in den letzten 7 Tagen gehandelt wurde
       20 %  Eingefrorener Preis: kaum Bewegung in 7 Tagen
       10 %  Breiter Spread (wenig Market Maker)
       10 %  Wenig Konkurrenz (Polymarkets "competitive"-Wert)
       10 %  Wenig Kommentare
  4) Pro Event nur die Antwort mit dem höchsten Score; die besten TOP_N kommen
     als HTML per Telegram, alle bewerteten Märkte zusätzlich als CSV.

Konfiguration über Umgebungsvariablen (in GitHub als Secrets/Variables):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (Secrets, Pflicht)
  TOP_N (20), MIN_SCORE (0), PRICE_MIN (0.05), PRICE_MAX (0.95),
  MIN_LIQUIDITY (5000), MIN_MARKET_VOLUME (2000),
  MIN_DAYS_LEFT (3), MAX_DAYS_LEFT (120), EXCLUDE_TAGS (siehe unten)
"""

import csv
import html
import io
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

GAMMA_URL = "https://gamma-api.polymarket.com/events/keyset"
TRADES_URL = "https://data-api.polymarket.com/v2/trades"
PAGE_SIZE = 100
MAX_PAGES = 600
THREADS = 8
TRADE_LIMIT = 1000  # so viele letzte Trades pro Markt werden ausgewertet


def env_float(name, default):
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return float(default)


TOP_N = int(env_float("TOP_N", 20))
MIN_SCORE = env_float("MIN_SCORE", 0)
PRICE_MIN = env_float("PRICE_MIN", 0.05)
PRICE_MAX = env_float("PRICE_MAX", 0.95)
MIN_LIQUIDITY = env_float("MIN_LIQUIDITY", 5000)
MIN_MARKET_VOLUME = env_float("MIN_MARKET_VOLUME", 2000)
MIN_DAYS_LEFT = env_float("MIN_DAYS_LEFT", 3)
MAX_DAYS_LEFT = env_float("MAX_DAYS_LEFT", 120)
EXCLUDE_TAGS = [t.strip().lower() for t in (os.getenv("EXCLUDE_TAGS") or
                "sports,games,esports,soccer,nba,nfl,tennis,crypto prices,"
                "up or down,recurring,weather,hide from new").split(",") if t.strip()]

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ---------- Hilfsfunktionen ----------

def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(v):
    return max(0.0, min(1.0, v))


def parse_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def yes_price(market):
    prices = parse_list(market.get("outcomePrices"))
    return to_float(prices[0], None) if prices else None


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def get_json(session, url, params, tries=4):
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == tries - 1:
                print(f"Abfrage fehlgeschlagen ({exc})", file=sys.stderr)
                return None
            time.sleep(2 ** attempt)
    return None


# ---------- Schritt 1: Events laden & harte Filter ----------

def fetch_open_events(session):
    events, cursor = [], None
    for _ in range(MAX_PAGES):
        params = {"active": "true", "closed": "false", "archived": "false",
                  "limit": PAGE_SIZE}
        if cursor:
            params["after_cursor"] = cursor
        data = get_json(session, GAMMA_URL, params)
        if data is None:
            raise SystemExit("Polymarket-Events konnten nicht geladen werden.")
        page = data.get("events", []) if isinstance(data, dict) else data
        if not page:
            break
        events.extend(page)
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor:
            break
        time.sleep(0.05)
    return events


def candidate_markets(events, now):
    out = []
    for ev in events:
        tags = [str(t.get("label", "")).lower() for t in (ev.get("tags") or [])]
        if any(ex in t for t in tags for ex in EXCLUDE_TAGS):
            continue
        for m in ev.get("markets") or []:
            if m.get("closed") or m.get("active") is False or not m.get("conditionId"):
                continue
            p = yes_price(m)
            if p is None or not (PRICE_MIN <= p <= PRICE_MAX):
                continue
            if to_float(m.get("liquidityNum", m.get("liquidity"))) < MIN_LIQUIDITY:
                continue
            if to_float(m.get("volumeNum", m.get("volume"))) < MIN_MARKET_VOLUME:
                continue
            end = parse_time(m.get("endDate") or ev.get("endDate"))
            if not end:
                continue
            days_left = (end - now) / 86400
            if not (MIN_DAYS_LEFT <= days_left <= MAX_DAYS_LEFT):
                continue
            out.append({"event": ev, "market": m, "price": p,
                        "days_left": days_left, "tags": ", ".join(
                            t.get("label", "") for t in (ev.get("tags") or []))})
    return out


# ---------- Schritt 2: echte Trades auswerten ----------

def trade_stats(session, cand, now):
    m = cand["market"]
    data = get_json(session, TRADES_URL, {"condition": m["conditionId"],
                                          "limit": TRADE_LIMIT, "taker_only": "true"})
    trades = (data or {}).get("data") or []
    vol7 = vol30 = 0.0
    price_7d_ago = None
    for t in trades:  # neueste zuerst
        age = now - to_float(t.get("timestamp"))
        usd = to_float(t.get("size")) * to_float(t.get("price"))
        if age <= 7 * 86400:
            vol7 += usd
        if age <= 30 * 86400:
            vol30 += usd
        if age > 7 * 86400 and price_7d_ago is None:
            tp = to_float(t.get("price"))
            # Preis immer auf "Yes" umrechnen
            price_7d_ago = tp if t.get("outcome_index") in (0, None) else 1 - tp
    capped = len(trades) >= TRADE_LIMIT and price_7d_ago is None
    return {**cand, "vol7": vol7, "vol30": vol30, "price_7d_ago": price_7d_ago,
            "very_active": capped, "ok": data is not None}


# ---------- Schritt 3: Score ----------

def score(c):
    m, ev = c["market"], c["event"]
    vol7, vol30 = c["vol7"], c["vol30"]
    weekly_avg = vol30 / (30 / 7)
    cool = clamp(1 - vol7 / weekly_avg) if weekly_avg > 0 else 1.0
    quiet = clamp(1 - math.log10(1 + vol7) / math.log10(1 + 10000))
    if c["very_active"]:
        change, frozen = None, 0.0
    elif c["price_7d_ago"] is None:
        change, frozen = 0.0, 1.0      # in 7+ Tagen kein älterer Trade → nichts passiert
    else:
        change = c["price"] - c["price_7d_ago"]
        frozen = clamp(1 - abs(change) / 0.05)
    spread = clamp(to_float(m.get("spread")) / 0.05)
    comp = clamp(1 - to_float(m.get("competitive")))
    comments = clamp(1 - math.log10(1 + to_float(ev.get("commentCount"))) / 3)
    total = 100 * (0.25 * cool + 0.25 * quiet + 0.20 * frozen +
                   0.10 * spread + 0.10 * comp + 0.10 * comments)
    return {
        "score": round(total),
        "event_title": ev.get("title", ""),
        "answer": m.get("groupItemTitle") or m.get("question", ""),
        "price": round(c["price"], 3),
        "change_7d_pts": round(change * 100, 1) if change is not None else "",
        "vol_7d_usd": round(vol7),
        "vol_30d_usd": round(vol30),
        "cooling_pct": round(cool * 100),
        "spread": round(to_float(m.get("spread")), 3),
        "liquidity_usd": round(to_float(m.get("liquidityNum", m.get("liquidity")))),
        "total_volume_usd": round(to_float(m.get("volumeNum", m.get("volume")))),
        "comments": int(to_float(ev.get("commentCount"))),
        "days_left": round(c["days_left"]),
        "end_date": str(m.get("endDate") or ev.get("endDate") or "")[:10],
        "tags": c["tags"],
        "url": f"https://polymarket.com/event/{ev.get('slug', '')}",
        "event_id": ev.get("id"),
        "market_id": m.get("id"),
    }


def best_per_event(rows):
    best = {}
    for r in rows:
        k = r["event_id"]
        if k not in best or r["score"] > best[k]["score"]:
            best[k] = r
    out = list(best.values())
    out.sort(key=lambda r: (r["score"], -r["vol_7d_usd"]), reverse=True)
    return out


# ---------- Ausgabe ----------

def rows_to_csv(rows):
    buf = io.StringIO()
    fields = list(rows[0].keys()) if rows else ["score"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def rows_to_html(rows, stamp, total_scored):
    cols = [("Score", "score"), ("Event", "event_title"), ("Antwort", "answer"),
            ("Preis", "price"), ("Δ 7 Tage", "change_7d_pts"),
            ("Volumen 7T $", "vol_7d_usd"), ("Volumen 30T $", "vol_30d_usd"),
            ("Abkühlung", "cooling_pct"), ("Spread", "spread"),
            ("Liquidität $", "liquidity_usd"), ("Kommentare", "comments"),
            ("Tage übrig", "days_left"), ("Tags", "tags")]
    body = []
    for r in rows:
        cells = []
        for _, k in cols:
            v = r.get(k, "")
            if k == "event_title":
                cells.append(f'<td><a href="{html.escape(r["url"])}" target="_blank">'
                             f'{html.escape(str(v))}</a></td>')
            elif k == "score":
                hue = 120 if v >= 75 else 45 if v >= 55 else 0
                cells.append(f'<td class="n" data-v="{v}" style="font-weight:700;'
                             f'color:hsl({hue},60%,35%)">{v}</td>')
            elif k == "price":
                cells.append(f'<td class="n" data-v="{v}">{v*100:.0f}%</td>')
            elif k == "change_7d_pts":
                txt = "–" if v == "" else f"{v:+.1f} Pkt"
                cells.append(f'<td class="n" data-v="{v if v != "" else 999}">{txt}</td>')
            elif k == "cooling_pct":
                cells.append(f'<td class="n" data-v="{v}">{v}%</td>')
            elif k == "spread":
                cells.append(f'<td class="n" data-v="{v}">{v*100:.1f}¢</td>')
            elif isinstance(v, (int, float)):
                cells.append(f'<td class="n" data-v="{v}">{v:,}</td>')
            else:
                cells.append(f"<td>{html.escape(str(v))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    head = "".join(f'<th onclick="sortBy({i})">{t}</th>' for i, (t, _) in enumerate(cols))
    return f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vergessene Märkte {stamp}</title><style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#fafafa}}
input{{padding:8px;width:100%;max-width:400px;margin-bottom:12px;font-size:15px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:14px}}
th,td{{padding:6px 10px;border-bottom:1px solid #e5e5e5;text-align:left;vertical-align:top}}
th{{background:#222;color:#fff;cursor:pointer;position:sticky;top:0}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
tr:hover{{background:#f0f6ff}} a{{color:#0b5fff;text-decoration:none}}
p{{color:#444;font-size:14px;max-width:900px}}</style></head><body>
<h2>Vergessene Märkte – {stamp} UTC – Top {len(rows)} von {total_scored}</h2>
<p>Score 0–100: je höher, desto weniger beachtet (Abkühlung des Handels, wenig Volumen,
eingefrorener Preis, breiter Spread, wenig Konkurrenz, wenig Kommentare).
<b>Nächster Schritt:</b> für die Top-Märkte prüfen, ob es neue Nachrichten gibt, die der Preis noch nicht zeigt.</p>
<input id="q" placeholder="Suchen …" oninput="filter()">
<table id="t"><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>
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


def send_document(filename, content, mime, caption=""):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                      data={"chat_id": CHAT_ID, "caption": caption},
                      files={"document": (filename, content, mime)}, timeout=60)
    if not r.ok:
        sys.exit(f"Telegram-Fehler {r.status_code}: {r.text}")


def send_message(text):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=60)
    if not r.ok:
        print(f"Telegram-Fehler {r.status_code}: {r.text}", file=sys.stderr)


def summary_text(top, stamp, total_scored):
    lines = [f"🕸 <b>Vergessene Märkte</b> – {stamp} UTC",
             f"Top {len(top)} von {total_scored} bewerteten Märkten", ""]
    for r in top[:10]:
        chg = "–" if r["change_7d_pts"] == "" else f'{r["change_7d_pts"]:+.1f} Pkt'
        lines.append(
            f'<b>{r["score"]}</b> · <a href="{html.escape(r["url"])}">'
            f'{html.escape(str(r["event_title"])[:70])}</a>\n'
            f'   {html.escape(str(r["answer"])[:50])}: {r["price"]*100:.0f}% · '
            f'7T ${r["vol_7d_usd"]:,} (30T ${r["vol_30d_usd"]:,}) · Δ {chg} · '
            f'{r["days_left"]} Tage')
    lines.append("\n👉 Komplette Liste in der .html-Datei (im Browser öffnen).")
    return "\n".join(lines)


# ---------- Hauptprogramm ----------

def main():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt.")
    now = time.time()
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-scanner/2.0"

    events = fetch_open_events(session)
    print(f"{len(events)} offene Events geladen.")
    cands = candidate_markets(events, now)
    print(f"{len(cands)} Antworten nach harten Filtern.")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        enriched = list(pool.map(lambda c: trade_stats(session, c, now), cands))
    failed = sum(not c["ok"] for c in enriched)
    print(f"Trades ausgewertet in {time.time() - t0:.0f}s ({failed} Abfragen fehlgeschlagen).")

    scored = best_per_event([score(c) for c in enriched if c["ok"]])
    scored = [r for r in scored if r["score"] >= MIN_SCORE]
    top = scored[:TOP_N]
    print(f"{len(scored)} Events bewertet, Top {len(top)} werden gesendet.")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    html_bytes = rows_to_html(top, stamp, len(scored))
    csv_bytes = rows_to_csv(scored)
    with open("polymarket_scan.html", "wb") as f:
        f.write(html_bytes)
    with open("polymarket_scan.csv", "wb") as f:
        f.write(csv_bytes)

    send_message(summary_text(top, stamp, len(scored)))
    send_document(f"vergessene_maerkte_{stamp}.html", html_bytes, "text/html",
                  f"🕸 Top {len(top)} vergessene Märkte – im Browser öffnen")
    send_document(f"vergessene_maerkte_alle_{stamp}.csv", csv_bytes, "text/csv",
                  f"Alle {len(scored)} bewerteten Märkte")
    print("An Telegram gesendet.")


if __name__ == "__main__":
    main()
