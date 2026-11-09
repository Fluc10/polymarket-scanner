"""
Facticity / ArAIstotle-Anbindung – gezielte Checks für die Märkte aus dem Scan
-----------------------------------------------------------------------------
Ablauf:
  1) Aus dem Scan werden Märkte ausgewählt (pro Event die Favoriten-Antwort,
     ohne Extrempreise unter 3 % / über 97 %).
  2) Feed (GET /polymarket/araistotle-insights/, 1 Credit): was dort schon
     analysiert ist, wird direkt übernommen.
  3) Für die restlichen Märkte gezielt POST /araistotle-insight/:
       vorhandene Analyse = 2 Credits, neue Analyse = 10 Credits,
       Status-Abfrage = kostenlos.
     Neue Analysen dauern oft länger, als Facticitys Server auf eine Antwort
     wartet (Fehler 504). Die Analyse läuft dann trotzdem weiter – das Skript
     wartet und holt sie ab, sobald sie fertig ist.

Umgebungsvariablen:
  FACTICITY_API_KEY      (Secret, Pflicht)
  MAX_INSIGHTS           (Default 20)  – max. Märkte pro Lauf
  MAX_NEW_GENERATIONS    (Default 5)   – max. neue Analysen à 10 Credits pro Lauf
  ANALYZE_MODE           (Default "top") – "top" = Favorit pro Event, "all" = alle Antworten
  SELECT_ORDER           (Default "niche") – "niche" = kleinste Events zuerst,
                                             "top" = größte zuerst, "random" = Zufall
  PRICE_MIN / PRICE_MAX  (Default 0.03 / 0.97) – Extrempreise überspringen
  WAIT_MINUTES           (Default 15)  – wie lange auf neue Analysen gewartet wird
  USE_FEED               (Default "true") – Feed vorher nutzen, um Credits zu sparen
"""

import json
import os
import re
import time

import requests

API = "https://api.facticity.ai"
API_KEY = os.getenv("FACTICITY_API_KEY", "").strip()
MAX_INSIGHTS = int(os.getenv("MAX_INSIGHTS") or "20")
MAX_NEW_GENERATIONS = int(os.getenv("MAX_NEW_GENERATIONS") or "5")
ANALYZE_MODE = (os.getenv("ANALYZE_MODE") or "top").lower()
SELECT_ORDER = (os.getenv("SELECT_ORDER") or "niche").lower()
PRICE_MIN = float(os.getenv("PRICE_MIN") or "0.03")
PRICE_MAX = float(os.getenv("PRICE_MAX") or "0.97")
WAIT_MINUTES = float(os.getenv("WAIT_MINUTES") or "15")
USE_FEED = (os.getenv("USE_FEED") or "true").lower() == "true"
POLL_SECONDS = 30
QUEUED = "QUEUED"  # Marker: Analyse wurde angestoßen, ist aber noch nicht fertig


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def market_yes_price(market):
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    return _f(raw[0]) if isinstance(raw, list) and raw else None


def parse_probability(odds):
    """Wahrscheinlichkeit aus Zahl, Prozent-String, Objekt oder Liste → 0–1."""
    if odds is None or isinstance(odds, bool):
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


def extract(result):
    """Wahrscheinlichkeit, Signal und Begründung aus einer Antwort ziehen."""
    if not isinstance(result, dict):
        return None, "", ""
    inner = result.get("insight") if isinstance(result.get("insight"), dict) else {}
    merged = {**inner, **result}
    prob = parse_probability(_first(
        merged, "araistotle_odds", "araistotle_probability", "araistotle_prob",
        "ai_probability", "probability", "odds"))
    signal = str(_first(merged, "signal", "mispricing_signal", "recommendation") or "")
    rationale = str(_first(merged, "rationale", "summary", "analysis") or "")
    return prob, signal, rationale


def select_markets(events, in_range):
    picked = []
    for ev in events:
        if not in_range(ev):
            continue
        open_markets = []
        for m in ev.get("markets") or []:
            p = market_yes_price(m)
            if (not m.get("closed") and m.get("id") is not None
                    and p is not None and PRICE_MIN <= p <= PRICE_MAX):
                open_markets.append(m)
        if not open_markets:
            continue
        chosen = (open_markets if ANALYZE_MODE == "all"
                  else [max(open_markets, key=lambda m: market_yes_price(m) or 0)])
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
    if SELECT_ORDER == "top":        # größtes Volumen zuerst
        picked.sort(key=lambda r: r["event_volume"], reverse=True)
    elif SELECT_ORDER == "random":   # jeder Lauf eine andere Stichprobe
        import random
        random.shuffle(picked)
    else:                            # "niche": kleinstes Volumen zuerst
        picked.sort(key=lambda r: r["event_volume"])
    return picked[:MAX_INSIGHTS]


class Facticity:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"X-API-KEY": API_KEY,
                               "Content-Type": "application/json"})

    def credits(self):
        try:
            r = self.s.get(f"{API}/get-credits", timeout=30)
            if not r.ok:
                print(f"get-credits Fehler {r.status_code}: {r.text[:200]}")
                return None
            return r.json().get("credits_left")
        except requests.RequestException as e:
            print(f"get-credits Fehler: {e}")
            return None

    def feed(self):
        try:
            r = self.s.get(f"{API}/polymarket/araistotle-insights/",
                           params={"limit": 50, "compact": "false"}, timeout=120)
            if not r.ok:
                print(f"Feed Fehler {r.status_code}: {r.text[:300]}")
                return {}
            items = r.json().get("markets") or []
        except (requests.RequestException, ValueError) as e:
            print(f"Feed Fehler: {e}")
            return {}
        out = {}
        for it in items:
            if isinstance(it, dict):
                mid = _first(it, "polymarket_market_id", "market_id", "id")
                if mid is not None:
                    out[str(mid)] = it
        print(f"Feed: {len(out)} Analysen geladen.")
        return out

    def status(self, market_id):
        """Kostenlos. Gibt {} zurück, wenn die Abfrage hängt oder fehlschlägt."""
        try:
            r = self.s.get(f"{API}/asp/prediction-insight/{market_id}/status",
                           params={"source": "polymarket"}, timeout=15)
            return r.json() if r.ok else {}
        except (requests.RequestException, ValueError):
            return {}

    def insight(self, market_id):
        """Gibt (Antwort, Fehlertext) zurück – bricht nie den Lauf ab.
        Bei Time-out (502/503/504 oder keine Antwort) läuft die Analyse bei
        Facticity weiter → Rückgabe (None, QUEUED)."""
        try:
            r = self.s.post(f"{API}/araistotle-insight/",
                            json={"polymarket_market_id": market_id, "refresh": False},
                            timeout=90)
        except requests.Timeout:
            return None, QUEUED
        except requests.RequestException as e:
            return None, f"Netzwerkfehler: {e}"
        if r.status_code in (502, 503, 504):
            return None, QUEUED
        if not r.ok:
            return None, f"Fehler {r.status_code}: {r.text[:150]}"
        try:
            data = r.json()
        except ValueError:
            return None, f"Ungültige Antwort: {r.text[:150]}"
        if data.get("araistotle_odds") is None and not data.get("ready"):
            return None, QUEUED  # angenommen, aber noch nicht fertig
        return data, ""


def analyze(events, in_range):
    if not API_KEY:
        print("Kein FACTICITY_API_KEY gesetzt – Analyse übersprungen.")
        return []

    fc = Facticity()
    print(f"Facticity-Credits vor dem Lauf: {fc.credits()}")

    candidates = select_markets(events, in_range)
    print(f"{len(candidates)} Märkte aus deinem Scan werden geprüft.")

    results = {}

    # 1) Feed nutzen (1 Credit) – was schon drin ist, muss nicht einzeln geholt werden
    if USE_FEED and candidates:
        feed = fc.feed()
        for c in candidates:
            if c["market_id"] in feed:
                results[c["market_id"]] = feed[c["market_id"]]
                c["ai_status"] = "aus Feed"

    # 2) Rest gezielt abfragen
    waiting, new_generations = [], 0
    for c in candidates:
        mid = c["market_id"]
        if mid in results:
            continue
        st = fc.status(mid)
        stage = (st.get("status") or "").lower()
        if st.get("ready"):
            res, err = fc.insight(mid)            # fertige Analyse holen (2 Credits)
            if res:
                results[mid] = res
                c["ai_status"] = "vorhanden"
            elif err == QUEUED:
                waiting.append(c)
            else:
                c["ai_status"] = err
        elif stage == "resolved":
            c["ai_status"] = "Markt bereits aufgelöst"
        elif st.get("exists"):
            waiting.append(c)                     # läuft bereits bei Facticity
        elif new_generations < MAX_NEW_GENERATIONS:
            res, err = fc.insight(mid)            # neue Analyse anstoßen (10 Credits)
            new_generations += 1
            if res:
                results[mid] = res
                c["ai_status"] = "neu erzeugt"
            elif err == QUEUED:
                waiting.append(c)
            else:
                c["ai_status"] = err
        else:
            c["ai_status"] = "übersprungen (Limit neue Analysen)"
        time.sleep(0.3)

    # 3) Auf laufende Analysen warten (Status-Abfragen sind kostenlos)
    deadline = time.time() + WAIT_MINUTES * 60
    while waiting and time.time() < deadline:
        print(f"Warte auf {len(waiting)} Analysen …")
        time.sleep(POLL_SECONDS)
        still = []
        for c in waiting:
            if fc.status(c["market_id"]).get("ready"):
                res, err = fc.insight(c["market_id"])
                if res:
                    results[c["market_id"]] = res
                    c["ai_status"] = "neu erzeugt"
                    continue
            still.append(c)
        waiting = still
    for c in waiting:
        c["ai_status"] = "Analyse läuft noch – kommt im nächsten Lauf"

    # 4) Ergebnisse
    rows = []
    for c in candidates:
        res = results.get(c["market_id"])
        ai_prob, signal, rationale = extract(res)
        price = c["polymarket_price"]
        edge = (ai_prob - price) if (ai_prob is not None and price is not None) else None
        rows.append({
            "event_title": c["event_title"],
            "answer": c["answer"],
            "polymarket_price": round(price, 3) if price is not None else "",
            "araistotle_prob": round(ai_prob, 3) if ai_prob is not None else "",
            "edge": round(edge, 3) if edge is not None else "",
            "ai_status": c.get("ai_status", ""),
            "signal": signal,
            "rationale": rationale[:600],
            "url": c["url"],
            "market_id": c["market_id"],
            "raw": json.dumps(res, ensure_ascii=False)[:1500] if res else "",
        })

    rows.sort(key=lambda r: abs(r["edge"]) if r["edge"] != "" else -1, reverse=True)
    print(f"{sum(r['edge'] != '' for r in rows)} von {len(rows)} Märkten mit Ergebnis.")
    print(f"Facticity-Credits nach dem Lauf: {fc.credits()}")
    return rows


# ---------- Ausgabe ----------

def insights_to_html(rows, stamp):
    import html as h
    cols = [("Event", "event_title"), ("Antwort", "answer"),
            ("Polymarket", "polymarket_price"), ("ArAIstotle", "araistotle_prob"),
            ("Edge", "edge"), ("Status", "ai_status"), ("Signal", "signal"),
            ("Begründung", "rationale")]
    body = []
    for r in rows:
        cells = []
        for _, k in cols:
            v = r.get(k, "")
            if k == "event_title":
                cells.append(f'<td><a href="{h.escape(r["url"])}" target="_blank">'
                             f'{h.escape(str(v))}</a></td>')
            elif k == "edge" and v != "":
                color = "#0a7a2f" if v > 0 else "#b3261e"
                cells.append(f'<td class="n" data-v="{v}" style="color:{color};'
                             f'font-weight:600">{v*100:+.1f}%</td>')
            elif k in ("polymarket_price", "araistotle_prob") and v != "":
                cells.append(f'<td class="n" data-v="{v}">{v*100:.1f}%</td>')
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
    import html as h
    with_edge = [r for r in rows if r["edge"] != ""]
    if not with_edge:
        pending = sum("läuft noch" in r["ai_status"] for r in rows)
        msg = "🤖 ArAIstotle: in diesem Lauf noch keine fertigen Ergebnisse."
        if pending:
            msg += f"\n⏳ {pending} Analysen laufen noch bei Facticity und kommen im nächsten Lauf."
        return msg
    lines = ["🤖 <b>ArAIstotle-Check deiner Märkte</b>", ""]
    for r in with_edge[:top_n]:
        lines.append(
            f'{"🟢" if r["edge"] > 0 else "🔴"} <a href="{h.escape(r["url"])}">'
            f'{h.escape(str(r["event_title"])[:70])}</a>\n'
            f'   {h.escape(str(r["answer"])[:50])}: Markt {r["polymarket_price"]*100:.0f}% · '
            f'KI {r["araistotle_prob"]*100:.0f}% · Edge {r["edge"]*100:+.0f}%')
    pending = sum("läuft noch" in r["ai_status"] for r in rows)
    lines.append(f"\n{len(with_edge)} von {len(rows)} Märkten geprüft."
                 + (f" ⏳ {pending} laufen noch." if pending else ""))
    return "\n".join(lines)
