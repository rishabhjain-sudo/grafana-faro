/* Demo Shop: a tiny single-page shop instrumented with Grafana Faro.
 *
 * Product analytics events (all go through faro.api.pushEvent):
 *   page_view         {page, path}
 *   product_viewed    {product, category, price}
 *   add_to_cart       {product, category, price}
 *   checkout_started  {items, cart_value}
 *   purchase          {order_id, items, revenue}
 *   signup            {plan}
 *   feature_used      {feature}
 * Faro's web instrumentations also send web vitals, JS errors and sessions
 * automatically.
 */
(function () {
  'use strict';

  const PRODUCTS = [
    { id: 'espresso-beans', name: 'Espresso Beans', category: 'coffee', price: 18.0 },
    { id: 'filter-blend', name: 'Filter Blend', category: 'coffee', price: 15.5 },
    { id: 'ceramic-mug', name: 'Ceramic Mug', category: 'merch', price: 12.0 },
    { id: 'pour-over-kit', name: 'Pour-over Kit', category: 'gear', price: 42.0 },
    { id: 'grinder-pro', name: 'Grinder Pro', category: 'gear', price: 129.0 },
    { id: 'gift-card', name: 'Gift Card', category: 'gift', price: 25.0 },
  ];

  // ---------------------------------------------------------------- Faro ---
  const { initializeFaro, getWebInstrumentations } = window.GrafanaFaroWebSdk;
  const cfg = window.FARO_CONFIG;
  // ?embed=1 is used by the heatmap viewer to show the page without tracking it.
  const EMBED = new URLSearchParams(location.search).has('embed');

  // Analytics must never break the product. If Faro can't start (blocked
  // script, old browser, ...), fall back to a no-op API.
  const noop = () => {};
  let faro = { api: { pushEvent: noop, setUser: noop, setView: noop } };
  try {
    if (EMBED) throw new Error('embed mode: tracking disabled');
    const instrumentations = getWebInstrumentations({ captureConsole: false });
    // Session replay (rrweb), recorded through the same Faro pipeline.
    const Replay = window.GrafanaFaroInstrumentationReplay;
    if (Replay && cfg.replay && cfg.replay.enabled) {
      instrumentations.push(new Replay.ReplayInstrumentation({
        samplingRate: cfg.replay.samplingRate,
        maskAllInputs: true,                  // never record what people type
        maskTextSelector: '[data-private]',   // demo: show page text, mask only marked elements
      }));
    }
    faro = initializeFaro({
      url: cfg.collectorUrl,
      app: cfg.app,
      instrumentations,
      sessionTracking: { enabled: true, persistent: true },
    });
  } catch (err) {
    if (!EMBED) console.warn('[demo-shop] Faro failed to initialise, analytics disabled:', err);
  }

  // Faro event attributes must be strings.
  function track(name, attrs) {
    const clean = {};
    Object.keys(attrs || {}).forEach((k) => { clean[k] = String(attrs[k]); });
    faro.api.pushEvent(name, clean, 'shop');
  }

  // Anonymous but stable visitor id, like a typical analytics cookie.
  function visitor() {
    let v = JSON.parse(localStorage.getItem('demo-visitor') || 'null');
    if (!v) {
      v = { id: 'user-' + Math.random().toString(36).slice(2, 10), plan: 'free' };
      localStorage.setItem('demo-visitor', JSON.stringify(v));
    }
    return v;
  }
  function identify() {
    const v = visitor();
    faro.api.setUser({ id: v.id, attributes: { plan: v.plan } });
    document.getElementById('user-label').textContent = v.id + ' · ' + v.plan;
  }

  // ---------------------------------------------------------------- Cart ---
  const cart = [];
  // Heatmap preview (?embed=1): show cart/checkout with a sample item so their
  // buttons exist to draw clicks on.
  if (EMBED) cart.push(PRODUCTS[0]);
  const cartValue = () => cart.reduce((s, p) => s + p.price, 0);
  function updateCartLink() {
    document.getElementById('cart-link').textContent = 'Cart (' + cart.length + ')';
  }

  // -------------------------------------------------------------- Views ----
  const $view = document.getElementById('view');
  const money = (n) => '$' + n.toFixed(2);

  const views = {
    home() {
      $view.innerHTML = '<h1>Fresh coffee, delivered</h1><div class="grid">' +
        PRODUCTS.map((p) =>
          `<a class="card" href="#/product/${p.id}"><div class="thumb">${p.category}</div>` +
          `<h3>${p.name}</h3><p>${money(p.price)}</p></a>`).join('') + '</div>';
    },
    product(id) {
      const p = PRODUCTS.find((x) => x.id === id);
      if (!p) return views.notFound();
      track('product_viewed', { product: p.id, category: p.category, price: p.price });
      $view.innerHTML = `<a href="#/">← Back</a><div class="detail"><div class="thumb big">${p.category}</div>` +
        `<div><h1>${p.name}</h1><p class="price">${money(p.price)}</p>` +
        `<button id="add">Add to cart</button></div></div>`;
      document.getElementById('add').onclick = () => {
        cart.push(p);
        updateCartLink();
        track('add_to_cart', { product: p.id, category: p.category, price: p.price });
        location.hash = '#/cart';
      };
    },
    cart() {
      if (!cart.length) {
        $view.innerHTML = '<h1>Your cart is empty</h1><a href="#/">Browse products</a>';
        return;
      }
      $view.innerHTML = '<h1>Cart</h1><ul class="lines">' +
        cart.map((p) => `<li>${p.name}<span>${money(p.price)}</span></li>`).join('') +
        `</ul><p class="total">Total ${money(cartValue())}</p><button id="checkout">Checkout</button>`;
      document.getElementById('checkout').onclick = () => {
        track('checkout_started', { items: cart.length, cart_value: cartValue().toFixed(2) });
        location.hash = '#/checkout';
      };
    },
    checkout() {
      if (!cart.length) { location.hash = '#/'; return; }
      $view.innerHTML = '<h1>Checkout</h1><label>Email <input value="demo@example.com"></label>' +
        `<p class="total">Pay ${money(cartValue())}</p><button id="pay">Place order</button>`;
      document.getElementById('pay').onclick = () => {
        const orderId = 'ord-' + Date.now().toString(36);
        track('purchase', { order_id: orderId, items: cart.length, revenue: cartValue().toFixed(2) });
        cart.length = 0;
        updateCartLink();
        location.hash = '#/success/' + orderId;
      };
    },
    success(orderId) {
      $view.innerHTML = `<h1>Thank you! 🎉</h1><p>Order <code>${orderId}</code> confirmed.</p><a href="#/">Keep shopping</a>`;
    },
    pricing() {
      $view.innerHTML = '<h1>Membership</h1><div class="grid">' +
        ['free', 'pro', 'team'].map((plan) =>
          `<div class="card"><h3>${plan}</h3><button data-plan="${plan}">Choose ${plan}</button></div>`).join('') +
        '</div>';
      $view.querySelectorAll('button[data-plan]').forEach((b) => {
        b.onclick = () => signup(b.dataset.plan);
      });
    },
    notFound() { $view.innerHTML = '<h1>Not found</h1>'; },
  };

  function signup(plan) {
    const v = visitor();
    v.plan = plan || 'free';
    localStorage.setItem('demo-visitor', JSON.stringify(v));
    identify();
    track('signup', { plan: v.plan });
  }

  // ------------------------------------------------------- Click heatmap ---
  // Each click is stored relative to the element that was clicked (CSS path +
  // position inside it), so the heatmap lines up on any screen size.
  let current = { page: 'home', path: '/' };
  function cssPath(el) {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.body && el !== document.documentElement) {
      if (el.id) { parts.unshift('#' + el.id); return parts.join(' > '); }
      let i = 1;
      for (let s = el.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      el = el.parentElement;
    }
    parts.unshift('body');
    return parts.join(' > ');
  }
  window.demoCssPath = cssPath; // used by tools/tests to keep the traffic generator in sync
  document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element)) return;
    const el = e.target.closest('a, button, input, select, textarea, label, [data-track]') || e.target;
    const r = el.getBoundingClientRect();
    const rel = (v, start, size) => (size > 0 ? Math.min(1, Math.max(0, (v - start) / size)) : 0.5).toFixed(3);
    track('ui_click', {
      page: current.page, path: current.path, selector: cssPath(el),
      label: (el.innerText || el.value || el.getAttribute('aria-label') || el.tagName).trim().replace(/\s+/g, ' ').slice(0, 40),
      rel_x: rel(e.clientX, r.left, r.width), rel_y: rel(e.clientY, r.top, r.height),
      x: Math.round(e.pageX), y: Math.round(e.pageY), vw: window.innerWidth,
      device: window.innerWidth < 768 ? 'mobile' : 'desktop',
    });
  }, true);

  // ------------------------------------------------------------- Router ----
  function route() {
    const parts = (location.hash.replace(/^#\/?/, '') || 'home').split('/');
    const page = parts[0] || 'home';
    current = { page: page, path: '/' + location.hash.replace(/^#\/?/, '') };
    faro.api.setView({ name: page });
    track('page_view', { page: page, path: current.path });
    (views[page] || views.notFound)(parts[1]);
    window.scrollTo(0, 0);
  }

  // ------------------------------------------------------------- Wiring ----
  document.getElementById('signup-btn').onclick = () => signup('free');
  document.getElementById('search-btn').onclick = () => track('feature_used', { feature: 'search' });
  document.getElementById('wishlist-btn').onclick = () => track('feature_used', { feature: 'wishlist' });
  document.getElementById('error-btn').onclick = () => {
    // Uncaught on purpose. Faro's error instrumentation reports it.
    setTimeout(() => { null.checkoutTotal(); }, 0);
  };
  document.getElementById('reset-btn').onclick = () => {
    localStorage.clear();
    sessionStorage.clear();
    location.hash = '#/';
    location.reload();
  };

  if (EMBED) document.body.classList.add('embed');
  identify();
  window.addEventListener('hashchange', route);
  route();
})();
