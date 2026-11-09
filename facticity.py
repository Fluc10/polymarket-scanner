"""
Facticity / ArAIstotle-Anbindung
--------------------------------
Holt für ausgewählte Polymarket-Märkte die ArAIstotle-Einschätzung
(eigene Wahrscheinlichkeit + Begründung) und vergleicht sie mit dem Marktpreis.

Kosten laut Facticity-API:
  - vorhandene Analyse abrufen:   2 Credits
  - neue Analyse erzeugen:       10 Credits
  - Status abfragen:              kostenlos

Umgebungsvariablen:
  FACTICITY_API_KEY      (Secret, Pflicht für diesen Schritt)
  MAX_INSIGHTS           (Default 20)  – max. Märkte pro Lauf
  MAX_NEW_GENERATIONS    (Default 5)   – max. neue Analysen (à 10 Credits) pro Lauf
  ANALYZE_MODE           (Default "top") – "top" = pro Event nur die Favoriten-Antwort,
                                           "all" = alle offenen Antworten
  WAIT_MINUTES           (Default 10)  – wie lange auf neue Analysen gewartet wird
"""

import json
import os
import re
import sys
import time

import requests

API = "https://api.facticity.ai"
API_KEY = os.getenv("FACTICITY_API_KEY", "")
MAX_INSIGHTS = int(os.getenv("MAX_INSIGHTS", "20"))
MAX_NEW_GENERATIONS = int(os.getenv("MAX_NEW_GENERATIONS", "5"))
ANALYZE_MODE = os.getenv("ANALYZE_MODE", "top").lower()
WAIT_MINUTES = float(os.getenv("WAIT_MINUTES", "10"))
POLL_SECONDS = 30


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _prices(market):
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    return raw if isinstance(raw, list) else []


def market_yes_price(market):
    prices = _prices(market)
    return _f(prices[0]) if prices else None


def parse_probability(odds):
    """araistotle_odds kann je nach Antwort unterschiedlich aussehen
    (Zahl, Prozent-String, Objekt, Liste). Liefert 0–1 oder None."""
    if odds is None:
        return None
    if isinstance(odds, bool):
        return None
    if isinstance(odds, (int, float)):
        v = float(odds)
        return v / 100 if v > 1 else v
    if isinstance(odds, str):
        m = re.search(r"-?\d+(\.\d+)?", odds)
        if not m:
            return None
        v = float(m.group())
        return v / 100 if ("%" in odds or v > 1) else v
    if isinstance(odds, dict):
        for key in ("yes", "Yes", "YES", "probability", "prob", "p_yes",
                    "araistotle", "odds", "value"):
            if key in odds:
                p = parse_probability(odds[key])
                if p is not None:
                    return p
        for v in odds.values():
            p = parse_probability(v)
            if p is not None:
                return p
    if isinstance(odds, list) and odds:
        return parse_probability(odds[0])
    return None


def select_markets(events, in_range):
    """Wählt die zu analysierenden Märkte (Antwortmöglichkeiten) aus."""
    picked = []
    for ev in events:
        if not in_range(ev):
            continue
        open_markets = [m for m in (ev.get("markets") or [])
                        if not m.get("closed") and m.get("id")]
        if not open_markets:
            continue
        if ANALYZE_MODE == "all":
            chosen = open_markets
        else:
            chosen = [max(open_markets, key=lambda m: market_yes_price(m) or 0)]
        for m in chosen:
            picked.append({
                "event_title": ev.get("title", ""),
                "answer": m.get("groupItemTitle") or m.get("question", ""),
                "market_id": str(m["id"]),
                "polymarket_price": market_yes_price(m),
                "event_volume": sum(_f(x.get("volumeNum", x.get("volume"))) or 0
                                    for x in ev.get("markets") or []),
                "url": f"https://polymarket.com/event/{ev.get('slug', '')}",
            })
    # Größte Events zuerst, dann auf MAX_INSIGHTS begrenzen
    picked.sort(key=lambda r: r["event_volume"], reverse=True)
    return picked[:MAX_INSIGHTS]


class Facticity:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"X-API-KEY": API_KEY,
                               "Content-Type": "application/json"})

    def credits(self):
        try:
            r = self.s.get(f"{API}/get-credits", timeout=30)
            return r.json().get("credits_left") if r.ok else None
        except requests.RequestException:
            return None

    def status(self, market_id):
        """Kostenlos: zeigt, ob eine Analyse existiert / fertig ist."""
        try:
            r = self.s.get(f"{API}/asp/prediction-insight/{market_id}/status",
                           params={"source": "polymarket"}, timeout=30)
            return r.json() if r.ok else {}
        except (requests.RequestException, ValueError):
            return {}

    def insight(self, market_id):
        """Kostenpflichtig: holt (2 Credits) oder erzeugt (10 Credits) eine Analyse."""
        r = self.s.post(f"{API}/araistotle-insight/",
                        json={"polymarket_market_id": market_id, "refresh": False},
                        timeout=120)
        if r.status_code == 401:
            sys.exit("Facticity: API-Key ungültig oder keine Credits mehr.")
        r.raise_for_status()
        return r.json()


def analyze(events, in_range):
    if not API_KEY:
        print("Kein FACTICITY_API_KEY gesetzt – Analyse übersprungen.")
        return []

    fc = Facticity()
    print(f"Facticity-Credits vor dem Lauf: {fc.credits()}")

    candidates = select_markets(events, in_range)
    print(f"{len(candidates)} Märkte für ArAIstotle ausgewählt.")

    results, waiting, new_generations = {}, [], 0

    # 1) Status prüfen: fertige Analysen holen, fehlende (begrenzt) erzeugen
    for c in candidates:
        st = fc.status(c["market_id"])
        stage = (st.get("status") or "").lower()
        if st.get("ready"):
            results[c["market_id"]] = fc.insight(c["market_id"])
        elif stage == "resolved":
            c["ai_status"] = "Markt bereits aufgelöst"
        elif st.get("exists"):
            waiting.append(c)  # Analyse läuft bereits
        elif new_generations < MAX_NEW_GENERATIONS:
            resp = fc.insight(c["market_id"])
            new_generations += 1
            if resp.get("ready"):
                results[c["market_id"]] = resp
            else:
                waiting.append(c)
        else:
            c["ai_status"] = "übersprungen (Limit neue Analysen)"
        time.sleep(0.3)

    # 2) Auf laufende Analysen warten (Status-Abfragen sind kostenlos)
    deadline = time.time() + WAIT_MINUTES * 60
    while waiting and time.time() < deadline:
        print(f"Warte auf {len(waiting)} Analysen …")
        time.sleep(POLL_SECONDS)
        still = []
        for c in waiting:
            if fc.status(c["market_id"]).get("ready"):
                results[c["market_id"]] = fc.insight(c["market_id"])
            else:
                still.append(c)
        waiting = still
    for c in waiting:
        c["ai_status"] = "Analyse läuft noch (nächster Lauf)"

    # 3) Ergebnisse zusammenführen
    rows = []
    for c in candidates:
        res = results.get(c["market_id"])
        ai_prob = parse_probability(res.get("araistotle_odds")) if res else None
        price = c["polymarket_price"]
        edge = (ai_prob - price) if (ai_prob is not None and price is not None) else None
        rows.append({
            "event_title": c["event_title"],
            "answer": c["answer"],
            "polymarket_price": round(price, 3) if price is not None else "",
            "araistotle_prob": round(ai_prob, 3) if ai_prob is not None else "",
            "edge": round(edge, 3) if edge is not None else "",
            "ai_status": c.get("ai_status") or (res.get("source") if res else ""),
            "rationale": (res.get("rationale") or "")[:600] if res else "",
            "araistotle_odds_raw": json.dumps(res.get("araistotle_odds"),
                                              ensure_ascii=False) if res else "",
            "url": c["url"],
            "market_id": c["market_id"],
        })

    # Größter Unterschied KI vs. Markt zuerst
    rows.sort(key=lambda r: abs(r["edge"]) if r["edge"] != "" else -1, reverse=True)
    print(f"Facticity-Credits nach dem Lauf: {fc.credits()}")
    return rows


# ---------- Ausgabe ----------

def insights_to_html(rows, stamp):
    import html as h
    cols = [("Event", "event_title"), ("Antwort", "answer"),
            ("Polymarket", "polymarket_price"), ("ArAIstotle", "araistotle_prob"),
            ("Edge", "edge"), ("Status", "ai_status"), ("Begründung", "rationale")]
    body = []
    for r in rows:
        cells = []
        for _, k in cols:
            v = r.get(k, "")
            if k == "event_title":
                cells.append(f'<td><a href="{h.escape(r["url"])}" target="_blank">{h.escape(str(v))}</a></td>')
            elif k in ("polymarket_price", "araistotle_prob", "edge") and v != "":
                color = ""
                if k == "edge":
                    color = ' style="color:#0a7a2f;font-weight:600"' if v > 0 else ' style="color:#b3261e;font-weight:600"'
                cells.append(f'<td class="n" data-v="{v}"{color}>{v*100:+.1f}%</td>' if k == "edge"
                             else f'<td class="n" data-v="{v}">{v*100:.1f}%</td>')
            elif k == "rationale":
                cells.append(f'<td class="r">{h.escape(str(v))}</td>')
            else:
                cells.append(f"<td>{h.escape(str(v))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    head = "".join(f'<th onclick="sortBy({i})">{t}</th>' for i, (t, _) in enumerate(cols))
    return f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ArAIstotle-Analyse {stamp}</title><style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#fafafa}}
input{{padding:8px;width:100%;max-width:400px;margin-bottom:12px;font-size:15px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:14px}}
th,td{{padding:6px 10px;border-bottom:1px solid #e5e5e5;text-align:left;vertical-align:top}}
th{{background:#222;color:#fff;cursor:pointer;position:sticky;top:0}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.r{{max-width:520px;font-size:13px;color:#444}}
a{{color:#0b5fff;text-decoration:none}}</style></head><body>
<h2>ArAIstotle vs. Polymarket – {stamp} UTC</h2>
<p>Edge = ArAIstotle-Wahrscheinlichkeit minus Marktpreis (Yes). Positiv = KI sieht „Yes“ höher als der Markt.</p>
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


def summary_message(rows, top_n=10):
    """Telegram-Text (HTML) mit den größten Abweichungen, Links klickbar."""
    import html as h
    with_edge = [r for r in rows if r["edge"] != ""]
    if not with_edge:
        return "🤖 ArAIstotle: noch keine fertigen Analysen in diesem Lauf."
    lines = ["🤖 <b>ArAIstotle vs. Markt – größte Abweichungen</b>", ""]
    for r in with_edge[:top_n]:
        lines.append(
            f'{"🟢" if r["edge"] > 0 else "🔴"} <a href="{h.escape(r["url"])}">'
            f'{h.escape(r["event_title"][:70])}</a>\n'
            f'   {h.escape(str(r["answer"])[:50])}: Markt {r["polymarket_price"]*100:.0f}% · '
            f'KI {r["araistotle_prob"]*100:.0f}% · Edge {r["edge"]*100:+.0f}%')
    return "\n".join(lines)
