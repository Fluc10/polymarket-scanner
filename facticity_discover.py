"""Sucht die ArAIstotle-Endpunkte der Facticity-API (kostenlos, nur Lesezugriff auf die Doku)."""
import json
import os

import requests

KEY = os.environ.get("FACTICITY_API_KEY", "")
H = {"X-API-KEY": KEY, "Authorization": f"Bearer {KEY}"} if KEY else {}
BASE = "https://api.facticity.ai"

for path in ["/openapi.json", "/docs/openapi.json", "/api/openapi.json", "/v1/openapi.json", "/swagger.json"]:
    try:
        r = requests.get(BASE + path, headers=H, timeout=30)
    except requests.RequestException as ex:
        print(path, "Fehler", ex)
        continue
    print(path, "->", r.status_code)
    if r.status_code != 200:
        continue
    try:
        spec = r.json()
    except ValueError:
        print(r.text[:300])
        continue
    paths = spec.get("paths") or {}
    print(f"{len(paths)} Endpunkte gefunden:")
    for p, ops in sorted(paths.items()):
        for method, op in (ops or {}).items():
            if not isinstance(op, dict):
                continue
            params = [x.get("name") for x in op.get("parameters") or []]
            body = ""
            rb = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema", {})
            if rb:
                ref = rb.get("$ref", "").split("/")[-1]
                props = (spec.get("components", {}).get("schemas", {}).get(ref, {}) or rb).get("properties", {})
                body = " body=" + ",".join(props)
            mark = "  <<<" if any(w in p.lower() for w in ("araistotle", "insight", "market", "polymarket", "predict")) else ""
            print(f"  {method.upper():6} {p}  params={params}{body}{mark}")
    break

# ein paar naheliegende Pfade direkt pruefen (GET, ohne Kosten)
for p in ["/araistotle/insight", "/araistotle/markets", "/api/araistotle/insight", "/v1/araistotle/insight",
          "/araistotle_insight", "/polymarket/insight", "/insight", "/credits", "/get_credits", "/api/credits"]:
    try:
        r = requests.get(BASE + p, headers=H, timeout=20)
        print("GET", p, "->", r.status_code, r.text[:120].replace("\n", " "))
    except requests.RequestException as ex:
        print("GET", p, "Fehler", ex)
