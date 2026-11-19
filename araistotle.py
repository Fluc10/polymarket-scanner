"""
ArAIstotle (Facticity.AI) - eigene Wahrscheinlichkeit fuer Polymarket-Maerkte.

REST-API https://api.facticity.ai (Header X-API-KEY):
  GET  /get-credits                                  Guthaben (kostenlos)
  GET  /asp/prediction-insight/<id>/status?source=polymarket   Status (kostenlos)
  POST /araistotle-insight/  {polymarket_market_id}  Analyse holen / anstossen

Kosten (Facticity-Credits): vorhandene Analyse 2 Credits, neue Analyse 10 Credits.
Neue Analysen laufen asynchron - wir fragen nach ARAISTOTLE_WAIT Sekunden erneut nach.

Secret:   FACTICITY_API_KEY
Variablen (optional):
  ARAISTOTLE_MAX_PER_RUN   max. Maerkte pro Lauf (Standard 20)
  ARAISTOTLE_WAIT          max. Wartezeit auf neue Analysen in Sekunden (Standard 900 = 15 min)
  ARAISTOTLE_LOW_CREDITS   Warnung ab diesem Guthaben (Standard 60)

Test: python araistotle.py <polymarket_market_id>
"""
import json
import os
import sys
import time

import requests

API_KEY = os.environ.get("FACTICITY_API_KEY", "").strip()
API = (os.environ.get("FACTICITY_API_URL") or "https://api.facticity.ai").rstrip("/")


def _int(name, default):
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return default


MAX_PER_RUN = _int("ARAISTOTLE_MAX_PER_RUN", 20)
WAIT = _int("ARAISTOTLE_WAIT", 900)
LOW_CREDITS = _int("ARAISTOTLE_LOW_CREDITS", 60)
QUEUED = "QUEUED"


class FacticityError(Exception):
    pass


def enabled():
    return bool(API_KEY) and MAX_PER_RUN > 0


class Client:
    """REST-Client fuer die Facticity-API (wie im frueheren facticity.py)."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"X-API-KEY": API_KEY, "Content-Type": "application/json"})

    def connect(self):
        c = self.credits()
        if c is None:
            raise FacticityError("Facticity nicht erreichbar oder API-Key ungültig (get-credits)")
        print(f"Facticity verbunden, Credits: {c}")
        return True

    def credits(self):
        try:
            r = self.s.get(f"{API}/get-credits", timeout=30)
            if not r.ok:
                print(f"get-credits Fehler {r.status_code}: {r.text[:200]}")
                return None
            return r.json().get("credits_left")
        except (requests.RequestException, ValueError) as ex:
            print(f"get-credits Fehler: {ex}")
            return None

    def status(self, market_id):
        """Kostenlos. {} wenn die Abfrage fehlschlaegt."""
        try:
            r = self.s.get(f"{API}/asp/prediction-insight/{market_id}/status",
                           params={"source": "polymarket"}, timeout=15)
            return r.json() if r.ok else {}
        except (requests.RequestException, ValueError):
            return {}

    def insight(self, market_id):
        """(daten, fehler). fehler == QUEUED: Analyse laeuft bei Facticity weiter."""
        try:
            r = self.s.post(f"{API}/araistotle-insight/",
                            json={"polymarket_market_id": str(market_id), "refresh": False},
                            timeout=90)
        except requests.Timeout:
            return None, QUEUED
        except requests.RequestException as ex:
            return None, f"Netzwerkfehler: {ex}"
        if r.status_code in (502, 503, 504):
            return None, QUEUED
        if not r.ok:
            return None, f"Fehler {r.status_code}: {r.text[:150]}"
        try:
            data = r.json()
        except ValueError:
            return None, f"Ungültige Antwort: {r.text[:150]}"
        if data.get("araistotle_odds") is None and not data.get("ready"):
            return None, QUEUED
        return data, ""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_answer(d, name):
    """Insight -> gleiches Format wie die Exa-Antworten (fuer Telegram, Log, Orderbuch)."""
    if isinstance(d.get("insight"), dict):
        d = {**d["insight"], **d}
    odds = _num(d.get("araistotle_odds"))
    if odds is None:
        odds = _num(d.get("araistotle_probability") or d.get("araistotle_prob"))
    if odds is not None and odds > 1:
        odds /= 100
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
        signal = "offen"  # wird in new_markets gegen den Marktpreis gesetzt
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
    Rueckgabe: ({key: antwort-dict}, info-dict mit credits_left / fehler / neu / pending_keys)."""
    info = {"credits_left": None, "fehler": "", "neu": 0, "vorhanden": 0, "offen": 0,
            "pending_keys": []}
    out = {}
    if not enabled() or not items:
        return out, info
    fc = Client()
    credits = fc.credits()
    if credits is None:
        info["fehler"] = "Facticity nicht erreichbar oder API-Key ungültig"
        return out, info
    info["credits_left"] = credits
    if credits < 2:
        info["fehler"] = "Zu wenig Facticity-Credits"
        return out, info
    waiting = []
    for key, mid, name in items[:MAX_PER_RUN]:
        st = fc.status(mid)
        stage = str(st.get("status") or "").lower()
        if stage == "resolved":
            continue
        was_ready = bool(st.get("ready"))
        if not was_ready and st.get("exists"):
            waiting.append((key, mid, name))  # laeuft schon bei Facticity
            continue
        d, err = fc.insight(mid)  # vorhanden: 2 Credits, neu: 10 Credits
        if not was_ready:
            info["neu"] += 1
        if d:
            a = to_answer(d, name)
            if a:
                out[key] = a
                info["vorhanden"] += was_ready
        elif err == QUEUED:
            waiting.append((key, mid, name))
        else:
            print(f"ArAIstotle {mid}: {err}")
            if "credit" in err.lower() or " 402" in err:
                info["fehler"] = "Zu wenig Facticity-Credits"
                break
            info["fehler"] = err
        time.sleep(0.3)
    # Auf laufende Analysen warten (Status kostenlos, Abholen 2 Credits)
    deadline = time.time() + WAIT
    while waiting and time.time() < deadline:
        time.sleep(min(30, max(5, deadline - time.time())))
        still = []
        for key, mid, name in waiting:
            if fc.status(mid).get("ready"):
                d, _ = fc.insight(mid)
                a = to_answer(d, name) if d else None
                if a:
                    out[key] = a
                    continue
            still.append((key, mid, name))
        waiting = still
    info["offen"] = len(waiting)
    info["pending_keys"] = [k for k, _, _ in waiting]
    c = fc.credits()
    if c is not None:
        info["credits_left"] = c
    print(f"ArAIstotle: {len(out)}/{min(len(items), MAX_PER_RUN)} analysiert "
          f"({info['vorhanden']} vorhanden, {info['neu']} neu angestoßen, {info['offen']} noch offen), "
          f"Credits übrig: {info['credits_left']}")
    return out, info


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("FACTICITY_API_KEY fehlt.")
    mid = sys.argv[1] if len(sys.argv) > 1 else ""
    Client().connect()
    if mid:
        res, info = analyze([("test", mid, "Test")])
        print(json.dumps({"ergebnis": res, "info": info}, ensure_ascii=False, indent=2)[:3000])
