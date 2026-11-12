"""
Polymarket Scanner
------------------
Holt alle offenen Polymarket-Events über die öffentliche Gamma-API,
filtert nach Gesamtvolumen (Summe über ALLE Antwortmöglichkeiten eines Events)
und nach 24-Stunden-Volumen, sortiert nach 24h-Volumen und schickt das
Ergebnis als HTML (klickbare Links) + CSV per Telegram.

Konfiguration über Umgebungsvariablen (in GitHub als Secrets/Variables):
  TELEGRAM_BOT_TOKEN   (Secret, Pflicht)  – Token von @BotFather
  TELEGRAM_CHAT_ID     (Secret, Pflicht)  – Chat-ID, an die gesendet wird
  MIN_VOLUME           (optional, Default 10000)  – Gesamtvolumen min.
  MAX_VOLUME           (optional, Default 200000) – Gesamtvolumen max.
  MIN_VOLUME_24H       (optional, Default 1000)   – 24h-Volumen min.
  MAX_VOLUME_24H       (optional, Default 0)      – 24h-Volumen max. (0 = keine Grenze)
"""

import csv
import html
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

GAMMA_URL = "https://gamma-api.polymarket.com/events/keyset"
PAGE_SIZE = 100
MAX_PAGES = 500  # Sicherheitsgrenze gegen Endlosschleifen

MIN_VOLUME = float(os.getenv("MIN_VOLUME", "10000"))
MAX_VOLUME = float(os.getenv("MAX_VOLUME", "200000"))
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H") or "1000")
MAX_VOLUME_24H = float(os.getenv("MAX_VOLUME_24H") or "0")  # 0 = keine Obergrenze
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_list(value):
    """Gamma liefert outcomes/outcomePrices oft als JSON-String."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def fetch_open_events():
    """Lädt alle offenen Events über die Cursor-Schnittstelle (/events/keyset).
    Polymarket filtert schon serverseitig grob nach Volumen (mit etwas Spielraum),
    die exakte Prüfung über alle Antwortmöglichkeiten passiert danach lokal."""
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-scanner/1.0"
    events, cursor = [], None

    for _ in range(MAX_PAGES):
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": PAGE_SIZE,
            "volume_min": int(MIN_VOLUME * 0.8),
            "volume_max": int(MAX_VOLUME * 1.2),
        }
        if cursor:
            params["after_cursor"] = cursor

        resp = None
        for attempt in range(4):
            try:
                resp = session.get(GAMMA_URL, params=params, timeout=30)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == 3:
                    raise
                print(f"Fehler ({exc}), neuer Versuch …", file=sys.stderr)
                time.sleep(2 ** attempt)

        data = resp.json()
        page = data.get("events", []) if isinstance(data, dict) else data
        if not page:
            break
        events.extend(page)
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor:
            break
        time.sleep(0.1)

    return events


def event_total_volume(event):
    """Gesamtvolumen = Summe aller Märkte (Antwortmöglichkeiten) des Events."""
    markets = event.get("markets") or []
    summed = sum(to_float(m.get("volumeNum", m.get("volume"))) for m in markets)
    # Falls Märkte fehlen, das Volumen auf Event-Ebene nehmen
    return summed if markets else to_float(event.get("volume"))


def event_volume_24h(event):
    """24h-Volumen des Events (Summe über alle Antwortmöglichkeiten)."""
    if event.get("volume24hr") is not None:
        return to_float(event.get("volume24hr"))
    return sum(to_float(m.get("volume24hr")) for m in event.get("markets") or [])


def in_24h_range(v24):
    if v24 < MIN_VOLUME_24H:
        return False
    return MAX_VOLUME_24H <= 0 or v24 <= MAX_VOLUME_24H


def build_rows(events):
    rows = []
    seen = set()
    for ev in events:
        ev_id = ev.get("id")
        if ev_id in seen:
            continue
        seen.add(ev_id)

        total = event_total_volume(ev)
        if not (MIN_VOLUME <= total <= MAX_VOLUME):
            continue
        v24 = event_volume_24h(ev)
        if not in_24h_range(v24):
            continue

        markets = ev.get("markets") or []
        open_markets = [m for m in markets if not m.get("closed")]

        # Top-Antwort (höchste "Yes"-Wahrscheinlichkeit) als Schnellinfo
        top_answer, top_price = "", ""
        for m in open_markets:
            prices = parse_list(m.get("outcomePrices"))
            if prices:
                p = to_float(prices[0])
                if top_price == "" or p > top_price:
                    top_price = p
                    top_answer = m.get("groupItemTitle") or m.get("question", "")

        tags = ", ".join(t.get("label", "") for t in (ev.get("tags") or []))

        rows.append({
            "event_title": ev.get("title", ""),
            "total_volume_usd": round(total, 2),
            "volume_24h_usd": round(v24, 2),
            "liquidity_usd": round(to_float(ev.get("liquidity")), 2),
            "num_answers": len(markets),
            "num_open_answers": len(open_markets),
            "top_answer": top_answer,
            "top_answer_price": top_price,
            "end_date": ev.get("endDate", ""),
            "tags": tags,
            "url": f"https://polymarket.com/event/{ev.get('slug', '')}",
            "event_id": ev_id,
        })

    rows.sort(key=lambda r: r["volume_24h_usd"], reverse=True)  # aktivste zuerst
    return rows


def rows_to_csv(rows):
    buf = io.StringIO()
    fields = list(rows[0].keys()) if rows else ["event_title"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    # utf-8-sig, damit Excel Umlaute korrekt anzeigt
    return buf.getvalue().encode("utf-8-sig")


def rows_to_html(rows, stamp):
    """Tabelle als HTML: öffnet in jedem Browser, Links klickbar,
    Spalten per Klick auf die Überschrift sortierbar, mit Suchfeld."""
    headers = [
        ("Event", "event_title"), ("24h-Volumen $", "volume_24h_usd"),
        ("Gesamtvolumen $", "total_volume_usd"), ("Liquidität $", "liquidity_usd"),
        ("Antworten", "num_answers"), ("Top-Antwort", "top_answer"),
        ("Preis", "top_answer_price"), ("Ende", "end_date"), ("Tags", "tags"),
    ]
    numeric = {"total_volume_usd", "volume_24h_usd", "liquidity_usd",
               "num_answers", "top_answer_price"}

    body = []
    for r in rows:
        cells = []
        for _, key in headers:
            val = r.get(key, "")
            if key == "event_title":
                cells.append(f'<td><a href="{html.escape(r["url"])}" target="_blank">'
                             f'{html.escape(str(val))}</a></td>')
            elif key in numeric and val != "":
                fmt = f"{val:.2f}" if key == "top_answer_price" else f"{val:,.0f}"
                cells.append(f'<td class="n" data-v="{val}">{fmt}</td>')
            elif key == "end_date":
                cells.append(f"<td>{html.escape(str(val)[:10])}</td>")
            else:
                cells.append(f"<td>{html.escape(str(val))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")

    head = "".join(f"<th onclick=\"sortBy({i})\">{h}</th>"
                   for i, (h, _) in enumerate(headers))

    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket-Scan {stamp}</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:16px;background:#fafafa}}
 input{{padding:8px;width:100%;max-width:400px;margin-bottom:12px;font-size:15px}}
 table{{border-collapse:collapse;width:100%;background:#fff;font-size:14px}}
 th,td{{padding:6px 10px;border-bottom:1px solid #e5e5e5;text-align:left}}
 th{{background:#222;color:#fff;cursor:pointer;position:sticky;top:0}}
 td.n{{text-align:right;font-variant-numeric:tabular-nums}}
 tr:hover{{background:#f0f6ff}}
 a{{color:#0b5fff;text-decoration:none}} a:hover{{text-decoration:underline}}
</style></head><body>
<h2>Polymarket-Scan {stamp} UTC – {len(rows)} Events</h2>
<input id="q" placeholder="Suchen (Titel, Tag, Antwort) …" oninput="filter()">
<table id="t"><thead><tr>{head}</tr></thead><tbody>
{''.join(body)}
</tbody></table>
<script>
let dir={{}};
function sortBy(c){{
 const tb=document.querySelector('#t tbody');const rows=[...tb.rows];
 dir[c]=!dir[c];
 rows.sort((a,b)=>{{
  const x=a.cells[c],y=b.cells[c];
  const vx=x.dataset.v!==undefined?parseFloat(x.dataset.v):x.innerText.toLowerCase();
  const vy=y.dataset.v!==undefined?parseFloat(y.dataset.v):y.innerText.toLowerCase();
  return (vx>vy?1:vx<vy?-1:0)*(dir[c]?-1:1);
 }});
 rows.forEach(r=>tb.appendChild(r));
}}
function filter(){{
 const q=document.getElementById('q').value.toLowerCase();
 for(const r of document.querySelectorAll('#t tbody tr'))
  r.style.display=r.innerText.toLowerCase().includes(q)?'':'none';
}}
</script></body></html>""".encode("utf-8")


def send_document(filename, content, mime, caption=""):
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        data={"chat_id": CHAT_ID, "caption": caption},
        files={"document": (filename, content, mime)},
        timeout=60,
    )
    if not resp.ok:
        sys.exit(f"Telegram-Fehler {resp.status_code}: {resp.text}")


def send_to_telegram(csv_bytes, html_bytes, count, stamp):
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt.")

    max24 = f"${MAX_VOLUME_24H:,.0f}" if MAX_VOLUME_24H > 0 else "∞"
    caption = (
        f"📊 Polymarket-Scan {stamp} UTC\n"
        f"{count} Events · Gesamtvolumen ${MIN_VOLUME:,.0f} – ${MAX_VOLUME:,.0f} · "
        f"24h-Volumen ${MIN_VOLUME_24H:,.0f} – {max24}\n"
        f"Sortiert nach 24h-Volumen (aktivste zuerst).\n"
        f"👉 Die .html-Datei im Browser öffnen: Links sind klickbar."
    )
    send_document(f"polymarket_{stamp}.html", html_bytes, "text/html", caption)
    send_document(f"polymarket_{stamp}.csv", csv_bytes, "text/csv")
    print("HTML + CSV an Telegram gesendet.")


def main():
    events = fetch_open_events()
    print(f"{len(events)} Events geladen.")
    rows = build_rows(events)
    print(f"{len(rows)} Events im Volumenbereich.")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    csv_bytes = rows_to_csv(rows)
    html_bytes = rows_to_html(rows, stamp)
    with open("polymarket_scan.csv", "wb") as f:  # auch als Artefakt in GitHub
        f.write(csv_bytes)
    with open("polymarket_scan.html", "wb") as f:
        f.write(html_bytes)

    send_to_telegram(csv_bytes, html_bytes, len(rows), stamp)


if __name__ == "__main__":
    main()
