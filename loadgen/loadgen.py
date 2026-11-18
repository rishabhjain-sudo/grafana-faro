#!/usr/bin/env python3
"""
Synthetic traffic for the Faro analytics POC.

Sends payloads in the Grafana Faro Web SDK format to Alloy's faro.receiver.
They are the same shape the browser SDK sends, so the dashboard treats them
exactly like real visitors.

Each simulated session walks the shop funnel with realistic drop-off:
  page_view(home) -> product_viewed -> add_to_cart -> checkout_started -> purchase
plus signups, feature usage, web vitals and the occasional JS error.

Loki stamps lines on arrival, so history can't be back-filled. The generator
sends a seed burst (SEED_SESSIONS) and then a steady live stream
(SESSIONS_PER_MINUTE).

Env:
  COLLECTOR_URL        default http://alloy:12347/collect
  SEED_SESSIONS        default 300
  SESSIONS_PER_MINUTE  default 30   (0 = stop after the seed burst)
  APP_NAME / APP_ENV   default demo-shop / poc
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

COLLECTOR_URL = os.environ.get("COLLECTOR_URL", "http://alloy:12347/collect")
SEED_SESSIONS = int(os.environ.get("SEED_SESSIONS", "300"))
SESSIONS_PER_MINUTE = float(os.environ.get("SESSIONS_PER_MINUTE", "30"))
APP = {"name": os.environ.get("APP_NAME", "demo-shop"), "version": "1.0.0",
       "environment": os.environ.get("APP_ENV", "poc")}
SITE = "http://localhost:8080"

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


def pick(items):
    return random.choices(items, weights=[i[-1] for i in items])[0]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


RETURNING_USERS = []  # pool of past visitors; ~30% of sessions are returning users


class Session:
    def __init__(self):
        if RETURNING_USERS and random.random() < 0.30:
            self.user_id = random.choice(RETURNING_USERS)
        else:
            self.user_id = "user-" + uuid.uuid4().hex[:8]
            RETURNING_USERS.append(self.user_id)
            del RETURNING_USERS[:-2000]
        self.session_id = uuid.uuid4().hex[:10]
        self.browser = pick(BROWSERS)
        self.campaign = pick(CAMPAIGNS)[0]
        self.plan = "anonymous"
        # Mobile converts a bit worse, newsletter traffic a bit better.
        self.intent = random.uniform(0.6, 1.2) * (0.8 if self.browser[3] else 1.0) * \
            (1.3 if self.campaign == "newsletter" else 1.0)

    def meta(self, page, path):
        name, version, os_, mobile, _ = self.browser
        return {
            "sdk": {"name": "@grafana/faro-web-sdk", "version": "2.14.0"},
            "app": APP,
            "session": {"id": self.session_id, "attributes": {"campaign": self.campaign}},
            "user": {"id": self.user_id, "attributes": {"plan": self.plan}},
            "page": {"url": SITE + "/#" + path},
            "browser": {"name": name, "version": version, "os": os_, "mobile": mobile},
            "view": {"name": page},
        }

    def send(self, page, path, events=(), measurements=(), exceptions=()):
        payload = {"meta": self.meta(page, path), "events": list(events),
                   "measurements": list(measurements), "exceptions": list(exceptions), "logs": []}
        post(payload)

    @staticmethod
    def event(name, **attrs):
        return {"name": name, "domain": "shop", "timestamp": now_iso(),
                "attributes": {k: str(v) for k, v in attrs.items()}}

    def page(self, page, path, *extra):
        vitals = []
        if random.random() < 0.7:  # not every page view reports vitals
            slow = 1.6 if self.browser[3] else 1.0
            vitals.append({"type": "web-vitals", "timestamp": now_iso(), "values": {
                "lcp": round(random.lognormvariate(7.3, 0.35) * slow, 1),   # ~1.5 s median
                "fcp": round(random.lognormvariate(6.8, 0.3) * slow, 1),
                "inp": round(random.lognormvariate(4.8, 0.5) * slow, 1),    # ~120 ms median
                "cls": round(max(0.0, random.gauss(0.05, 0.04)), 3),
                "ttfb": round(random.lognormvariate(5.6, 0.4), 1),
            }})
        self.send(page, path, [self.event("page_view", page=page, path=path), *extra], vitals)

    def run(self):
        self.page("home", "/")
        if random.random() < 0.10:
            self.send("home", "/", [self.event("feature_used", feature=random.choice(["search", "wishlist", "filter"]))])
        if random.random() < 0.08:
            self.plan = random.choices(["free", "pro", "team"], weights=[6, 3, 1])[0]
            self.page("pricing", "/pricing")
            self.send("pricing", "/pricing", [self.event("signup", plan=self.plan)])

        if random.random() > 0.72 * self.intent:
            return self.maybe_error("home", "/")
        cart = []
        for _ in range(random.choices([1, 2, 3], weights=[6, 3, 1])[0]):
            pid, cat, price, _w = pick(PRODUCTS)
            path = "/product/" + pid
            self.page("product", path, self.event("product_viewed", product=pid, category=cat, price=price))
            if random.random() < 0.42 * self.intent:
                cart.append((pid, cat, price))
                self.send("product", path, [self.event("add_to_cart", product=pid, category=cat, price=price)])
        if not cart:
            return self.maybe_error("product", "/product")

        value = sum(p[2] for p in cart)
        self.page("cart", "/cart")
        if random.random() > 0.62 * self.intent:
            return self.maybe_error("cart", "/cart")
        self.send("cart", "/cart", [self.event("checkout_started", items=len(cart), cart_value=f"{value:.2f}")])
        self.page("checkout", "/checkout")
        if random.random() < 0.06:  # payment errors sometimes kill the order
            return self.error("checkout", "/checkout", ERRORS[2])
        if random.random() > 0.70 * self.intent:
            return None
        order = "ord-" + uuid.uuid4().hex[:8]
        self.send("checkout", "/checkout",
                  [self.event("purchase", order_id=order, items=len(cart), revenue=f"{value:.2f}")])
        self.page("success", "/success/" + order)
        return None

    def maybe_error(self, page, path):
        if random.random() < 0.04:
            self.error(page, path, random.choice(ERRORS[:2]))

    def error(self, page, path, err):
        etype, value, filename = err
        self.send(page, path, exceptions=[{
            "type": etype, "value": value, "timestamp": now_iso(),
            "stacktrace": {"frames": [{"filename": f"{SITE}/{filename}", "function": "handler",
                                       "lineno": random.randint(10, 400), "colno": random.randint(1, 80)}]},
        }])


def post(payload, retries=5):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(COLLECTOR_URL, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status in (200, 202, 204):
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt == retries - 1:
                print(f"[loadgen] giving up on payload: {e}", file=sys.stderr)
                return
        time.sleep(1 + attempt)


def wait_for_collector():
    probe = {"meta": {"app": APP, "sdk": {"name": "loadgen-probe", "version": "0"}},
             "events": [], "measurements": [], "exceptions": [], "logs": []}
    for i in range(120):
        try:
            req = urllib.request.Request(COLLECTOR_URL, data=json.dumps(probe).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
            print(f"[loadgen] collector reachable at {COLLECTOR_URL}", flush=True)
            return
        except Exception:  # noqa: BLE001
            time.sleep(2)
    sys.exit(f"[loadgen] collector {COLLECTOR_URL} not reachable")


def main():
    wait_for_collector()
    print(f"[loadgen] seeding {SEED_SESSIONS} sessions...", flush=True)
    for i in range(SEED_SESSIONS):
        Session().run()
        if (i + 1) % 50 == 0:
            print(f"[loadgen]   {i + 1}/{SEED_SESSIONS}", flush=True)
    if SESSIONS_PER_MINUTE <= 0:
        print("[loadgen] seed done, exiting", flush=True)
        return
    print(f"[loadgen] live traffic: ~{SESSIONS_PER_MINUTE:g} sessions/min", flush=True)
    while True:
        Session().run()
        time.sleep(random.expovariate(SESSIONS_PER_MINUTE / 60.0))


if __name__ == "__main__":
    main()
