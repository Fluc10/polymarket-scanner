"""
FutureSearch-Check fuer die staerksten Exa-Signale des Tages.

Einmal taeglich (Standard 18 Uhr Wien) werden aus data/exa_log.csv die neuen
Maerkte der letzten FS_HOURS Stunden genommen und die FS_PER_DAY Maerkte mit dem
groessten Exa-Delta (|Exa % - Marktpreis %|) an FutureSearch geschickt.
FutureSearch bekommt Frage, Aufloesungsregeln, Enddatum und den aktuellen
Marktpreis und liefert eine eigene Wahrscheinlichkeit mit Begruendung.

Ergebnis:
  - Telegram-Nachricht mit Markt / Exa / Exa Pro / FutureSearch
  - Protokoll data/futuresearch_log.csv (wird vom Workflow committed)
  - Blatt "FutureSearch" im Google Sheet (falls eingerichtet)
  - Guthaben-Warnung per Telegram, wenn das FutureSearch-Guthaben knapp oder leer ist
"""
import asyncio
import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import requests

import orderbook

GAMMA = "https://gamma-api.polymarket.com"
LOG_FILE = os.path.join("data", "exa_log.csv")
FS_FILE = os.path.join("data", "futuresearch_log.csv")
STATE_FILE = os.path.join("data", "futuresearch_state.json")
TZ = ZoneInfo("Europe/Vienna")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FS_KEY = os.environ.get("FUTURESEARCH_API_KEY", "")


def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


FS_PER_DAY = int(env_float("FS_PER_DAY", 2))          # Maerkte pro Tag
FS_HOURS = env_float("FS_HOURS", 24)                  # Zeitfenster rueckwirkend
FS_RUN_HOUR = int(env_float("FS_RUN_HOUR", 18))       # Uhrzeit Wien, ab der geprueft wird
FS_MIN_BALANCE = env_float("FS_MIN_BALANCE", 6)       # Warnung unter diesem Guthaben ($)
FS_EFFORT = (os.environ.get("FS_EFFORT") or "LOW").strip().upper()
FS_FORCE = (os.environ.get("FS_FORCE") or "").strip().lower() in ("1", "true", "ja", "yes")
STAKE = orderbook.TRADE_SIZE

FS_FIELDS = ["geprueft_am", "market_id", "event", "event_slug", "antwort", "frage", "erfasst_am",
             "markt_bei_erfassung", "exa_prozent", "exa_pro_prozent", "delta_pp", "markt_bei_check",
             "fs_prozent", "fs_delta_pp", "fs_richtung", "einig_mit_exa", "begruendung",
             "kosten_ca", "effort"]


# ---------------------------------------------------------------- Hilfen

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def get_json(url, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return None


def yes_price(m):
    try:
        return float(json.loads(m.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError, AttributeError):
        return None


def outcome_of(m):
    if not m or not m.get("closed"):
        return None
    p = yes_price(m)
    if p is None:
        return None
    return 1 if p >= 0.98 else 0 if p <= 0.02 else None


def send(text):
    if not TOKEN or not CHAT_ID:
        print(text)
        return
    for i in range(0, len(text), 3900):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text[i:i + 3900], "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=30)
        if r.status_code != 200:
            print("Telegram-Fehler:", r.status_code, r.text[:300])


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ---------------------------------------------------------------- Auswahl

def pick_candidates(log, done_ids, now_utc):
    """Die FS_PER_DAY Maerkte mit dem groessten |Exa-Delta| aus den letzten FS_HOURS Stunden."""
    since = now_utc - timedelta(hours=FS_HOURS)
    best = {}
    for x in log:
        try:
            t = datetime.strptime(x.get("erfasst_am", ""), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        d = num(x.get("delta_pp"))
        mid = str(x.get("market_id") or "")
        if t < since or d is None or not mid or mid in done_ids:
            continue
        if mid not in best or abs(d) > abs(num(best[mid].get("delta_pp")) or 0):
            best[mid] = x
    return sorted(best.values(), key=lambda x: -abs(num(x.get("delta_pp")) or 0))


def build_question(x, m):
    q = (m.get("question") or "").strip()
    if not q:
        ans = x.get("antwort") or "Ja"
        q = x.get("event", "") if ans in ("", "Ja") else f'{x.get("event", "")} – Antwort: {ans}'
    return q


# ---------------------------------------------------------------- FutureSearch

async def fs_balance():
    from futuresearch.billing import get_billing_balance
    return (await get_billing_balance()).current_balance_dollars


async def fs_forecast(rows):
    from pandas import DataFrame
    from futuresearch.ops import forecast
    from futuresearch.generated.models.forecast_effort_level import ForecastEffortLevel
    level = ForecastEffortLevel.HIGH if FS_EFFORT == "HIGH" else ForecastEffortLevel.LOW
    result = await forecast(input=DataFrame(rows), forecast_type="binary", effort_level=level)
    if getattr(result, "error", None):
        print("FutureSearch-Hinweis:", result.error)
    return result.data


def balance_or_none():
    try:
        return asyncio.run(fs_balance())
    except Exception as ex:
        print("Guthaben nicht abrufbar:", ex)
        return None


def is_balance_error(ex):
    try:
        from futuresearch.errors import FuturesearchInsufficientBalanceError
        if isinstance(ex, FuturesearchInsufficientBalanceError):
            return True
    except ImportError:
        pass
    text = str(ex).lower()
    return "insufficient" in text or "balance" in text or "402" in text


# ---------------------------------------------------------------- Google Sheet

def sheet_table(fs_rows, markets):
    rows, links = [], []
    for x in sorted(fs_rows, key=lambda r: r.get("geprueft_am", ""), reverse=True):
        m = markets.get(str(x.get("market_id")))
        o = outcome_of(m)
        p = yes_price(m) if m else None
        mkt_c, exa, fs = num(x.get("markt_bei_check")), num(x.get("exa_prozent")), num(x.get("fs_prozent"))
        pro = num(x.get("exa_pro_prozent"))
        closest = ""
        if o is not None:
            cand = {"Markt": mkt_c, "Exa": exa, "Exa Pro": pro, "FutureSearch": fs}
            cand = {k: v for k, v in cand.items() if v is not None}
            if cand:
                closest = min(cand, key=lambda k: abs(o * 100 - cand[k]))
        side = x.get("fs_richtung") or ""
        gain = None
        if o is not None and side and mkt_c:
            price = mkt_c if side == "ja" else 100 - mkt_c
            won = o if side == "ja" else 1 - o
            gain = round(STAKE / (price / 100) * won - STAKE, 2) if price > 0 else None
        rows.append([x.get("geprueft_am", ""), x.get("event", ""), x.get("antwort", ""),
                     num(x.get("markt_bei_erfassung")), exa, pro, mkt_c, fs, num(x.get("fs_delta_pp")),
                     {"ja": "Ja", "nein": "Nein"}.get(side, ""), x.get("einig_mit_exa", ""),
                     None if p is None else round(p * 100, 1),
                     "aufgelöst: JA" if o == 1 else "aufgelöst: NEIN" if o == 0 else
                     ("geschlossen" if m and m.get("closed") else "offen"),
                     closest, gain, num(x.get("kosten_ca")), x.get("begruendung", "")])
        links.append(f"https://polymarket.com/event/{x.get('event_slug')}" if x.get("event_slug") else "")
    money = '+0.00" $";-0.00" $";0.00" $"'
    return {
        "title": "FutureSearch",
        "header": ["Geprüft (Wien)", "Event", "Antwort", "Markt bei Erfassung %", "Exa %", "Exa Pro %",
                   "Markt bei Check %", "FutureSearch %", "FS Delta (FS−Markt) PP", "FS Richtung",
                   "Einig mit Exa?", "Markt jetzt %", "Status", "Am nächsten am Ergebnis",
                   f"Gewinn {STAKE:.0f} $ in FS-Richtung (Preis bei Check)", "Kosten ca. $",
                   "Begründung (FutureSearch)"],
        "rows": rows,
        "widths": [16, 48, 22, 11, 9, 9, 11, 12, 12, 10, 10, 10, 15, 16, 16, 10, 90],
        "link_col": 1, "links": links,
        "num_cols": {3: "0.0", 4: "0.0", 5: "0.0", 6: "0.0", 7: "0.0", 8: "+0.0;-0.0;0.0",
                     11: "0.0", 14: money, 15: "0.00"},
        "posneg_cols": [8, 14], "yesno_cols": [10], "wrap_cols": [16],
    }


def write_sheet(fs_rows):
    if not (os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")):
        return ""
    try:
        import gsheet
        markets = {}
        for x in fs_rows:
            mid = str(x.get("market_id"))
            if mid and mid not in markets:
                m = get_json(f"{GAMMA}/markets/{mid}")
                if isinstance(m, dict):
                    markets[mid] = m
        gsheet.write_tables([sheet_table(fs_rows, markets)])
        # Blatt hinter "Märkte", "Verlauf", "Auswertung" einsortieren
        sh = gsheet._open()
        ws = sh.worksheet("FutureSearch")
        sh.batch_update({"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": ws.id, "index": 3}, "fields": "index"}}]})
        return gsheet.sheet_url()
    except Exception as ex:
        print("Google-Sheet-Fehler:", ex)
        return ""


# ---------------------------------------------------------------- Auswertung

def summary(fs_rows):
    res = []
    for x in fs_rows:
        m = get_json(f"{GAMMA}/markets/{x.get('market_id')}")
        o = outcome_of(m) if isinstance(m, dict) else None
        if o is not None:
            res.append((x, o))
    if not res:
        return f"Bisher {len(fs_rows)} Märkte geprüft, noch keiner aufgelöst."

    def brier(key):
        v = [(num(x.get(key)) / 100 - o) ** 2 for x, o in res if num(x.get(key)) is not None]
        return (sum(v) / len(v), len(v)) if v else (None, 0)

    parts = []
    for key, name in (("markt_bei_check", "Markt"), ("exa_prozent", "Exa"),
                      ("exa_pro_prozent", "Exa Pro"), ("fs_prozent", "FutureSearch")):
        b, n = brier(key)
        if b is not None:
            parts.append(f"{name} {b:.3f}")
    return (f"Bisher {len(fs_rows)} geprüft, {len(res)} aufgelöst. Brier (kleiner = besser): "
            + " · ".join(parts))


# ---------------------------------------------------------------- Ablauf

def main():
    if os.environ.get("GITHUB_ACTIONS") and (not TOKEN or not CHAT_ID):
        raise SystemExit("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt – Secrets prüfen.")
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    state = load_state()

    # Zeitfenster: einmal pro Tag ab FS_RUN_HOUR (Wien) - manuell mit FS_FORCE jederzeit
    if not FS_FORCE:
        if now.hour < FS_RUN_HOUR:
            print(f"Noch nicht {FS_RUN_HOUR} Uhr in Wien ({now:%H:%M}) – nichts zu tun.")
            return
        if state.get("last_run") == today:
            print("Heute schon gelaufen.")
            return

    if not FS_KEY:
        send("🔮 <b>FutureSearch-Check</b>\n⚠️ Secret <code>FUTURESEARCH_API_KEY</code> fehlt – bitte in GitHub anlegen.")
        return

    fs_log = read_csv(FS_FILE)
    done = {str(x.get("market_id")) for x in fs_log}
    cands = pick_candidates(read_csv(LOG_FILE), done, datetime.now(timezone.utc))

    # Aktuellen Stand holen, geschlossene Maerkte ueberspringen
    chosen = []
    for x in cands:
        m = get_json(f"{GAMMA}/markets/{x.get('market_id')}")
        if not isinstance(m, dict) or m.get("closed") or yes_price(m) is None:
            continue
        chosen.append((x, m))
        if len(chosen) >= FS_PER_DAY:
            break

    state["last_run"] = today
    if not chosen:
        save_state(state)
        send(f"🔮 <b>FutureSearch-Check {now:%d.%m.}</b>\nKeine neuen Märkte mit Exa-Delta in den letzten {FS_HOURS:.0f} h.")
        return

    before = balance_or_none()
    if before is not None and before < 0.5:
        save_state(state)
        send(f"🔮 <b>FutureSearch-Check {now:%d.%m.}</b>\n🚨 <b>Guthaben leer</b> ({before:.2f} $) – "
             f"heute keine Prüfung. Aufladen: futuresearch.ai/app")
        return

    rows = []
    for x, m in chosen:
        p = yes_price(m) * 100
        rows.append({
            "question": build_question(x, m),
            "resolution_criteria": (m.get("description") or "").strip()[:4000],
            "resolution_date": (m.get("endDate") or "")[:10],
            "market_creation_date": (m.get("createdAt") or "")[:10],
            "market_price": f"{p:.1f}% Ja auf Polymarket (Stand {now:%Y-%m-%d %H:%M} Wien)",
        })

    try:
        data = asyncio.run(fs_forecast(rows))
    except Exception as ex:
        if is_balance_error(ex):
            save_state(state)
            send(f"🔮 <b>FutureSearch-Check {now:%d.%m.}</b>\n🚨 <b>Guthaben reicht nicht</b> für die Prüfung "
                 f"({escape(str(ex)[:200])}). Aufladen: futuresearch.ai/app")
        else:
            send(f"🔮 <b>FutureSearch-Check {now:%d.%m.}</b>\n❌ Fehler: {escape(str(ex)[:300])}")
        raise

    after = balance_or_none()
    cost = None if before is None or after is None else max(before - after, 0)
    per = None if cost is None else round(cost / len(chosen), 3)

    new_rows, blocks = [], []
    records = data.to_dict("records") if data is not None else []
    by_q = {str(r.get("question")): r for r in records if r.get("question")}
    for i, (x, m) in enumerate(chosen):
        r = by_q.get(rows[i]["question"]) or (records[i] if i < len(records) else {})
        fs_p = num(r.get("probability"))
        mkt_c = round(yes_price(m) * 100, 1)
        exa, pro, delta = num(x.get("exa_prozent")), num(x.get("exa_pro_prozent")), num(x.get("delta_pp"))
        fs_d = None if fs_p is None else round(fs_p - mkt_c, 1)
        fs_side = "" if fs_d is None or abs(fs_d) < 0.5 else ("ja" if fs_d > 0 else "nein")
        exa_side = x.get("richtung") or ("ja" if (delta or 0) > 0 else "nein")
        einig = "" if not fs_side else ("ja" if fs_side == exa_side else "nein")
        reason = str(r.get("rationale") or "").strip()
        new_rows.append({
            "geprueft_am": now.strftime("%Y-%m-%d %H:%M"), "market_id": x.get("market_id"),
            "event": x.get("event", ""), "event_slug": x.get("event_slug", ""),
            "antwort": x.get("antwort", ""), "frage": rows[i]["question"],
            "erfasst_am": x.get("erfasst_am", ""), "markt_bei_erfassung": x.get("markt_prozent", ""),
            "exa_prozent": x.get("exa_prozent", ""), "exa_pro_prozent": x.get("exa_pro_prozent", ""),
            "delta_pp": x.get("delta_pp", ""), "markt_bei_check": mkt_c,
            "fs_prozent": "" if fs_p is None else fs_p, "fs_delta_pp": "" if fs_d is None else fs_d,
            "fs_richtung": fs_side, "einig_mit_exa": einig, "begruendung": reason,
            "kosten_ca": "" if per is None else per, "effort": FS_EFFORT,
        })

        link = f'<a href="https://polymarket.com/event/{x.get("event_slug", "")}">{escape(x.get("event", ""))}</a>'
        ans = x.get("antwort") or "Ja"
        line = [f"{link}" + (f" – <b>{escape(ans)}</b>" if ans != "Ja" else "")]
        exa_txt = f"Exa {exa:.0f}%" if exa is not None else "Exa –"
        if pro is not None:
            exa_txt += f" · Exa Pro {pro:.0f}%"
        line.append(f"   Markt jetzt {mkt_c:.0f}% · {exa_txt}")
        if fs_p is None:
            line.append("   ❔ FutureSearch: keine Wahrscheinlichkeit erhalten")
        else:
            icon = "🟢" if fs_d >= 5 else "🔴" if fs_d <= -5 else "⚪"
            agree = {"ja": "🤝 einig mit Exa", "nein": "⚔️ gegen Exa"}.get(einig, "")
            line.append(f"   {icon} <b>FutureSearch {fs_p:.0f}%</b> ({fs_d:+.0f} PP zum Markt) {agree}")
        if reason:
            line.append(f"   <i>{escape(reason[:450])}{'…' if len(reason) > 450 else ''}</i>")
        blocks.append("\n".join(line))

    orderbook.csv_append(FS_FILE, FS_FIELDS, new_rows)
    save_state(state)
    all_rows = fs_log + new_rows
    url = write_sheet(all_rows)

    text = (f"🔮 <b>FutureSearch-Check {now:%d.%m.}</b> – Top {len(chosen)} Exa-Deltas der letzten "
            f"{FS_HOURS:.0f} h\n\n" + "\n\n".join(blocks) + "\n\n" + summary(all_rows))
    if after is not None:
        text += f"\n💳 Guthaben: <b>{after:.2f} $</b>" + (f" (heute ca. {cost:.2f} $)" if cost is not None else "")
        if after < FS_MIN_BALANCE:
            left = f" – reicht nur noch für ca. {int(after / per)} Prüfungen" if per else ""
            text += f"\n⚠️ <b>Guthaben knapp</b>{left}. Aufladen: futuresearch.ai/app"
    if url:
        text += f'\n📄 <a href="{url}">Google Sheet öffnen</a> (Blatt „FutureSearch“)'
    send(text)
    print(f"{len(chosen)} Märkte geprüft, Kosten ca. {cost}")


if __name__ == "__main__":
    main()
