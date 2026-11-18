#!/usr/bin/env python3
"""Builds grafana/dashboards/product-analytics.json (run: python3 tools/build_dashboard.py).
Keeping the panels as data makes every query easy to review and test."""
import json, pathlib

DS = {"type": "loki", "uid": "loki"}
SEL = '{source="faro", app_name="$app", app_env="$env", kind="%s"}'
EV, MS, EX = SEL % "event", SEL % "measurement", SEL % "exception"

def sessions_with(event):
    return (f'count(sum by (session_id) (count_over_time({EV} | logfmt '
            f'| event_name="{event}" [$__range])))')

REVENUE = (f'sum(sum_over_time({EV} | logfmt | event_name="purchase" '
           f'| unwrap event_data_revenue | __error__="" [$__range]))')
PURCHASES = f'sum(count_over_time({EV} | logfmt | event_name="purchase" [$__range]))'
ALL_SESSIONS = f'count(sum by (session_id) (count_over_time({EV} | logfmt [$__range])))'
CAMPAIGN = '| label_format session_attr_campaign=`{{ or .session_attr_campaign "direct" }}` '  # no UTM = direct

def vital(name):
    # `by (source)` collapses all sessions into one population before taking p75.
    # Without it, `| logfmt` makes every session its own series.
    return (f'quantile_over_time(0.75, {MS} | logfmt | type="web-vitals" '
            f'| value_{name}!="" | unwrap value_{name} | __error__="" [$__range]) by (source)')

# (title, type, w, h, queries[(expr, legend, instant)], unit, extra fieldConfig/options)
P = []
def panel(title, ptype, w, h, queries, unit=None, desc="", **opts):
    P.append(dict(title=title, type=ptype, w=w, h=h, queries=queries, unit=unit, desc=desc, opts=opts))
def row(title):
    P.append(dict(row=title))

row("Overview")
panel("Unique users", "stat", 4, 4, [(f'count(sum by (user_id) (count_over_time({EV} | logfmt [$__range])))', "", True)],
      desc="Distinct user_id values with at least one event in the time range.")
panel("Sessions", "stat", 4, 4, [(ALL_SESSIONS, "", True)])
panel("Page views", "stat", 4, 4, [(f'sum(count_over_time({EV} | logfmt | event_name="page_view" [$__range]))', "", True)])
panel("Conversion rate", "stat", 4, 4, [(f'{sessions_with("purchase")} / {ALL_SESSIONS} * 100', "", True)], unit="percent",
      desc="Sessions with a purchase ÷ all sessions.")
panel("Revenue", "stat", 4, 4, [(REVENUE, "", True)], unit="currencyUSD")
panel("Avg order value", "stat", 4, 4, [(f'{REVENUE} / {PURCHASES}', "", True)], unit="currencyUSD")

row("Engagement")
panel("Events per interval", "timeseries", 16, 8,
      [(f'sum by (event_name) (count_over_time({EV} | logfmt | event_name!~"faro.session_recording.event|ui_click" [$__auto]))', "{{event_name}}", False)])
panel("Active users per interval", "timeseries", 8, 8,
      [(f'count(sum by (user_id) (count_over_time({EV} | logfmt [$__auto])))', "active users", False)])

row("Funnel")
panel("Checkout funnel (unique sessions per step)", "bargauge", 12, 9, [
    (sessions_with("page_view"), "1 · Visited", True),
    (sessions_with("product_viewed"), "2 · Viewed product", True),
    (sessions_with("add_to_cart"), "3 · Added to cart", True),
    (sessions_with("checkout_started"), "4 · Started checkout", True),
    (sessions_with("purchase"), "5 · Purchased", True),
], desc="Unique sessions that fired each step's event in the time range (step counts, not strict ordered funnel).",
   orientation="horizontal")
panel("Revenue by campaign", "bargauge", 12, 9,
      [(f'sort_desc(sum by (session_attr_campaign) (sum_over_time({EV} | logfmt '
        f'| event_name="purchase" {CAMPAIGN}| unwrap event_data_revenue | __error__="" [$__range])))', "{{session_attr_campaign}}", True)],
      unit="currencyUSD", orientation="horizontal")

panel("Abandoned carts (sessions)", "stat", 6, 5,
      [(f'count(sum by (session_id) (count_over_time({EV} | logfmt | event_name="add_to_cart" [$__range])) '
        f'unless on (session_id) sum by (session_id) (count_over_time({EV} | logfmt | event_name="purchase" [$__range])))', "", True)],
      thresholds=[1, 50], desc="Sessions that added to cart but never purchased. Inspect them in Insights → Funnels / Session replay.")
panel("Recorded sessions (replay)", "stat", 6, 5,
      [(f'count(sum by (session_id) (count_over_time({EV} | logfmt | event_name="faro.session_recording.started" [$__range])))', "", True)])
panel("Most-clicked elements (heatmap data)", "bargauge", 12, 5,
      [(f'topk(8, sum by (event_data_label) (count_over_time({EV} | logfmt | event_name="ui_click" [$__range])))', "{{event_data_label}}", True)],
      orientation="horizontal")

row("Product & audience")
panel("Top pages", "bargauge", 8, 9,
      [(f'topk(10, sum by (event_data_path) (count_over_time({EV} | logfmt | event_name="page_view" [$__range])))',
        "{{event_data_path}}", True)], orientation="horizontal")
panel("Most added-to-cart products", "bargauge", 8, 9,
      [(f'sort_desc(sum by (event_data_product) (count_over_time({EV} | logfmt | event_name="add_to_cart" [$__range])))',
        "{{event_data_product}}", True)], orientation="horizontal")
panel("Sessions by campaign", "piechart", 8, 9,
      [(f'count by (session_attr_campaign) (sum by (session_attr_campaign, session_id) (count_over_time({EV} | logfmt {CAMPAIGN}[$__range])))',
        "{{session_attr_campaign}}", True)])
panel("Browsers (page views)", "piechart", 8, 8,
      [(f'sum by (browser_name) (count_over_time({EV} | logfmt | event_name="page_view" [$__range]))', "{{browser_name}}", True)])
panel("Signups by plan", "bargauge", 8, 8,
      [(f'sum by (event_data_plan) (count_over_time({EV} | logfmt | event_name="signup" [$__range]))', "{{event_data_plan}}", True)],
      orientation="horizontal")
panel("Feature adoption", "bargauge", 8, 8,
      [(f'sum by (event_data_feature) (count_over_time({EV} | logfmt | event_name="feature_used" [$__range]))', "{{event_data_feature}}", True)],
      orientation="horizontal")

row("Performance (Core Web Vitals, p75)")
panel("LCP p75", "stat", 6, 4, [(vital("lcp"), "", True)], unit="ms", thresholds=[2500, 4000])
panel("INP p75", "stat", 6, 4, [(vital("inp"), "", True)], unit="ms", thresholds=[200, 500])
panel("CLS p75", "stat", 6, 4, [(vital("cls"), "", True)], unit="none", thresholds=[0.1, 0.25], decimals=3)
panel("TTFB p75", "stat", 6, 4, [(vital("ttfb"), "", True)], unit="ms", thresholds=[800, 1800])
panel("LCP p75 · mobile vs desktop", "timeseries", 24, 8,
      [(f'quantile_over_time(0.75, {MS} | logfmt | type="web-vitals" | value_lcp!="" '
        f'| unwrap value_lcp | __error__="" [$__auto]) by (browser_mobile)', "mobile={{browser_mobile}}", False)], unit="ms")

row("Errors")
panel("JS errors", "stat", 4, 8, [(f'sum(count_over_time({EX} [$__range]))', "", True)], thresholds=[1, 20])
panel("Top errors", "bargauge", 12, 8,
      [(f'topk(10, sum by (type, value) (count_over_time({EX} | logfmt [$__range])))', "{{type}}: {{value}}", True)],
      orientation="horizontal")

panel("Frustration signals (Faro)", "bargauge", 8, 8,
      [(f'sum by (event_name, view_name) (count_over_time({EV} | logfmt | event_name=~"faro.frustration.*" [$__range]))',
        "{{event_name}} · {{view_name}}", True)], orientation="horizontal",
      desc="Rage clicks and dead clicks detected automatically by the Faro Web SDK.")

row("Raw events")
panel("Live event stream", "logs", 24, 10, [(EV + ' != "faro.session_recording.event"', "", False)])

# --------------------------------------------------------------------------- build
def stat_thresholds(th):
    if not th:
        return {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}
    return {"mode": "absolute", "steps": [{"color": "green", "value": None},
                                          {"color": "orange", "value": th[0]}, {"color": "red", "value": th[1]}]}

panels, y, x, rowh, pid = [], 0, 0, 0, 1
for p in P:
    if "row" in p:
        if x: y += rowh; x = 0; rowh = 0
        panels.append({"type": "row", "title": p["row"], "id": pid, "collapsed": False,
                       "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
        pid += 1; y += 1; continue
    if x + p["w"] > 24: y += rowh; x = 0; rowh = 0
    targets = [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": l,
                "queryType": "instant" if inst else "range", "editorMode": "code"}
               for i, (e, l, inst) in enumerate(p["queries"])]
    defaults = {"thresholds": stat_thresholds(p["opts"].get("thresholds"))}
    if p["unit"]: defaults["unit"] = p["unit"]
    if "decimals" in p["opts"]: defaults["decimals"] = p["opts"]["decimals"]
    options = {}
    if p["type"] == "stat":
        options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                   "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "center"}
        if not p["opts"].get("thresholds"):
            defaults["color"] = {"mode": "fixed", "fixedColor": "blue"}
    elif p["type"] == "bargauge":
        options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                   "orientation": p["opts"].get("orientation", "horizontal"), "displayMode": "gradient",
                   "showUnfilled": True, "valueMode": "color", "namePlacement": "top", "sizing": "auto"}
        defaults["color"] = {"mode": "palette-classic"}
        defaults["min"] = 0
    elif p["type"] == "piechart":
        options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                   "pieType": "donut", "legend": {"displayMode": "table", "placement": "right", "values": ["value", "percent"]},
                   "tooltip": {"mode": "single"}}
    elif p["type"] == "timeseries":
        options = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                   "tooltip": {"mode": "multi", "sort": "desc"}}
        defaults["custom"] = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 12, "showPoints": "never",
                              "stacking": {"mode": "normal" if p["title"].startswith("Events") else "none", "group": "A"}}
    elif p["type"] == "logs":
        options = {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True,
                   "prettifyLogMessage": False, "dedupStrategy": "none"}
    panels.append({"id": pid, "type": p["type"], "title": p["title"], "description": p["desc"], "datasource": DS,
                   "gridPos": {"x": x, "y": y, "w": p["w"], "h": p["h"]}, "targets": targets,
                   "fieldConfig": {"defaults": defaults, "overrides": []}, "options": options})
    pid += 1; x += p["w"]; rowh = max(rowh, p["h"])

dashboard = {
    "uid": "faro-product-analytics", "title": "Product Analytics · Grafana Faro",
    "description": "Frontend product analytics from Grafana Faro events stored in Loki.",
    "tags": ["faro", "product-analytics", "poc"], "timezone": "browser", "editable": True,
    "schemaVersion": 39, "version": 1, "refresh": "30s", "graphTooltip": 1,
    "time": {"from": "now-30m", "to": "now"},
    "timepicker": {"refresh_intervals": ["10s", "30s", "1m", "5m"]},
    "templating": {"list": [
        {"name": "app", "label": "App", "type": "query", "datasource": DS, "refresh": 2,
         "query": {"type": 1, "label": "app_name", "stream": '{source="faro"}', "refId": "app"},
         "definition": "label_values({source=\"faro\"}, app_name)", "current": {"text": "demo-shop", "value": "demo-shop"}},
        {"name": "env", "label": "Environment", "type": "query", "datasource": DS, "refresh": 2,
         "query": {"type": 1, "label": "app_env", "stream": '{source="faro", app_name="$app"}', "refId": "env"},
         "definition": "label_values({source=\"faro\", app_name=\"$app\"}, app_env)", "current": {"text": "poc", "value": "poc"}},
    ]},
    "annotations": {"list": []}, "panels": panels,
    "links": [{"title": t, "type": "link", "url": u, "targetBlank": True, "icon": "external link"} for t, u in [
        ("Session replay", "http://localhost:8080/insights/replay.html"), ("Funnels & drop-off", "http://localhost:8080/insights/funnel.html"),
        ("Retention", "http://localhost:8080/insights/retention.html"), ("User paths", "http://localhost:8080/insights/paths.html"),
        ("Heatmaps", "http://localhost:8080/insights/heatmap.html")]],
}
out = pathlib.Path(__file__).resolve().parent.parent / "grafana/dashboards/product-analytics.json"
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} ({len([p for p in panels if p['type'] != 'row'])} panels)")
