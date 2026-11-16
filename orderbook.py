"""
Orderbuch-Auswertung fuer Polymarket (CLOB).

Fuer einen Markt werden die Orderbuecher von Ja- und Nein-Token geholt und
berechnet, zu welchem Durchschnittspreis ein Kauf von TRADE_SIZE $ tatsaechlich
gefuellt wuerde - nur ueber die Preisstufen innerhalb von FILL_WINDOW Cent ueber
dem besten Ask.

Handelbar = Spread <= MAX_SPREAD Cent UND TRADE_SIZE $ innerhalb des Fensters fuellbar.
Alle Preise in Cent (0-100).
"""
import csv
import json
import os
import time

import requests

CLOB = "https://clob.polymarket.com"


def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


TRADE_SIZE = env_float("TRADE_SIZE", 100)    # Einsatz pro Trade in $
FILL_WINDOW = env_float("FILL_WINDOW", 5)    # Cent ueber dem besten Ask, die durchlaufen werden
MAX_SPREAD = env_float("MAX_SPREAD", 5)      # maximaler Spread in Cent fuer "handelbar"


def get_book(token_id, tries=3):
    """Orderbuch eines Tokens: (bids absteigend, asks aufsteigend) als Listen (preis, menge)."""
    if not token_id:
        return None
    for i in range(tries):
        try:
            r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=20)
            if r.status_code == 200:
                d = r.json()
                bids = sorted(((float(x["price"]), float(x["size"])) for x in d.get("bids") or []),
                              reverse=True)
                asks = sorted((float(x["price"]), float(x["size"])) for x in d.get("asks") or [])
                return bids, asks
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            return None
        except (requests.RequestException, ValueError, KeyError):
            time.sleep(2 * (i + 1))
    return None


def walk(asks, usd=None, window=None):
    """Kauf ueber die Ask-Stufen innerhalb von `window` Cent ueber dem besten Ask.
    Rueckgabe: dict(avg Cent, gefuellt $, anteile, voll True/False) oder None."""
    usd = TRADE_SIZE if usd is None else usd
    window = FILL_WINDOW if window is None else window
    if not asks:
        return None
    best = asks[0][0]
    spent = shares = 0.0
    for p, s in asks:
        if p > best + window / 100 + 1e-9 or p <= 0:
            break
        take = min(p * s, usd - spent)
        spent += take
        shares += take / p
        if spent >= usd - 1e-9:
            break
    return {"avg": round(100 * spent / shares, 2) if shares else None,
            "gefuellt": round(spent, 2), "anteile": round(shares, 2),
            "voll": spent >= usd - 0.01}


def market_book(m):
    """Orderbuch-Kennzahlen eines Gamma-Markts (dict) - Preise in Cent."""
    try:
        ids = json.loads(m.get("clobTokenIds") or "[]")
    except (ValueError, TypeError):
        ids = []
    if len(ids) < 2:
        return None
    yes, no = get_book(ids[0]), get_book(ids[1])
    if yes is None:
        return None
    if no is None or (not no[0] and not no[1]):
        # Ersatz: Nein-Buch aus dem Ja-Buch spiegeln (Ja-Bid p == Nein-Ask 1-p)
        no = ([(1 - p, s) for p, s in yes[1]], sorted((1 - p, s) for p, s in yes[0]))
    yb, ya = yes
    nb, na = no
    c = lambda v: None if v is None else round(v * 100, 2)
    out = {
        "ja_bid": c(yb[0][0]) if yb else None, "ja_ask": c(ya[0][0]) if ya else None,
        "nein_bid": c(nb[0][0]) if nb else None, "nein_ask": c(na[0][0]) if na else None,
    }
    fy, fn = walk(ya), walk(na)
    out["einstieg_ja"] = fy["avg"] if fy else None
    out["gefuellt_ja"] = fy["gefuellt"] if fy else 0.0
    out["einstieg_nein"] = fn["avg"] if fn else None
    out["gefuellt_nein"] = fn["gefuellt"] if fn else 0.0
    return out


def spread(b, side):
    bid, ask = b.get(f"{side}_bid"), b.get(f"{side}_ask")
    if bid is None or ask is None:
        return None
    return round(ask - bid, 2)


def tradable(b, side):
    """(True/False, Grund) fuer Kauf der Seite 'ja' oder 'nein'."""
    if not b or b.get(f"einstieg_{side}") is None:
        return False, "kein Orderbuch"
    sp = spread(b, side)
    filled = float(b.get(f"gefuellt_{side}") or 0)
    if sp is None:
        return False, "kein Orderbuch"
    if sp > MAX_SPREAD + 1e-9:
        return False, f"Spread zu groß ({sp:.0f} ¢)"
    if filled < TRADE_SIZE - 0.01:
        return False, f"zu wenig Liquidität ({filled:.0f} $ bis +{FILL_WINDOW:.0f} ¢)"
    return True, "handelbar"


def csv_append(path, fields, rows):
    """Haengt Zeilen an eine CSV an. Hat die Datei eine aeltere Spaltenliste,
    wird sie einmalig auf die neue Spaltenliste umgeschrieben (alte Daten bleiben)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            old_fields = reader.fieldnames or []
            old_rows = list(reader) if old_fields != fields else None
        if old_rows is not None:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(old_rows)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)
