#!/usr/bin/env python3
"""Runs every query in the dashboard against Loki, exactly as Grafana would
(after variable substitution), and prints what each panel would show.
Usage: python3 tools/test_queries.py [LOKI_URL]   (default http://localhost:3100)"""
import json, sys, time, urllib.parse, urllib.request, pathlib

LOKI = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3100"
dash = json.loads((pathlib.Path(__file__).resolve().parent.parent / "grafana/dashboards/product-analytics.json").read_text())
SUBS = {"$app": "demo-shop", "$env": "poc", "$__range": "30m", "$__auto": "1m"}

def q(path, params):
    url = f"{LOKI}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"status": "error", "error": e.read().decode()[:300]}

failures = 0
for p in dash["panels"]:
    if p["type"] == "row":
        continue
    for t in p["targets"]:
        expr = t["expr"]
        for k, v in SUBS.items():
            expr = expr.replace(k, v)
        now = time.time()
        if t["queryType"] == "instant":
            res = q("/loki/api/v1/query", {"query": expr, "time": int(now * 1e9)})
        else:
            res = q("/loki/api/v1/query_range", {"query": expr, "start": int((now - 1800) * 1e9),
                                                  "end": int(now * 1e9), "step": "60s", "limit": 20})
        label = f"{p['title']}" + (f" [{t['legendFormat']}]" if p["type"] == "bargauge" and len(p["targets"]) > 1 else "")
        if res.get("status") != "success":
            failures += 1
            print(f"FAIL  {label}: {res.get('error')}")
            continue
        data = res["data"]["result"]
        if not data:
            failures += 1
            print(f"EMPTY {label}")
            continue
        if res["data"]["resultType"] == "vector":
            vals = []
            for s in data[:6]:
                name = ",".join(f"{v}" for k, v in s["metric"].items()) or "value"
                vals.append(f"{name}={float(s['value'][1]):.4g}")
            print(f"OK    {label}: " + "  ".join(vals) + (" …" if len(data) > 6 else ""))
        elif res["data"]["resultType"] == "matrix":
            print(f"OK    {label}: {len(data)} series, {sum(len(s['values']) for s in data)} points")
        else:
            print(f"OK    {label}: {sum(len(s['values']) for s in data)} log lines")
print(f"\n{'ALL QUERIES RETURNED DATA' if not failures else str(failures) + ' query problem(s)'}")
sys.exit(1 if failures else 0)
