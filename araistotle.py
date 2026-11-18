"""
ArAIstotle (Facticity.AI) - eigene Wahrscheinlichkeit fuer Polymarket-Maerkte.

Spricht den Facticity-MCP-Server direkt per HTTP (JSON-RPC, "Streamable HTTP") an
und ruft das Tool `araistotle_insight` auf.

Kosten (Facticity-Credits): vorhandene Analyse 2 Credits, neue Analyse 10 Credits.
Neue Analysen laufen asynchron - wir fragen nach ARAISTOTLE_WAIT Sekunden erneut nach.

Secret:   FACTICITY_API_KEY
Variablen (optional):
  FACTICITY_MCP_URL        eigene Server-Adresse (Standard: https://mcp.facticity.ai/mcp)
  ARAISTOTLE_MAX_PER_RUN   max. Maerkte pro Lauf (Standard 20)
  ARAISTOTLE_WAIT          max. Wartezeit auf neue Analysen in Sekunden (Standard 300)
  ARAISTOTLE_LOW_CREDITS   Warnung ab diesem Guthaben (Standard 60)

Test: python araistotle.py <polymarket_market_id>
"""
import json
import os
import sys
import time

import requests

API_KEY = os.environ.get("FACTICITY_API_KEY", "").strip()
URLS = [u for u in [os.environ.get("FACTICITY_MCP_URL", "").strip(),
                    "https://mcp.facticity.ai/mcp", "https://mcp.facticity.ai/"] if u]


def _int(name, default):
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return default


MAX_PER_RUN = _int("ARAISTOTLE_MAX_PER_RUN", 20)
WAIT = _int("ARAISTOTLE_WAIT", 300)
LOW_CREDITS = _int("ARAISTOTLE_LOW_CREDITS", 60)
PENDING = {"pending", "processing", "queued", "running", "generating", "in_progress", "started"}


class FacticityError(Exception):
    pass


def enabled():
    return bool(API_KEY) and MAX_PER_RUN > 0


def _auth_variants():
    return [{"X-API-KEY": API_KEY, "Authorization": f"Bearer {API_KEY}"},
            {"X-API-KEY": API_KEY},
            {"Authorization": f"Bearer {API_KEY}"}]


def _parse(resp):
    """JSON-RPC-Antwort aus JSON oder SSE (text/event-stream) lesen."""
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        last = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if "result" in msg or "error" in msg:
                    last = msg
        return last or {}
    try:
        return resp.json()
    except ValueError:
        return {}


class Client:
    """Minimaler MCP-Client: initialize + tools/call."""

    def __init__(self):
        self.url, self.headers, self.session = None, None, None
        self._id = 0

    def _post(self, url, headers, payload, timeout=60):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": "2025-03-26", **headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        return requests.post(url, json=payload, headers=h, timeout=timeout)

    def connect(self):
        errors = []
        for url in URLS:
            for headers in _auth_variants():
                self.session = None
                try:
                    r = self._post(url, headers, {
                        "jsonrpc": "2.0", "id": 0, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "polymarket-scanner", "version": "1"}}})
                except requests.RequestException as ex:
                    errors.append(f"{url}: {type(ex).__name__}")
                    break
                if r.status_code != 200:
                    errors.append(f"{url} [{'/'.join(headers)}]: HTTP {r.status_code}")
                    continue
                msg = _parse(r)
                if "result" not in msg:
                    errors.append(f"{url}: {str(msg.get('error'))[:120]}")
                    continue
                self.url, self.headers = url, headers
                self.session = r.headers.get("Mcp-Session-Id")
                try:
                    self._post(url, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                               timeout=20)
                except requests.RequestException:
                    pass
                print(f"Facticity verbunden: {url} (Auth: {'/'.join(headers)})")
                return True
        raise FacticityError("Keine Verbindung zu Facticity: " + "; ".join(errors[:6]))

    def call(self, tool, args, timeout=120):
        if not self.url:
            self.connect()
        self._id += 1
        for attempt in range(3):
            try:
                r = self._post(self.url, self.headers, {"jsonrpc": "2.0", "id": self._id,
                                                        "method": "tools/call",
                                                        "params": {"name": tool, "arguments": args}},
                               timeout=timeout)
            except requests.RequestException as ex:
                if attempt == 2:
                    raise FacticityError(f"{tool}: {ex}")
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (404, 400) and self.session and attempt == 0:
                self.url = None  # Session abgelaufen -> neu verbinden
                self.connect()
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise FacticityError(f"{tool}: HTTP {r.status_code} {r.text[:150]}")
            msg = _parse(r)
            if "error" in msg:
                raise FacticityError(f"{tool}: {str(msg['error'])[:200]}")
            res = msg.get("result") or {}
            if res.get("structuredContent"):
                return res["structuredContent"]
            for c in res.get("content") or []:
                if c.get("type") == "text":
                    try:
                        return json.loads(c["text"])
                    except ValueError:
                        return {"error": c["text"][:300]}
            return res
        raise FacticityError(f"{tool}: keine Antwort")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_answer(d, name):
    """Insight -> gleiches Format wie die Exa-Antworten (fuer Telegram, Log, Orderbuch)."""
    odds = _num(d.get("araistotle_odds"))
    sig = d.get("market_pricing_signal") or {}
    if odds is None:
        odds = _num(sig.get("araistotle_prob"))
    mkt = _num(sig.get("market_implied_prob"))
    if odds is None:
        return None
    p = round(odds * 100, 1)
    direction = str(sig.get("direction") or "")
    if direction == "fairly_priced" or (mkt is not None and abs(p - mkt * 100) < 2):
        signal = "fair"
    elif mkt is not None:
        signal = "unterbewertet" if p > mkt * 100 else "ueberbewertet"
    else:
        signal = "unklar"
    rri = (d.get("rri") or {}).get("score")
    return {
        "antwort": name, "focus": name, "signal": signal, "einschaetzung_prozent": p,
        "marktpreis_prozent": None if mkt is None else round(mkt * 100, 1),
        "begruendung": str(d.get("rationale") or "")[:1500],
        "rri": rri, "rfs": (d.get("rfs") or {}).get("score"),
        "quelle": d.get("source", ""), "credits": d.get("credits_charged"),
        "credits_left": d.get("credits_left"), "model": "araistotle",
    }


def analyze(items):
    """items: Liste (key, polymarket_market_id, antwort-name).
    Rueckgabe: ({key: antwort-dict}, info-dict mit credits_left / fehler / neu)."""
    info = {"credits_left": None, "fehler": "", "neu": 0, "vorhanden": 0, "offen": 0,
            "pending_keys": []}
    out = {}
    if not enabled() or not items:
        return out, info
    client = Client()
    try:
        client.connect()
    except FacticityError as ex:
        info["fehler"] = str(ex)
        print(ex)
        return out, info
    pending = []
    for key, mid, name in items[:MAX_PER_RUN]:
        try:
            d = client.call("araistotle_insight", {"polymarket_market_id": str(mid)})
        except FacticityError as ex:
            print("ArAIstotle-Fehler:", ex)
            info["fehler"] = str(ex)
            if "credit" in str(ex).lower():
                break
            continue
        if d.get("credits_left") is not None:
            info["credits_left"] = d["credits_left"]
        if d.get("error") or d.get("success") is False:
            msg = str(d.get("error") or d.get("message") or d)[:200]
            print(f"ArAIstotle {mid}: {msg}")
            if "credit" in msg.lower():
                info["fehler"] = "Zu wenig Facticity-Credits"
                break
            continue
        status = str(d.get("status") or d.get("insight_status") or "").lower()
        if d.get("ready") or status == "completed":
            a = to_answer(d, name)
            if a:
                out[key] = a
                info["vorhanden"] += 1
        elif status in PENDING or d.get("task_id") or not d.get("ready"):
            pending.append((key, mid, name))
            info["neu"] += 1
    # Neue Analysen laufen asynchron -> spaeter nochmal abholen
    deadline = time.time() + WAIT
    while pending and time.time() < deadline:
        time.sleep(min(60, max(5, deadline - time.time())))
        still = []
        for key, mid, name in pending:
            try:
                d = client.call("araistotle_insight", {"polymarket_market_id": str(mid)})
            except FacticityError as ex:
                print("ArAIstotle-Fehler:", ex)
                still.append((key, mid, name))
                continue
            if d.get("credits_left") is not None:
                info["credits_left"] = d["credits_left"]
            status = str(d.get("status") or d.get("insight_status") or "").lower()
            a = to_answer(d, name) if (d.get("ready") or status == "completed") else None
            if a:
                out[key] = a
            elif status in ("failed", "error", "resolved_skipped"):
                print(f"ArAIstotle {mid}: {status}")
            else:
                still.append((key, mid, name))
        pending = still
    info["offen"] = len(pending)
    info["pending_keys"] = [k for k, _, _ in pending]
    print(f"ArAIstotle: {len(out)}/{min(len(items), MAX_PER_RUN)} analysiert "
          f"({info['vorhanden']} vorhanden, {info['neu']} neu angestoßen, {info['offen']} noch offen), "
          f"Credits übrig: {info['credits_left']}")
    return out, info


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("FACTICITY_API_KEY fehlt.")
    mid = sys.argv[1] if len(sys.argv) > 1 else ""
    c = Client()
    c.connect()
    if mid:
        res, info = analyze([("test", mid, "Test")])
        print(json.dumps({"ergebnis": res, "info": info}, ensure_ascii=False, indent=2)[:3000])
    else:
        print(json.dumps(c.call("get_credits", {}), ensure_ascii=False)[:1000])
