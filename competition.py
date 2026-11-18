"""
Trading Competition der Analyse-Tools (Exa, Exa Pro, FutureSearch, Endspurt-Scanner).

Regeln (fuer jedes Tool gleich):
  - Jedes Tool handelt mit TRADE_SIZE $ (Standard 100 $) pro Markt.
  - Ein Trade findet nur statt, wenn die Edge mindestens COMP_MIN_EDGE Cent betraegt
    (Standard 10 ¢): Tool-Wahrscheinlichkeit minus echter Einstiegspreis aus dem Orderbuch.
  - Gekauft wird die Seite (Ja oder Nein), die das Tool als unterbewertet sieht.
  - Gefuellt wird nur ueber Orderbuch-Stufen bis FILL_WINDOW Cent (Standard 5 ¢) ueber dem
    besten Ask. Reicht die Tiefe nicht fuer 100 $, wird nur der gefuellte Betrag gehandelt.
  - Exa und Exa Pro handeln zum Orderbuch bei Erfassung des Markts,
    FutureSearch zum Orderbuch beim taeglichen Check (18 Uhr).
  - Positionen werden bis zur Aufloesung gehalten. Offene Positionen werden zum
    aktuellen besten Bid bewertet (Wert bei Verkauf jetzt).

Liest data/exa_log.csv, data/futuresearch_log.csv und data/exa_snapshots.csv,
schreibt die Blaetter "Competition", "Competition Trades" und "Competition Verlauf"
ins Google Sheet (mit Gewinnkurve) und schickt die Rangliste per Telegram.
"""
import os
from datetime import datetime, timedelta
from html import escape

import exa_tracker as et
import gsheet
import orderbook

FS_FILE = os.path.join("data", "futuresearch_log.csv")
ENDGAME_FILE = os.path.join("data", "endgame_log.csv")
MIN_EDGE = et.env_float("COMP_MIN_EDGE", 10)
STAKE = orderbook.TRADE_SIZE
TOOLS = ["Exa", "Exa Pro", "FutureSearch", "Exa (Endspurt)", "Exa Pro (Endspurt)"]
MEDALS = ["🥇", "🥈", "🥉"]
SIG_PP = et.env_float("TRACKER_MIN_DELTA", 5)  # ab diesem Abstand zum Markt zaehlt eine Richtungsprognose
BOOK_KEYS = ["ja_bid", "ja_ask", "nein_bid", "nein_ask", "einstieg_ja", "gefuellt_ja",
             "einstieg_nein", "gefuellt_nein"]


# ---------------------------------------------------------------- Trades

def decide(prob, book):
    """Welche Seite kauft ein Tool mit Wahrscheinlichkeit `prob` (Prozent) bei diesem Orderbuch?
    Rueckgabe: dict(seite, einstieg, gefuellt, edge, fair) oder dict mit grund, wenn kein Trade."""
    if prob is None:
        return {"grund": "keine Einschätzung"}
    best = None
    for side, fair in (("ja", prob), ("nein", 100 - prob)):
        entry, filled = book.get(f"einstieg_{side}"), book.get(f"gefuellt_{side}")
        if entry is None or not filled:
            continue
        edge = round(fair - entry, 1)
        if best is None or edge > best["edge"]:
            best = {"seite": side, "einstieg": entry, "gefuellt": min(STAKE, filled),
                    "edge": edge, "fair": fair}
    if best is None:
        return {"grund": "kein Orderbuch"}
    if best["edge"] < MIN_EDGE:
        best["grund"] = f"Edge {best['edge']:.1f} ¢ < {MIN_EDGE:.0f} ¢"
    return best


def collect_trades(log, fs_log, eg_log=()):
    """Alle Trades und alle verworfenen Signale je Tool."""
    trades, skipped = [], {t: 0 for t in TOOLS}
    sources = []
    for x in log:
        book = {k: et.num(x.get(k)) for k in BOOK_KEYS}
        if book["einstieg_ja"] is None and book["einstieg_nein"] is None:
            continue  # alter Eintrag ohne Orderbuch
        sources.append(("Exa", x, et.num(x.get("exa_prozent")), book, x.get("erfasst_am", "")))
        sources.append(("Exa Pro", x, et.num(x.get("exa_pro_prozent")), book, x.get("erfasst_am", "")))
    for x in fs_log:
        book = {k: et.num(x.get(k)) for k in BOOK_KEYS}
        if book["einstieg_ja"] is None and book["einstieg_nein"] is None:
            continue
        sources.append(("FutureSearch", x, et.num(x.get("fs_prozent")), book, x.get("geprueft_am", "")))
    for x in eg_log:  # Endspurt-Scanner: Maerkte 2-7 Tage vor Ablauf
        book = {k: et.num(x.get(k)) for k in BOOK_KEYS}
        if book["einstieg_ja"] is None and book["einstieg_nein"] is None:
            continue
        x = dict(x, antwort=x.get("frage") or x.get("antwort"))
        sources.append(("Exa (Endspurt)", x, et.num(x.get("exa_prozent")), book, x.get("erfasst_am", "")))
        sources.append(("Exa Pro (Endspurt)", x, et.num(x.get("exa_pro_prozent")), book, x.get("erfasst_am", "")))
    taken = set()  # pro Tool und Markt nur der erste Trade

    for tool, x, prob, book, when in sources:
        if prob is None:
            continue
        d = decide(prob, book)
        key = (tool, str(x.get("market_id") or ""))
        if key in taken:
            continue
        if d.get("grund"):
            if "Edge" in d["grund"]:
                skipped[tool] += 1
            continue
        taken.add(key)
        mkt = et.num(x.get("markt_prozent") or x.get("markt_bei_check"))
        trades.append({
            "tool": tool, "zeit": when, "market_id": str(x.get("market_id") or ""),
            "event": x.get("event", ""), "slug": x.get("event_slug", ""),
            "antwort": x.get("antwort", "") or "Ja", "prob": prob, "markt": mkt,
            "seite": d["seite"], "einstieg": d["einstieg"], "edge": d["edge"],
            "einsatz": round(d["gefuellt"], 2),
            "anteile": round(d["gefuellt"] / (d["einstieg"] / 100), 2),
        })
    return trades, skipped


def snapshot_index(snaps):
    by = {}
    for s in sorted(snaps, key=lambda s: s["datum"]):
        by.setdefault(s["market_id"], []).append(s)
    return by


def value_on(trade, snap):
    """Wert der Position laut Schnappschuss (Aufloesung = 1 $ pro Anteil, sonst bester Bid)."""
    erg = snap.get("ergebnis")
    if erg in ("0", "1", 0, 1):
        won = int(float(erg)) if trade["seite"] == "ja" else 1 - int(float(erg))
        return trade["anteile"] * won, True
    bid = et.num(snap.get(f"{trade['seite']}_bid"))
    return (None if bid is None else trade["anteile"] * bid / 100), False


def evaluate(trades, markets, snaps_by):
    for t in trades:
        m = markets.get(t["market_id"])
        o = et.outcome_of(m)
        t["status"], t["wert"], t["gewinn"], t["ausstieg"] = "offen", None, None, None
        if o is not None:
            won = o if t["seite"] == "ja" else 1 - o
            t["status"] = "gewonnen" if won else "verloren"
            t["wert"] = round(t["anteile"] * won, 2)
        else:
            last = (snaps_by.get(t["market_id"]) or [None])[-1]
            if last:
                v, _ = value_on(t, last)
                bid = et.num(last.get(f"{t['seite']}_bid"))
                t["ausstieg"] = bid
                t["wert"] = None if v is None else round(v, 2)
            if m and m.get("closed"):
                t["status"] = "geschlossen"
        t["gewinn"] = None if t["wert"] is None else round(t["wert"] - t["einsatz"], 2)
    return trades


def leaderboard(trades, skipped):
    out = []
    for tool in TOOLS:
        ts = [t for t in trades if t["tool"] == tool]
        done = [t for t in ts if t["status"] in ("gewonnen", "verloren")]
        opn = [t for t in ts if t not in done]
        won = [t for t in done if t["status"] == "gewonnen"]
        real = round(sum(t["gewinn"] for t in done), 2)
        unreal = round(sum(t["gewinn"] for t in opn if t["gewinn"] is not None), 2)
        stake_done = sum(t["einsatz"] for t in done)
        stake_all = sum(t["einsatz"] for t in ts)
        gains = [t["gewinn"] for t in ts if t["gewinn"] is not None]
        out.append({
            "tool": tool, "trades": len(ts), "offen": len(opn), "aufgeloest": len(done),
            "gewonnen": len(won), "quote": (100 * len(won) / len(done)) if done else None,
            "einsatz": round(stake_all, 2), "realisiert": real,
            "roi": (100 * real / stake_done) if stake_done else None,
            "unrealisiert": unreal, "gesamt": round(real + unreal, 2),
            "roi_gesamt": (100 * (real + unreal) / stake_all) if stake_all else None,
            "edge": (sum(t["edge"] for t in ts) / len(ts)) if ts else None,
            "einstieg": (sum(t["einstieg"] for t in ts) / len(ts)) if ts else None,
            "fuellung": (sum(t["einsatz"] for t in ts) / len(ts)) if ts else None,
            "bester": max(gains) if gains else None, "schlechtester": min(gains) if gains else None,
            "verworfen": skipped.get(tool, 0),
        })
    return sorted(out, key=lambda r: -r["gesamt"])


def equity_curve(trades, snaps_by, today):
    """Kumulierter Gewinn (realisiert + Wert offener Positionen) je Tool und Tag."""
    if not trades:
        return [], []
    start = min(t["zeit"][:10] for t in trades)
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(today, "%Y-%m-%d")
    dates = [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]
    rows = []
    for d in dates:
        row = [d]
        for tool in TOOLS:
            total = 0.0
            for t in trades:
                if t["tool"] != tool or t["zeit"][:10] > d:
                    continue
                val = None
                for s in snaps_by.get(t["market_id"], []):
                    if s["datum"] > d:
                        break
                    if s["datum"] >= t["zeit"][:10]:
                        v, _ = value_on(t, s)
                        if v is not None:
                            val = v
                if d == today and t["wert"] is not None:
                    val = t["wert"]
                total += (val - t["einsatz"]) if val is not None else 0.0
            row.append(round(total, 2))
        rows.append(row)
    return dates, rows


# ---------------------------------------------------------------- Tabellen

def r(v, d=1):
    return None if v is None else round(v, d)


def build_tables(trades, board, curve_rows, now):
    T = f"{STAKE:.0f} $"
    rows, bold = [], []
    bold.append(len(rows))
    rows.append([f"🏆 Trading Competition – Stand {now:%d.%m.%Y %H:%M}"])
    rows.append([f"Regeln: je {T} pro Trade · nur bei Edge ≥ {MIN_EDGE:.0f} ¢ nach echtem Einstiegspreis · "
                 f"Füllung nur bis {orderbook.FILL_WINDOW:.0f} ¢ über bestem Ask · halten bis Auflösung"])
    rows.append([])
    bold.append(len(rows))
    rows.append(["Platz", "Tool", "Gesamt $ (realisiert + offen)", "Rendite gesamt %", "Trades", "Offen",
                 "Aufgelöst", "Gewonnen", "Trefferquote %", "Realisiert $", "Rendite realisiert %",
                 "Offene Positionen $", "Einsatz gesamt $", "Ø Edge ¢", "Ø Einstieg ¢", "Ø Füllung $",
                 "Bester Trade $", "Schlechtester Trade $", f"Signale verworfen (Edge < {MIN_EDGE:.0f} ¢)"])
    first_board = len(rows)
    for i, b in enumerate(board):
        rows.append([f"{i + 1}.", b["tool"], b["gesamt"], r(b["roi_gesamt"]), b["trades"], b["offen"],
                     b["aufgeloest"], b["gewonnen"], r(b["quote"]), b["realisiert"], r(b["roi"]),
                     b["unrealisiert"], b["einsatz"], r(b["edge"]), r(b["einstieg"]), r(b["fuellung"], 2),
                     b["bester"], b["schlechtester"], b["verworfen"]])
    rows.append([])

    # Monatswertung
    months = sorted({t["zeit"][:7] for t in trades}, reverse=True)
    if months:
        bold.append(len(rows))
        rows.append(["Monatswertung (Gesamt $ der in diesem Monat eröffneten Trades)"])
        bold.append(len(rows))
        rows.append(["Monat"] + TOOLS + [f"Trades {x}" for x in TOOLS])
        for mo in months:
            ms = [t for t in trades if t["zeit"].startswith(mo)]
            gains = [round(sum(t["gewinn"] or 0 for t in ms if t["tool"] == x), 2) for x in TOOLS]
            counts = [sum(1 for t in ms if t["tool"] == x) for x in TOOLS]
            rows.append([mo] + gains + counts)
        rows.append([])

    # Kopf-an-Kopf: Maerkte, in denen mindestens zwei Tools gehandelt haben
    by_market = {}
    for t in trades:
        by_market.setdefault(t["market_id"], {})[t["tool"]] = t
    shared = {k: v for k, v in by_market.items() if len(v) >= 2}
    if shared:
        bold.append(len(rows))
        rows.append(["Kopf-an-Kopf: Märkte, in denen mindestens zwei Tools gehandelt haben"])
        bold.append(len(rows))
        rows.append(["Tool", "Märkte", "Gesamt $ auf diesen Märkten", "Gleiche Richtung wie andere Tools"])
        for x in TOOLS:
            mine = [v[x] for v in shared.values() if x in v]
            same = sum(1 for v in shared.values() if x in v
                       and all(o["seite"] == v[x]["seite"] for o in v.values()))
            rows.append([x, len(mine), round(sum(t["gewinn"] or 0 for t in mine), 2),
                         f"{same} von {len(mine)}"])
        rows.append([])

    bold.append(len(rows))
    rows.append(["Erklärung"])
    for text in [
        "Gesamt $ = realisierter Gewinn aufgelöster Märkte + Wert offener Positionen, wenn man sie jetzt zum besten Bid verkaufen würde.",
        "Edge = Wahrscheinlichkeit des Tools für die gekaufte Seite minus echter Durchschnitts-Einstiegspreis aus dem Orderbuch (in Cent).",
        f"Füllung: Es werden nur Orderbuch-Stufen bis {orderbook.FILL_WINDOW:.0f} ¢ über dem besten Ask genutzt. Ist dort weniger als {T} Liquidität, wird nur der gefüllte Betrag gehandelt (Ø Füllung).",
        "Exa und Exa Pro bewerten jeden neuen Markt, FutureSearch nur die 2 Märkte mit dem größten Exa-Delta pro Tag. "
        "„Endspurt“ = eigene Strategie: Märkte 2–7 Tage vor Ablauf (Ja 15–85 %), geprüft auf schon feststehende, noch nicht eingepreiste Ergebnisse. "
        "Deshalb sind Rendite % und Trefferquote fairer zum Vergleichen als Gesamt $; die Kopf-an-Kopf-Tabelle zeigt nur gemeinsame Märkte.",
        "Gewinnkurve: siehe Diagramm unten und Blatt „Competition Verlauf“.",
    ]:
        rows.append([text])
    chart_row = len(rows) + 1

    money_cols = [2, 9, 11, 16, 17]
    comp = {"title": "Competition", "header": None, "rows": rows, "bold_rows": bold,
            "widths": [10, 20, 16, 12, 9, 9, 10, 10, 12, 13, 13, 14, 14, 10, 11, 11, 13, 14, 16],
            "posneg_cols": money_cols + [3, 10], "wrap_cols": []}

    trade_rows, links = [], []
    for t in sorted(trades, key=lambda t: t["zeit"], reverse=True):
        trade_rows.append([t["zeit"], t["tool"], t["event"], t["antwort"],
                           "Ja" if t["seite"] == "ja" else "Nein", r(t["prob"]), r(t["markt"]),
                           t["einstieg"], t["edge"], t["einsatz"], t["anteile"], t["status"],
                           t["ausstieg"], t["wert"], t["gewinn"]])
        links.append(f"https://polymarket.com/event/{t['slug']}" if t["slug"] else "")
    money = '+0.00" $";-0.00" $";0.00" $"'
    trades_tab = {
        "title": "Competition Trades",
        "header": ["Einstieg (Zeit)", "Tool", "Event", "Antwort", "Gekauft", "Tool %", "Markt-Mitte %",
                   "Einstieg ¢", "Edge ¢", "Einsatz $", "Anteile", "Status", "Bid jetzt ¢",
                   "Wert jetzt $", "Gewinn $"],
        "rows": trade_rows, "links": links, "link_col": 2,
        "widths": [16, 13, 48, 22, 8, 8, 10, 10, 8, 10, 10, 12, 10, 11, 11],
        "num_cols": {5: "0.0", 6: "0.0", 7: "0.0", 8: "+0.0;-0.0;0.0", 9: "0.00", 10: "0.00",
                     12: "0.0", 13: "0.00", 14: money},
        "posneg_cols": [8, 14],
    }
    curve_tab = {
        "title": "Competition Verlauf",
        "header": ["Datum"] + [f"{x} Gesamt $" for x in TOOLS],
        "rows": curve_rows, "widths": [12] + [18] * len(TOOLS),
        "num_cols": {c: "0.00" for c in range(1, len(TOOLS) + 1)},
        "posneg_cols": list(range(1, len(TOOLS) + 1)),
    }
    return [comp, trades_tab, curve_tab], chart_row, first_board


def finish_sheet(chart_row, n_curve):
    """Blaetter ans Ende sortieren und Gewinnkurve als Diagramm einfuegen."""
    sh = gsheet._open()
    meta = sh.fetch_sheet_metadata({"fields": "sheets(properties(sheetId,title,index),charts(chartId))"})
    sheets = sorted(meta.get("sheets", []), key=lambda s: s["properties"]["index"])
    ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheets}
    ours = [t for t in ("Competition", "Tool-Vergleich", "Prognosen", "Competition Trades",
                        "Competition Verlauf") if t in ids]
    others = [s["properties"]["title"] for s in sheets if s["properties"]["title"] not in ours]
    req = []
    for i, title in enumerate(others + ours):
        req.append({"updateSheetProperties": {"properties": {"sheetId": ids[title], "index": i},
                                              "fields": "index"}})
    for s in sheets:
        if s["properties"]["title"] == "Competition":
            for c in s.get("charts") or []:
                req.append({"deleteEmbeddedObject": {"objectId": c["chartId"]}})
    if n_curve:
        src = ids["Competition Verlauf"]
        series = [{"series": {"sourceRange": {"sources": [gsheet._rng(src, 0, n_curve + 1, c, c + 1)]}},
                   "targetAxis": "LEFT_AXIS"} for c in range(1, len(TOOLS) + 1)]
        req.append({"addChart": {"chart": {
            "spec": {"title": f"Gewinnkurve je Tool ({STAKE:.0f} $ pro Trade)",
                     "basicChart": {
                         "chartType": "LINE", "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
                         "axis": [{"position": "BOTTOM_AXIS", "title": "Datum"},
                                  {"position": "LEFT_AXIS", "title": "Gewinn $"}],
                         "domains": [{"domain": {"sourceRange": {
                             "sources": [gsheet._rng(src, 0, n_curve + 1, 0, 1)]}}}],
                         "series": series}},
            "position": {"overlayPosition": {
                "anchorCell": {"sheetId": ids["Competition"], "rowIndex": chart_row, "columnIndex": 0},
                "widthPixels": 900, "heightPixels": 380}}}}})
    sh.batch_update({"requests": req})


# ---------------------------------------------------------------- Tool-Vergleich (Treffsicherheit)

def collect_forecasts(log, fs_log, eg_log):
    """Alle Prognosen je Tool: (tool, market_id, tool %, markt % zum selben Zeitpunkt, slug, event).
    Pro Tool und Markt zaehlt die erste Prognose."""
    out, seen = [], set()

    def add(tool, x, prob, mkt, when=""):
        key = (tool, str(x.get("market_id") or ""))
        if prob is None or mkt is None or not key[1] or key in seen:
            return
        seen.add(key)
        out.append({"tool": tool, "market_id": key[1], "prob": prob, "markt": mkt, "zeit": when,
                    "event": x.get("event") or x.get("frage") or "", "slug": x.get("event_slug", ""),
                    "antwort": x.get("frage") if tool.endswith("(Endspurt)") else (x.get("antwort") or "Ja")})

    for x in log:
        mkt = et.num(x.get("markt_prozent"))
        add("Exa", x, et.num(x.get("exa_prozent")), mkt, x.get("erfasst_am", ""))
        add("Exa Pro", x, et.num(x.get("exa_pro_prozent")), mkt, x.get("erfasst_am", ""))
    for x in fs_log:
        add("FutureSearch", x, et.num(x.get("fs_prozent")), et.num(x.get("markt_bei_check")),
            x.get("geprueft_am", ""))
    for x in eg_log:
        mkt = et.num(x.get("markt_prozent"))
        add("Exa (Endspurt)", x, et.num(x.get("exa_prozent")), mkt, x.get("erfasst_am", ""))
        add("Exa Pro (Endspurt)", x, et.num(x.get("exa_pro_prozent")), mkt, x.get("erfasst_am", ""))
    return out


def accuracy(fc, outcomes):
    """Treffsicherheit je Tool im Vergleich zum Marktpreis zum selben Zeitpunkt."""
    rows = []
    for tool in TOOLS:
        fs = [f for f in fc if f["tool"] == tool]
        res = [(f, outcomes[f["market_id"]]) for f in fs if f["market_id"] in outcomes]
        n = len(res)
        if n:
            b_tool = sum((f["prob"] / 100 - o) ** 2 for f, o in res) / n
            b_mkt = sum((f["markt"] / 100 - o) ** 2 for f, o in res) / n
            closer = [abs(o * 100 - f["prob"]) < abs(o * 100 - f["markt"]) for f, o in res
                      if abs(f["prob"] - f["markt"]) >= 0.5]
            sig = [(f, o) for f, o in res if abs(f["prob"] - f["markt"]) >= SIG_PP]
            right = sum(1 for f, o in sig if (f["prob"] > f["markt"]) == (o == 1))
            err = sum(abs(o * 100 - f["prob"]) for f, o in res) / n
        rows.append({
            "tool": tool, "prognosen": len(fs), "aufgeloest": n,
            "brier": round(b_tool, 4) if n else None, "brier_markt": round(b_mkt, 4) if n else None,
            "vorsprung": round(b_mkt - b_tool, 4) if n else None,
            "naeher": f"{sum(closer)} von {len(closer)} ({et.pct(sum(closer), len(closer))})" if n and closer else "–",
            "naeher_q": (100 * sum(closer) / len(closer)) if n and closer else None,
            "richtung": f"{right} von {len(sig)} ({et.pct(right, len(sig))})" if n and sig else "–",
            "fehler": round(err, 1) if n else None,
            "abweichung": round(sum(abs(f["prob"] - f["markt"]) for f in fs) / len(fs), 1) if fs else None,
        })
    return rows


def head_to_head(fs_log, outcomes):
    """Gleiche Maerkte, gleicher Zeitpunkt: alle vom FutureSearch-Check geprueften Maerkte."""
    names = [("Markt", "markt_bei_check"), ("Exa", "exa_prozent"), ("Exa Pro", "exa_pro_prozent"),
             ("FutureSearch", "fs_prozent")]
    res = [(x, outcomes[str(x.get("market_id"))]) for x in fs_log if str(x.get("market_id")) in outcomes]
    full = [(x, o) for x, o in res if all(et.num(x.get(k)) is not None for _, k in names)]
    rows = []
    for name, key in names:
        if not full:
            rows.append([name, 0, "–", "–"])
            continue
        br = sum((et.num(x.get(key)) / 100 - o) ** 2 for x, o in full) / len(full)
        wins = sum(1 for x, o in full
                   if min(names, key=lambda nk: abs(o * 100 - et.num(x.get(nk[1]))))[0] == name)
        rows.append([name, len(full), round(br, 4), wins])
    return rows, len(full)


def comparison_tab(fc, outcomes, fs_log, now):
    acc = accuracy(fc, outcomes)
    rows, bold = [], []
    bold.append(len(rows))
    rows.append([f"🎯 Tool-Vergleich: Wer sagt Märkte am besten voraus? – Stand {now:%d.%m.%Y %H:%M}"])
    rows.append(["Unabhängig vom Handeln: jede Prognose zählt, auch ohne Trade. Verglichen wird immer mit dem "
                 "Marktpreis zum selben Zeitpunkt."])
    rows.append([])
    bold.append(len(rows))
    rows.append(["Tool", "Prognosen", "davon aufgelöst", "Brier Tool", "Brier Markt (gleiche Märkte)",
                 "Vorsprung vor Markt", "Näher am Ergebnis als Markt", f"Richtung richtig (≥ {SIG_PP:.0f} PP Abstand)",
                 "Ø Fehler PP", "Ø Abstand zum Markt PP"])
    for r in sorted(acc, key=lambda r: (r["vorsprung"] is None, -(r["vorsprung"] or 0))):
        rows.append([r["tool"], r["prognosen"], r["aufgeloest"], r["brier"], r["brier_markt"], r["vorsprung"],
                     r["naeher"], r["richtung"], r["fehler"], r["abweichung"]])
    rows.append([])
    h2h, n_full = head_to_head(fs_log, outcomes)
    bold.append(len(rows))
    rows.append([f"Kopf-an-Kopf auf denselben Märkten (vom FutureSearch-Check geprüft, aufgelöst: {n_full})"])
    bold.append(len(rows))
    rows.append(["Quelle", "Märkte", "Brier-Score", "Am nächsten am Ergebnis (Anzahl)"])
    rows.extend(h2h)
    rows.append([])
    bold.append(len(rows))
    rows.append(["Erklärung"])
    for t in [
        "Brier-Score = mittlerer quadratischer Fehler der Wahrscheinlichkeit (0 = perfekt, 0,25 = Münzwurf). Kleiner ist besser.",
        "Vorsprung vor Markt = Brier Markt minus Brier Tool auf denselben Märkten. Positiv = das Tool war besser als der Marktpreis – nur dann lohnt es sich, ihm zu folgen.",
        "Näher am Ergebnis: Bei wie vielen aufgelösten Märkten lag die Zahl des Tools näher an 0 % bzw. 100 % als der Marktpreis (nur wenn beide sich unterscheiden).",
        f"Richtung richtig: Wenn das Tool mindestens {SIG_PP:.0f} PP vom Markt abweicht – lag es mit der Richtung (höher/niedriger) richtig?",
        "Ø Abstand zum Markt: Wie stark weicht das Tool im Schnitt überhaupt vom Markt ab. Nahe 0 = das Tool plappert nur den Marktpreis nach.",
        "Kopf-an-Kopf ist der fairste Vergleich: gleiche Märkte, gleiche Frage. Aussagekräftig ab etwa 30 aufgelösten Märkten je Tool.",
    ]:
        rows.append([t])
    tab = {"title": "Tool-Vergleich", "header": None, "rows": rows, "bold_rows": bold,
           "widths": [22, 11, 13, 11, 16, 15, 22, 22, 11, 16], "posneg_cols": [5], "wrap_cols": []}
    return tab, acc


def forecasts_tab(fc, outcomes, markets):
    """Jede einzelne Prognose: Wahrscheinlichkeit, Entwicklung seit der Prognose, Ergebnis."""
    rows, links = [], []
    for f in sorted(fc, key=lambda f: f["zeit"], reverse=True):
        m = markets.get(f["market_id"])
        p = et.yes_price(m) if m else None
        now_p = None if p is None else round(p * 100, 1)
        o = outcomes.get(f["market_id"])
        diff = round(f["prob"] - f["markt"], 1)
        move = gap = toward = None
        if o is None and now_p is not None:
            move = round(now_p - f["markt"], 1)
            gap = round(f["prob"] - now_p, 1)
            if abs(diff) < 0.5 or abs(move) < 1:
                toward = "unverändert"
            else:
                toward = "ja" if (move > 0) == (diff > 0) else "nein"
        if o is not None:
            status = "aufgelöst: JA" if o == 1 else "aufgelöst: NEIN"
            right = "ja" if (f["prob"] >= 50) == (o == 1) else "nein"
            closer = "" if abs(diff) < 0.5 else (
                "ja" if abs(o * 100 - f["prob"]) < abs(o * 100 - f["markt"]) else "nein")
        else:
            status = "geschlossen" if m and m.get("closed") else "offen"
            right = closer = ""
        rows.append([f["zeit"], f["tool"], f["event"], f["antwort"], round(f["prob"], 1), round(f["markt"], 1),
                     diff, now_p if o is None else (100.0 if o == 1 else 0.0), move, toward, gap, status,
                     right, closer])
        links.append(f"https://polymarket.com/event/{f['slug']}" if f["slug"] else "")
    pp = "+0.0;-0.0;0.0"
    return {
        "title": "Prognosen",
        "header": ["Prognose am", "Tool", "Event", "Antwort / Frage", "Tool %", "Markt bei Prognose %",
                   "Tool − Markt PP", "Markt jetzt %", "Bewegung seit Prognose PP", "Bewegt sich zum Tool hin?",
                   "Abstand Tool − Markt jetzt PP", "Status", "Vorhersage richtig? (Tool ≥ 50 % = Ja)",
                   "Besser als der Markt?"],
        "rows": rows, "links": links, "link_col": 2,
        "widths": [16, 16, 44, 30, 9, 12, 11, 11, 13, 14, 14, 15, 18, 14],
        "num_cols": {4: "0.0", 5: "0.0", 6: pp, 7: "0.0", 8: pp, 10: pp},
        "posneg_cols": [8], "yesno_cols": [9, 12, 13], "wrap_cols": [],
    }


def outcomes_for(ids):
    markets = et.fetch_markets(ids) if ids else {}
    out = {}
    for mid, m in markets.items():
        o = et.outcome_of(m)
        if o is not None:
            out[mid] = o
    for r in et.read_csv(os.path.join("data", "endgame_results.csv")):
        if r.get("ergebnis") in ("0", "1"):
            out.setdefault(r["market_id"], int(r["ergebnis"]))
    return out, markets


# ---------------------------------------------------------------- Telegram

def telegram_text(board, trades, now, url):
    lines = [f"🏆 <b>Trading Competition – {now:%d.%m.%Y}</b>",
             f"<i>je {STAKE:.0f} $, nur bei Edge ≥ {MIN_EDGE:.0f} ¢, Füllung ≤ {orderbook.FILL_WINDOW:.0f} ¢</i>", ""]
    for i, b in enumerate(board):
        medal = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        roi = "" if b["roi_gesamt"] is None else f", {b['roi_gesamt']:+.1f} %"
        quote = f" · {b['gewonnen']}/{b['aufgeloest']} gewonnen" if b["aufgeloest"] else ""
        lines.append(f"{medal} <b>{escape(b['tool'])}</b>: {b['gesamt']:+.2f} $"
                     f" ({b['trades']} Trades{roi}{quote})")
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    fresh = [t for t in trades if t["zeit"] >= since]
    if fresh:
        lines += ["", "<b>Neue Trades (24 h):</b>"]
        for t in sorted(fresh, key=lambda t: -t["edge"])[:8]:
            side = "Ja" if t["seite"] == "ja" else "Nein"
            ans = "" if t["antwort"] in ("", "Ja") else f" – {escape(t['antwort'][:25])}"
            lines.append(f"• {escape(t['tool'])}: {side} @ {t['einstieg']:.1f}¢ (Edge +{t['edge']:.0f}¢, "
                         f"{t['einsatz']:.0f} $) – {escape(t['event'][:55])}{ans}")
    if url:
        lines += ["", f'📄 <a href="{url}">Competition im Google Sheet</a>']
    return "\n".join(lines)


# ---------------------------------------------------------------- Ablauf

def main():
    if os.environ.get("GITHUB_ACTIONS") and (not et.TOKEN or not et.CHAT_ID):
        raise SystemExit("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt – Secrets prüfen.")
    now = datetime.now(et.TZ)
    log, fs_log, eg_log = et.read_csv(et.LOG_FILE), et.read_csv(FS_FILE), et.read_csv(ENDGAME_FILE)
    trades, skipped = collect_trades(log, fs_log, eg_log)

    # Treffsicherheit aller Prognosen (auch ohne Trade)
    fc = collect_forecasts(log, fs_log, eg_log)
    outcomes, markets = outcomes_for({f["market_id"] for f in fc} | {t["market_id"] for t in trades})
    cmp_tab, acc = comparison_tab(fc, outcomes, fs_log, now)

    snaps_by = snapshot_index(et.read_csv(et.SNAP_FILE))
    evaluate(trades, markets, snaps_by)
    board = leaderboard(trades, skipped)
    _, curve_rows = equity_curve(trades, snaps_by, now.strftime("%Y-%m-%d"))
    tabs, chart_row, _ = build_tables(trades, board, curve_rows, now)
    tabs.insert(1, cmp_tab)
    tabs.insert(2, forecasts_tab(fc, outcomes, markets))

    url = ""
    if gsheet.enabled():
        try:
            gsheet.write_tables(tabs)
            finish_sheet(chart_row, len(curve_rows))
            url = gsheet.sheet_url()
        except Exception as ex:
            print("Google-Sheet-Fehler:", ex)
    if not url:
        et.send_document(f"competition_{now:%Y-%m-%d}.xlsx", et.build_excel(tabs),
                         "🏆 Trading Competition – in Excel öffnen")
    text = telegram_text(board, trades, now, url) if trades else (
        f"🏆 <b>Trading Competition – {now:%d.%m.%Y}</b>\nNoch keine Trades – kein Signal hatte bisher eine Edge "
        f"≥ {MIN_EDGE:.0f} ¢ nach Einstiegspreis (verworfen: "
        + ", ".join(f"{k} {v}" for k, v in skipped.items() if v) + ")."
        + (f'\n\n📄 <a href="{url}">Google Sheet öffnen</a>' if url else ""))
    ranked = sorted((r for r in acc if r["aufgeloest"]), key=lambda r: -(r["vorsprung"] or 0))
    if ranked:
        text += "\n\n🎯 <b>Treffsicherheit</b> (Brier, kleiner = besser; Vorsprung vor Markt):\n" + "\n".join(
            f"• {escape(r['tool'])}: {r['brier']:.3f} vs. Markt {r['brier_markt']:.3f} "
            f"({r['vorsprung']:+.3f}, {r['aufgeloest']} aufgelöst)" for r in ranked)
    else:
        n = len({f["market_id"] for f in fc})
        text += f"\n\n🎯 Tool-Vergleich: {len(fc)} Prognosen zu {n} Märkten gesammelt, noch keiner aufgelöst."
    et.send(text)
    print(f"{len(trades)} Trades, {len(fc)} Prognosen, {len(outcomes)} aufgelöste Märkte")


if __name__ == "__main__":
    main()
