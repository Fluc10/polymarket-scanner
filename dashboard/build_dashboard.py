"""
Baut das öffentliche Dashboard (GitHub Pages) aus dem Google Sheet.

Liest die Blätter "Prognosen" und "Competition", rechnet die Daten für die Seite
und schreibt site/index.html aus dashboard/template.html.

Lokal ohne Google: python dashboard/build_dashboard.py <prognosen.json> <competition.json>
(jeweils {"values": [[...], ...]} wie von der Sheets-API).
"""
import json
import os
import re
import sys
from collections import OrderedDict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from cats import cat  # noqa: E402

TOOLS = ["Exa", "Exa Pro", "ArAIstotle", "FutureSearch"]
OUT = os.path.join(os.path.dirname(HERE), "site", "index.html")


def num(s):
    s = str(s or "").strip().replace(".", "").replace(",", ".") if "," in str(s or "") else str(s or "").strip()
    s = s.replace("+", "").replace("$", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def load_values():
    if len(sys.argv) >= 3:
        return [json.load(open(p, encoding="utf-8"))["values"] for p in sys.argv[1:3]]
    import gsheet
    if not gsheet.enabled():
        raise SystemExit("GOOGLE_SHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON fehlen.")
    sh = gsheet._open()
    return [sh.worksheet("Prognosen").get_all_values(), sh.worksheet("Competition").get_all_values()]


def build_data(values):
    rows = [list(r) + [""] * 14 for r in values[1:] if r and r[0]]
    markets = OrderedDict()
    for r in sorted(rows, key=lambda r: r[0]):
        k = (r[2], r[3])
        e = markets.setdefault(k, {"t": {}, "check": None, "now": None, "status": r[11]})
        e["status"] = r[11]
        p = num(r[4])
        if r[1] in e["t"] or p is None:
            continue
        e["t"][r[1]] = p
        if e["check"] is None:
            e["check"] = num(r[5])
        if num(r[7]) is not None:
            e["now"] = num(r[7])
    op, res = [], []
    for (ev, ans), e in markets.items():
        if e["check"] is None or not e["t"]:
            continue
        x = {"title": ev, "ans": ans, "cat": cat(ev), "check": e["check"],
             "tools": [[t, e["t"][t]] for t in TOOLS if t in e["t"]]}
        if e["status"] == "offen":
            if e["now"] is None:
                continue
            x["now"] = e["now"]
            op.append((e, x))
        elif "JA" in e["status"].upper() or "NEIN" in e["status"].upper():
            x["out"] = 100 if "JA" in e["status"].upper() else 0
            res.append((e, x))
    first = {}
    for r in rows:
        if r[1] in TOOLS:
            first[r[1]] = min(first.get(r[1], r[0]), r[0])
    dates = sorted(r[0] for r in rows)
    op.reverse(); res.reverse()  # neueste zuerst
    return {"open": [x for _, x in op], "res": [x for _, x in res]}, first, dates


def build_comp(values):
    out = []
    for r in values:
        if len(r) > 4 and r[1] in TOOLS:
            out.append({"tool": r[1], "pnl": num(r[2]) or 0.0, "ret": num(r[3]), "trades": int(num(r[4]) or 0)})
    title = values[0][0] if values and values[0] else ""
    return out, title


def fmt_day(d):
    return f"{d:%b} {d.day}"


def main():
    prog, comp = load_values()
    data, first, dates = build_data(prog)
    comp_rows, comp_title = build_comp(comp)
    now = datetime.now(timezone.utc)
    t0 = datetime.strptime(dates[0], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc) if dates else now
    days = max(1, (now - t0).days)
    starts = " · ".join(f"{t} {fmt_day(datetime.strptime(first[t][:10], '%Y-%m-%d'))}" for t in TOOLS if t in first)
    meta = {"asof": f"{fmt_day(now)}, {now.year}", "asofShort": fmt_day(now),
            "first": f"{fmt_day(t0)}, {t0.year}, {t0:%H:%M} UTC", "days": days,
            "range": f"{fmt_day(t0)} – {fmt_day(now)}", "starts": starts}
    j = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    html = html.replace("__DATA__", j(data)).replace("__COMP__", j(comp_rows)).replace("__META__", j(meta))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write(html)
    print(f"Dashboard gebaut: {len(data['open'])} offene, {len(data['res'])} aufgelöste Märkte, "
          f"{len(comp_rows)} Tools in der Competition ({comp_title})")


if __name__ == "__main__":
    main()
