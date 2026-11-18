#!/usr/bin/env python3
"""
Synthetic traffic for the Faro analytics POC.

Three phases:
  1. BACKFILL (once): BACKFILL_DAYS of history written straight to Loki with
     historical timestamps, in exactly the line format Alloy's faro.receiver
     produces. Faro events are stamped on arrival, so this is the only way to
     get retention cohorts and multi-day trends in a fresh POC. Uses its own
     stream (label origin="backfill") so it never conflicts with live data.
     Skipped if history already exists.
  2. SEED: SEED_SESSIONS sessions sent through Alloy right now.
  3. LIVE: ~SESSIONS_PER_MINUTE sessions/minute through Alloy, forever.

Each simulated session walks the shop with realistic behaviour: the
page_view -> product_viewed -> add_to_cart -> checkout_started -> purchase
funnel with drop-off, returning users, campaigns, devices, web vitals, JS
errors, and ui_click events on the real DOM elements of the demo shop (for
heatmaps). Session replays only come from real browsers (they need a DOM).

Env:
  COLLECTOR_URL             default http://alloy:12347/collect
  LOKI_PUSH_URL             default http://loki:3100/loki/api/v1/push
  LOKI_QUERY_URL            default http://loki:3100
  BACKFILL_DAYS             default 13   (0 = no backfill; max 13, Loki accepts samples up to 14 days old)
  BACKFILL_SESSIONS_PER_DAY default 120
  SEED_SESSIONS             default 300
  SESSIONS_PER_MINUTE       default 20   (0 = stop after seeding)
"""
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

COLLECTOR_URL = os.environ.get("COLLECTOR_URL", "http://alloy:12347/collect")
LOKI_PUSH_URL = os.environ.get("LOKI_PUSH_URL", "http://loki:3100/loki/api/v1/push")
LOKI_QUERY_URL = os.environ.get("LOKI_QUERY_URL", "http://loki:3100")
BACKFILL_DAYS = min(int(os.environ.get("BACKFILL_DAYS", "13")), 13)
BACKFILL_SESSIONS_PER_DAY = int(os.environ.get("BACKFILL_SESSIONS_PER_DAY", "120"))
SEED_SESSIONS = int(os.environ.get("SEED_SESSIONS", "300"))
SESSIONS_PER_MINUTE = float(os.environ.get("SESSIONS_PER_MINUTE", "20"))
APP = {"name": os.environ.get("APP_NAME", "demo-shop"), "version": "1.0.0",
       "environment": os.environ.get("APP_ENV", "poc")}
SITE = "http://localhost:8080"
SDK = {"name": "@grafana/faro-web-sdk", "version": "2.14.0"}

# Same order as the product grid in demo-app/app.js (card N = nth product).
PRODUCTS = [
    ("espresso-beans", "coffee", 18.0, 0.30),
    ("filter-blend", "coffee", 15.5, 0.22),
    ("ceramic-mug", "merch", 12.0, 0.18),
    ("pour-over-kit", "gear", 42.0, 0.14),
    ("grinder-pro", "gear", 129.0, 0.08),
    ("gift-card", "gift", 25.0, 0.08),
]
BROWSERS = [  # name, version, os, mobile, weight
    ("Chrome", "130.0", "Windows 11", False, 0.34),
    ("Chrome", "130.0", "Android 15", True, 0.22),
    ("Safari", "18.1", "iOS 18", True, 0.18),
    ("Safari", "18.1", "Mac OS 15", False, 0.10),
    ("Edge", "130.0", "Windows 11", False, 0.09),
    ("Firefox", "132.0", "Ubuntu", False, 0.07),
]
CAMPAIGNS = [("direct", 0.40), ("google", 0.30), ("newsletter", 0.15), ("instagram", 0.15)]
ERRORS = [
    ("TypeError", "Cannot read properties of null (reading 'checkoutTotal')", "checkout.js"),
    ("ReferenceError", "analytics is not defined", "vendor.js"),
    ("Error", "Payment gateway timeout", "payment.js"),
]

# CSS paths of the demo shop's elements, exactly as demo-app/app.js computes
# them (verified by running the app in a DOM). Keeps generated heatmap clicks
# on real elements.
SEL = {
    "card": "#view > div:nth-of-type(1) > a:nth-of-type({n})",
    "nav_shop": "body > header:nth-of-type(1) > nav:nth-of-type(1) > a:nth-of-type(1)",
    "nav_pricing": "body > header:nth-of-type(1) > nav:nth-of-type(1) > a:nth-of-type(2)",
    "nav_cart": "#cart-link",
    "brand": "body > header:nth-of-type(1) > a:nth-of-type(1)",
    "signup": "#signup-btn",
    "back": "#view > a:nth-of-type(1)",
    "thumb": "#view > div:nth-of-type(1) > div:nth-of-type(1)",
    "add": "#add",
    "price": "#view > div:nth-of-type(1) > div:nth-of-type(2) > p:nth-of-type(1)",
    "checkout": "#checkout",
    "cart_line": "#view > ul:nth-of-type(1) > li:nth-of-type(1)",
    "email": "#view > label:nth-of-type(1) > input:nth-of-type(1)",
    "pay": "#pay",
    "plan": "#view > div:nth-of-type(1) > div:nth-of-type({n}) > button:nth-of-type(1)",
    "search": "#search-btn",
    "wishlist": "#wishlist-btn",
}
PLAN_INDEX = {"free": 1, "pro": 2, "team": 3}


def pick(items):
    return random.choices(items, weights=[i[-1] for i in items])[0]


def iso(ts):
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------- sinks
class LiveSink:
    """Sends Faro SDK payloads to Alloy now (Loki stamps them on arrival)."""

    def send(self, payload, _when):
        post_json(COLLECTOR_URL, payload)


def _fmt(v):
    s = str(v)
    if s == "" or any(c in s for c in ' ="\\\t\n'):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return s


class BackfillSink:
    """Turns Faro payloads into the same logfmt lines Alloy writes and buffers
    them with historical timestamps for a direct Loki push."""

    def __init__(self):
        self.lines = []  # (ns, kind, line)

    def _meta_fields(self, m):
        f = [("sdk_name", m["sdk"]["name"]), ("sdk_version", m["sdk"]["version"]),
             ("app_name", m["app"]["name"]), ("app_version", m["app"]["version"]),
             ("app_environment", m["app"]["environment"]), ("user_id", m["user"]["id"])]
        f += [("user_attr_" + k, v) for k, v in sorted(m["user"]["attributes"].items())]
        f += [("session_id", m["session"]["id"])]
        f += [("session_attr_" + k, v) for k, v in sorted(m["session"]["attributes"].items())]
        b = m["browser"]
        f += [("page_url", m["page"]["url"]), ("browser_name", b["name"]), ("browser_version", b["version"]),
              ("browser_os", b["os"]), ("browser_mobile", str(b["mobile"]).lower()), ("view_name", m["view"]["name"])]
        return f

    def send(self, payload, when):
        meta = self._meta_fields(payload["meta"])
        ts_field = [("timestamp", when.strftime("%Y-%m-%d %H:%M:%S.") + f"{when.microsecond // 1000:03d} +0000 UTC")]
        ns = int(when.timestamp() * 1e9)
        for e in payload["events"]:
            f = ts_field + [("kind", "event"), ("event_name", e["name"]), ("event_domain", e["domain"])]
            f += [("event_data_" + k, v) for k, v in sorted(e["attributes"].items())]
            self.lines.append((ns, "event", " ".join(f"{k}={_fmt(v)}" for k, v in f + meta)))
        for m in payload["measurements"]:
            f = ts_field + [("kind", "measurement"), ("type", m["type"])]
            f += [(k, f"{v:.6f}") for k, v in sorted(m["values"].items())]
            f += [("value_" + k, v) for k, v in sorted(m["values"].items())]
            self.lines.append((ns, "measurement", " ".join(f"{k}={_fmt(v)}" for k, v in f + meta)))
        for x in payload["exceptions"]:
            fr = x["stacktrace"]["frames"][0]
            st = f'{x["type"]}: {x["value"]}\n  at {fr["function"]} ({fr["filename"]}:{fr["lineno"]}:{fr["colno"]})'
            f = ts_field + [("kind", "exception"), ("type", x["type"]), ("value", x["value"]), ("stacktrace", st)]
            self.lines.append((ns + 1, "exception", " ".join(f"{k}={_fmt(v)}" for k, v in f + meta)))

    def flush(self):
        self.lines.sort()
        total, batch_size = len(self.lines), 2000
        for i in range(0, total, batch_size):
            streams = {}
            for ns, kind, line in self.lines[i:i + batch_size]:
                streams.setdefault(kind, []).append([str(ns), line])
            body = {"streams": [{"stream": {"source": "faro", "app_name": APP["name"], "app_env": APP["environment"],
                                            "kind": kind, "origin": "backfill"}, "values": vals}
                                for kind, vals in streams.items()]}
            post_json(LOKI_PUSH_URL, body)
        print(f"[loadgen] backfill: pushed {total} lines to Loki", flush=True)
        # Loki only queries its in-memory ingester for recent data (~3h), so old
        # back-filled lines are invisible until flushed to storage. Force it.
        try:
            urllib.request.urlopen(urllib.request.Request(f"{LOKI_QUERY_URL}/flush", data=b"", method="POST"), timeout=60)
            time.sleep(10)
            print("[loadgen] backfill: flushed to storage, history is queryable", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[loadgen] backfill: flush failed ({e}); history appears once Loki flushes on its own", file=sys.stderr)


# ------------------------------------------------------------------- session
class User:
    def __init__(self, first_seen=None):
        self.id = "user-" + uuid.uuid4().hex[:8]
        self.browser = pick(BROWSERS)
        self.plan = "anonymous"
        self.first_seen = first_seen


RETURNING_USERS = []  # pool for live traffic; ~30% of live sessions are returning users


class Session:
    def __init__(self, sink, start=None, user=None):
        self.sink = sink
        self.clock = start  # None = live (real time)
        if user is None:
            if RETURNING_USERS and random.random() < 0.30:
                user = random.choice(RETURNING_USERS)
            else:
                user = User()
                RETURNING_USERS.append(user)
                del RETURNING_USERS[:-2000]
        self.user = user
        self.session_id = uuid.uuid4().hex[:10]
        self.browser = user.browser
        self.campaign = pick(CAMPAIGNS)[0]
        self.mobile = self.browser[3]
        self.intent = random.uniform(0.6, 1.2) * (0.8 if self.mobile else 1.0) * \
            (1.3 if self.campaign == "newsletter" else 1.0)
        self.view = ("home", "/")

    # time ------------------------------------------------------------------
    def now(self):
        return datetime.now(timezone.utc) if self.clock is None else self.clock

    def think(self, lo=2, hi=25):
        if self.clock is not None:
            self.clock += timedelta(seconds=random.uniform(lo, hi))

    # payload ---------------------------------------------------------------
    def meta(self):
        name, version, os_, mobile, _ = self.browser
        page, path = self.view
        return {
            "sdk": SDK, "app": APP,
            "session": {"id": self.session_id, "attributes": {"campaign": self.campaign}},
            "user": {"id": self.user.id, "attributes": {"plan": self.user.plan}},
            "page": {"url": SITE + "/#" + path},
            "browser": {"name": name, "version": version, "os": os_, "mobile": mobile},
            "view": {"name": page},
        }

    def send(self, events=(), measurements=(), exceptions=()):
        self.sink.send({"meta": self.meta(), "events": list(events), "measurements": list(measurements),
                        "exceptions": list(exceptions), "logs": []}, self.now())

    def event(self, name, **attrs):
        return {"name": name, "domain": "shop", "timestamp": iso(self.now()),
                "attributes": {k: str(v) for k, v in attrs.items()}}

    def click(self, key, label, n=None):
        page, path = self.view
        selector = SEL[key].format(n=n) if n else SEL[key]
        clamp = lambda v: min(1.0, max(0.0, v))
        self.send([self.event("ui_click", page=page, path=path, selector=selector, label=label,
                              rel_x=f"{clamp(random.gauss(0.5, 0.18)):.3f}", rel_y=f"{clamp(random.gauss(0.5, 0.2)):.3f}",
                              x=0, y=0, vw=390 if self.mobile else 1440,
                              device="mobile" if self.mobile else "desktop")])

    def page(self, page, path, *extra):
        self.think()
        self.view = (page, path)
        vitals = []
        if random.random() < 0.7:
            slow = 1.6 if self.mobile else 1.0
            vitals.append({"type": "web-vitals", "timestamp": iso(self.now()), "values": {
                "lcp": round(random.lognormvariate(7.3, 0.35) * slow, 1),
                "fcp": round(random.lognormvariate(6.8, 0.3) * slow, 1),
                "inp": round(random.lognormvariate(4.8, 0.5) * slow, 1),
                "cls": round(max(0.0, random.gauss(0.05, 0.04)), 3),
                "ttfb": round(random.lognormvariate(5.6, 0.4), 1),
            }})
        self.send([self.event("page_view", page=page, path=path), *extra], vitals)

    # behaviour -------------------------------------------------------------
    def run(self):
        self.page("home", "/")
        if random.random() < 0.10:
            feature = random.choice(["search", "wishlist"])
            self.think(1, 5)
            self.click(feature, "Use " + feature)
            self.send([self.event("feature_used", feature=feature)])
        if random.random() < 0.08:
            self.click("nav_pricing", "Pricing")
            self.user.plan = random.choices(["free", "pro", "team"], weights=[6, 3, 1])[0]
            self.page("pricing", "/pricing")
            self.think(3, 15)
            self.click("plan", "Choose " + self.user.plan, n=PLAN_INDEX[self.user.plan])
            self.send([self.event("signup", plan=self.user.plan)])
            self.click("nav_shop", "Shop")
            self.page("home", "/")

        if random.random() > 0.72 * self.intent:
            if random.random() < 0.3:
                self.click("brand", "☕ Demo Shop")  # wandering clicks before leaving
            return self.maybe_error()
        cart = []
        for i in range(random.choices([1, 2, 3], weights=[6, 3, 1])[0]):
            idx = PRODUCTS.index(pick(PRODUCTS))
            pid, cat, price, _w = PRODUCTS[idx]
            self.think(1, 6)
            self.click("card", pid.replace("-", " ").title(), n=idx + 1)
            path = "/product/" + pid
            self.page("product", path, self.event("product_viewed", product=pid, category=cat, price=price))
            if random.random() < 0.35:
                for _ in range(random.choice([1, 1, 3])):  # people click product images (dead clicks)
                    self.click("thumb", cat.upper())
            if random.random() < 0.42 * self.intent:
                self.think(2, 12)
                self.click("add", "Add to cart")
                cart.append((pid, cat, price))
                self.send([self.event("add_to_cart", product=pid, category=cat, price=price)])
                if i < 2 and random.random() < 0.5:
                    self.click("nav_shop", "Shop")
                    self.page("home", "/")
            elif random.random() < 0.6:
                self.click("back", "← Back")
                self.page("home", "/")
        if not cart:
            return self.maybe_error()

        value = sum(p[2] for p in cart)
        if self.view[0] != "cart":
            self.click("nav_cart", f"Cart ({len(cart)})")
        self.page("cart", "/cart")
        if random.random() < 0.2:
            self.click("cart_line", cart[0][0].replace("-", " ").title())
        if random.random() > 0.62 * self.intent:
            return self.maybe_error()
        self.think(2, 10)
        self.click("checkout", "Checkout")
        self.send([self.event("checkout_started", items=len(cart), cart_value=f"{value:.2f}")])
        self.page("checkout", "/checkout")
        self.click("email", "INPUT")
        if random.random() < 0.06:
            self.click("pay", "Place order")
            return self.error(ERRORS[2])
        if random.random() > 0.70 * self.intent:
            return None
        self.think(5, 30)
        self.click("pay", "Place order")
        order = "ord-" + uuid.uuid4().hex[:8]
        self.send([self.event("purchase", order_id=order, items=len(cart), revenue=f"{value:.2f}")])
        self.page("success", "/success/" + order)
        return None

    def maybe_error(self):
        if random.random() < 0.04:
            self.error(random.choice(ERRORS[:2]))

    def error(self, err):
        etype, value, filename = err
        self.send(exceptions=[{
            "type": etype, "value": value, "timestamp": iso(self.now()),
            "stacktrace": {"frames": [{"filename": f"{SITE}/{filename}", "function": "handler",
                                       "lineno": random.randint(10, 400), "colno": random.randint(1, 80)}]},
        }])


# ---------------------------------------------------------------------- http
def post_json(url, payload, retries=5):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status in (200, 202, 204):
                    return True
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if 400 <= e.code < 500 and e.code != 429:
                print(f"[loadgen] {url} rejected payload ({e.code}): {msg}", file=sys.stderr)
                return False
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt == retries - 1:
                print(f"[loadgen] giving up on {url}: {e}", file=sys.stderr)
                return False
        time.sleep(1 + attempt)
    return False


def wait_for(url, probe):
    for _ in range(120):
        try:
            if probe is None:
                urllib.request.urlopen(url, timeout=5)
            else:
                urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(probe).encode(), method="POST",
                                                              headers={"Content-Type": "application/json"}), timeout=5)
            return
        except Exception:  # noqa: BLE001
            time.sleep(2)
    sys.exit(f"[loadgen] {url} not reachable")


def history_exists():
    q = urllib.parse.urlencode({"query": 'sum(count_over_time({source="faro"}[13d]))',
                                "time": str(int((time.time() - 2 * 86400) * 1e9))})
    try:
        with urllib.request.urlopen(f"{LOKI_QUERY_URL}/loki/api/v1/query?{q}", timeout=30) as r:
            res = json.load(r)["data"]["result"]
            return bool(res) and float(res[0]["value"][1]) > 0
    except Exception as e:  # noqa: BLE001
        print(f"[loadgen] could not check for history ({e}); skipping backfill", file=sys.stderr)
        return True


# ------------------------------------------------------------------ backfill
def backfill():
    """BACKFILL_DAYS of history with realistic retention: new users each day,
    and earlier users returning with a probability that decays with age."""
    sink, users = BackfillSink(), []
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(BACKFILL_DAYS, 0, -1):
        day = today - timedelta(days=d)
        weekend = day.weekday() >= 5
        volume = BACKFILL_SESSIONS_PER_DAY * (0.7 if weekend else 1.0) * random.uniform(0.9, 1.1)
        visitors = []
        for u in users:  # returning users: ~22% next day, decaying to a ~6% floor
            age = (day - u.first_seen).days
            if random.random() < 0.06 + 0.16 * math.exp(-(age - 1) / 4):
                visitors.append(u)
        for _ in range(max(0, int(volume) - len(visitors))):
            u = User(first_seen=day)
            users.append(u)
            visitors.append(u)
        for u in visitors:
            # Daytime-weighted start time (IST-ish peak, expressed in UTC)
            start = day + timedelta(hours=min(23.5, max(0.0, random.gauss(9, 4))), minutes=random.uniform(0, 59))
            Session(sink, start=start, user=u).run()
        print(f"[loadgen] backfill {day:%Y-%m-%d}: {len(visitors)} sessions", flush=True)
    sink.flush()


def main():
    wait_for(COLLECTOR_URL, {"meta": {"app": APP, "sdk": {"name": "loadgen-probe", "version": "0"}},
                             "events": [], "measurements": [], "exceptions": [], "logs": []})
    print(f"[loadgen] collector reachable at {COLLECTOR_URL}", flush=True)
    if BACKFILL_DAYS > 0:
        wait_for(f"{LOKI_QUERY_URL}/ready", None)
        if history_exists():
            print("[loadgen] history already present, skipping backfill", flush=True)
        else:
            print(f"[loadgen] back-filling {BACKFILL_DAYS} days of history...", flush=True)
            backfill()
    live = LiveSink()
    print(f"[loadgen] seeding {SEED_SESSIONS} sessions...", flush=True)
    for i in range(SEED_SESSIONS):
        Session(live).run()
        if (i + 1) % 100 == 0:
            print(f"[loadgen]   {i + 1}/{SEED_SESSIONS}", flush=True)
    if SESSIONS_PER_MINUTE <= 0:
        print("[loadgen] seed done, exiting", flush=True)
        return
    print(f"[loadgen] live traffic: ~{SESSIONS_PER_MINUTE:g} sessions/min", flush=True)
    while True:
        Session(live).run()
        time.sleep(random.expovariate(SESSIONS_PER_MINUTE / 60.0))


if __name__ == "__main__":
    main()
