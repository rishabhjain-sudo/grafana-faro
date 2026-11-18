# Frontend product analytics POC: Grafana Faro + Grafana

A local Docker proof of concept for **frontend product analytics** using only open-source Grafana tools:

| Component | Role | Licence |
|---|---|---|
| **Grafana Faro Web SDK** 2.14 | In the browser: captures page views, custom product events, sessions, users, Core Web Vitals, JS errors, rage/dead clicks | Apache-2.0 |
| **Grafana Alloy** 1.21 | `faro.receiver` collects SDK payloads over HTTP and ships them to Loki | Apache-2.0 |
| **Loki** 3.7 | Stores every event as a queryable log line | AGPL-3.0 |
| **Grafana** 13.2 | Product analytics dashboard + ad-hoc LogQL in Explore | AGPL-3.0 |

```
 Demo shop (Faro SDK) ─┐                                        ┌─► Grafana dashboard
                       ├─ POST /collect ─► Alloy ─► Loki ◄─LogQL┤
 Traffic generator  ───┘     :12347                             └─► Explore (ad-hoc queries)
```

## What you get

- **Demo shop** (http://localhost:8080): a small single-page shop instrumented with Faro. Click around and your own visits appear on the dashboard.
- **Traffic generator**: 300 realistic sessions on start, then about 20 per minute. It covers funnel drop-off, returning users, campaigns, mobile vs desktop, web vitals and errors.
- **"Product Analytics · Grafana Faro" dashboard** (25 panels):
  - **Overview:** unique users, sessions, page views, conversion rate, revenue, average order value
  - **Engagement:** events over time, active users
  - **Funnel:** visited → viewed product → added to cart → started checkout → purchased
  - **Product and audience:** top pages, top products, sessions and revenue by campaign, browsers, signups by plan, feature adoption
  - **Performance:** Core Web Vitals p75 (LCP, INP, CLS, TTFB), LCP on mobile vs desktop
  - **Errors:** JS errors, top errors, rage and dead clicks
  - **Raw events:** live event stream
- **Query cookbook** ([docs/QUERIES.md](docs/QUERIES.md)): ready-to-run LogQL for DAU, retention, conversion by device, revenue, per-page vitals, and single-user journeys.

## Run it

**Requirements:** Docker Desktop (or Docker Engine + Compose v2), about 2 GB of free RAM, and free ports **3000, 8080, 12347**. Python 3 is optional, only for `./poc.sh test`.

```bash
unzip faro-analytics-poc.zip && cd faro-analytics-poc
./poc.sh up
```
On Windows, run it from WSL2. Without bash, `docker compose up -d` does the same thing.

Then:
1. Open **http://localhost:3000**. The dashboard opens directly, with no login. Data appears within about 30 seconds.
2. Open **http://localhost:8080**, browse products, add to cart, check out, sign up on *Pricing*, and press *Trigger JS error*.
3. Back in Grafana, your clicks show up within about 30 seconds. Use the **Live event stream** panel or Explore (`{source="faro"} | logfmt | browser_name="Chrome"`).
4. Try your own questions in **Explore** using [docs/QUERIES.md](docs/QUERIES.md).

```bash
./poc.sh test     # runs every dashboard query against Loki and prints the numbers
./poc.sh logs loadgen
./poc.sh down     # stop (data kept)    ./poc.sh reset  # stop and delete data
```

Traffic volume: `SEED_SESSIONS=1000 SESSIONS_PER_MINUTE=60 docker compose up -d loadgen`. Set `SESSIONS_PER_MINUTE=0` for a one-off seed.

## Instrument your own app

```html
<script src="https://unpkg.com/@grafana/faro-web-sdk@2.14.0/dist/bundle/faro-web-sdk.iife.js"></script>
<script>
  const faro = GrafanaFaroWebSdk.initializeFaro({
    url: 'http://localhost:12347/collect',
    app: { name: 'my-app', version: '1.0.0', environment: 'dev' },
    instrumentations: GrafanaFaroWebSdk.getWebInstrumentations(),
    sessionTracking: { enabled: true, persistent: true },
  });
  faro.api.setUser({ id: 'user-123', attributes: { plan: 'pro' } });
  faro.api.pushEvent('signup', { plan: 'pro' });          // attribute values must be strings
</script>
```
For npm-based apps, install `@grafana/faro-web-sdk` and import `initializeFaro` and `getWebInstrumentations` from it. Pick your app in the dashboard's **App** dropdown.

## What was verified

The dashboard was checked against a real **Loki 3.7.10 + Alloy 1.21.3** pipeline, using the same versions and the same config files as `docker-compose.yml`:

- **Dashboard queries:** all 29 return data. Users, sessions, page views, conversion, funnel and p75 web vitals match numbers calculated separately from the raw events.
- **Demo shop:** its real `app.js` ran in a headless DOM with the vendored Faro SDK 2.14.0. One visitor's full journey (product → cart → checkout → purchase → signup → feature use) reached Loki with a consistent user and session.
- **Query cookbook:** every query in `docs/QUERIES.md` runs. `docker-compose.yml` validates, and `poc.sh` passes shellcheck.

**Not run here:** the Grafana UI itself (Grafana couldn't be downloaded in my environment) and the containers as a whole (no Docker daemon). If a panel looks off in your Grafana, its query is in `tools/build_dashboard.py`. Run `./poc.sh test` to see what Loki returns for it.

## POC limits (and the path to production)

- **The funnel counts unique sessions per step,** not a strict ordered funnel. Strict ordered funnels, cohorts and retention grids are where LogQL gets awkward. For those, add ClickHouse (or PostHog) next to Loki. Keep Faro and Alloy as the collection layer, and add a ClickHouse writer to the pipeline.
- **Loki stamps events when they arrive,** so you can't back-fill history through Faro.
- **High-cardinality fields** (`session_id`, `user_id`) stay in the log line, not in labels. That's correct for Loki, but long time ranges over millions of events are slow. Use recording rules or ClickHouse for heavy reporting.
- **Security:** Grafana runs with anonymous admin and the collector accepts any origin. For a real deployment, turn on Grafana auth, restrict `cors_allowed_origins`, put the collector behind TLS, and set Loki retention to your policy.

## Layout

```
docker-compose.yml                 Loki, Alloy, Grafana, demo shop (nginx), traffic generator
alloy/config.alloy                 faro.receiver → Loki (labels: source, app_name, app_env, kind)
loki/loki.yaml                     single-binary Loki, filesystem storage, 7-day retention
grafana/provisioning/              Loki data source + dashboard provider
grafana/dashboards/                product-analytics.json (generated by tools/build_dashboard.py)
demo-app/                          instrumented demo shop + vendored Faro SDK 2.14.0
loadgen/loadgen.py                 synthetic visitors (stdlib Python, Faro payload format)
tools/build_dashboard.py           dashboard source of truth (edit here, then re-run)
tools/test_queries.py              runs every dashboard query against Loki
docs/QUERIES.md                    LogQL cookbook for product questions
```
