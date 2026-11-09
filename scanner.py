"""
Polymarket Scanner
------------------
Holt alle offenen Polymarket-Events über die öffentliche Gamma-API,
filtert nach Gesamtvolumen (Summe über ALLE Antwortmöglichkeiten/Märkte
eines Events) und schickt das Ergebnis als HTML (klickbare Links) + CSV per Telegram.
Optional: ArAIstotle-Check der gefundenen Märkte (siehe facticity.py).

Konfiguration über Umgebungsvariablen (in GitHub als Secrets/Variables):
  TELEGRAM_BOT_TOKEN   (Secret, Pflicht)  – Token von @BotFather
  TELEGRAM_CHAT_ID     (Secret, Pflicht)  – Chat-ID, an die gesendet wird
  MIN_VOLUME           (optional, Default 10000)
  MAX_VOLUME           (optional, Default 200000)
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

GAMMA_URL = "https://gamma-api.polymarket.com/events"
PAGE_SIZE = 100
MAX_PAGES = 200  # Sicherheitsgrenze gegen Endlosschleifen

MIN_VOLUME = float(os.getenv("MIN_VOLUME", "10000"))
MAX_VOLUME = float(os.getenv("MAX_VOLUME", "200000"))
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
    """Lädt alle offenen Events seitenweise."""
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-scanner/1.0"
    events, offset = [], 0

    for _ in range(MAX_PAGES):
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        for attempt in range(4):
            try:
                resp = session.get(GAMMA_URL, params=params, timeout=30)
                if resp.status_code == 429:
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
        # Kompatibel mit Listen-Antwort und {"data": [...]}-Antwort
        page = data.get("data", []) if isinstance(data, dict) else data
        if not page:
            break
        events.extend(page)
        # Die API liefert evtl. weniger als PAGE_SIZE pro Seite (z. B. max. 100),
        # daher erst aufhören, wenn eine Seite leer ist
        offset += len(page)
        time.sleep(0.2)

    return events


def event_total_volume(event):
    """Gesamtvolumen = Summe aller Märkte (Antwortmöglichkeiten) des Events."""
    markets = event.get("markets") or []
    summed = sum(to_float(m.get("volumeNum", m.get("volume"))) for m in markets)
    # Falls Märkte fehlen, das Volumen auf Event-Ebene nehmen
    return summed if markets else to_float(event.get("volume"))


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
            "volume_24h_usd": round(to_float(ev.get("volume24hr")), 2),
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

    rows.sort(key=lambda r: r["total_volume_usd"], reverse=True)
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
        ("Event", "event_title"), ("Gesamtvolumen $", "total_volume_usd"),
        ("24h $", "volume_24h_usd"), ("Liquidität $", "liquidity_usd"),
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

    caption = (
        f"📊 Polymarket-Scan {stamp} UTC\n"
        f"{count} Events mit Gesamtvolumen "
        f"${MIN_VOLUME:,.0f} – ${MAX_VOLUME:,.0f}\n"
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

    # ---- ArAIstotle / Facticity-Analyse (nur wenn API-Key gesetzt) ----
    import facticity
    in_range = lambda ev: MIN_VOLUME <= event_total_volume(ev) <= MAX_VOLUME
    insights = facticity.analyze(events, in_range)
    if insights:
        ins_csv = rows_to_csv(insights)
        ins_html = facticity.insights_to_html(insights, stamp)
        with open("araistotle_insights.csv", "wb") as f:
            f.write(ins_csv)
        with open("araistotle_insights.html", "wb") as f:
            f.write(ins_html)
        send_message(facticity.summary_message(insights))
        send_document(f"araistotle_{stamp}.html", ins_html, "text/html",
                      "🤖 ArAIstotle-Analyse – im Browser öffnen")
        send_document(f"araistotle_{stamp}.csv", ins_csv, "text/csv")
        print("ArAIstotle-Analyse an Telegram gesendet.")


def send_message(text):
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=60,
    )
    if not resp.ok:
        print(f"Telegram-Fehler {resp.status_code}: {resp.text}", file=sys.stderr)


if __name__ == "__main__":
    main()
