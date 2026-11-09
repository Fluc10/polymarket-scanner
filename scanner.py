"""
Polymarket Scanner
------------------
Holt alle offenen Polymarket-Events über die öffentliche Gamma-API,
filtert nach Gesamtvolumen (Summe über ALLE Antwortmöglichkeiten/Märkte
eines Events) und schickt das Ergebnis als CSV per Telegram.

Konfiguration über Umgebungsvariablen (in GitHub als Secrets/Variables):
  TELEGRAM_BOT_TOKEN   (Secret, Pflicht)  – Token von @BotFather
  TELEGRAM_CHAT_ID     (Secret, Pflicht)  – Chat-ID, an die gesendet wird
  MIN_VOLUME           (optional, Default 10000)
  MAX_VOLUME           (optional, Default 200000)
"""

import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

GAMMA_URL = "https://gamma-api.polymarket.com/events"
PAGE_SIZE = 500
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
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
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


def send_to_telegram(csv_bytes, count):
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt.")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    caption = (
        f"📊 Polymarket-Scan {stamp} UTC\n"
        f"{count} Events mit Gesamtvolumen "
        f"${MIN_VOLUME:,.0f} – ${MAX_VOLUME:,.0f}"
    )
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        data={"chat_id": CHAT_ID, "caption": caption},
        files={"document": (f"polymarket_{stamp}.csv", csv_bytes, "text/csv")},
        timeout=60,
    )
    if not resp.ok:
        sys.exit(f"Telegram-Fehler {resp.status_code}: {resp.text}")
    print("CSV an Telegram gesendet.")


def main():
    events = fetch_open_events()
    print(f"{len(events)} Events geladen.")
    rows = build_rows(events)
    print(f"{len(rows)} Events im Volumenbereich.")

    csv_bytes = rows_to_csv(rows)
    with open("polymarket_scan.csv", "wb") as f:  # auch als Artefakt in GitHub
        f.write(csv_bytes)

    send_to_telegram(csv_bytes, len(rows))


if __name__ == "__main__":
    main()
