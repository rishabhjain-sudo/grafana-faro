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

  // Analytics must never break the product. If Faro can't start (blocked
  // script, old browser, ...), fall back to a no-op API.
  const noop = () => {};
  let faro = { api: { pushEvent: noop, setUser: noop, setView: noop } };
  try {
    faro = initializeFaro({
      url: cfg.collectorUrl,
      app: cfg.app,
      instrumentations: getWebInstrumentations({ captureConsole: false }),
      sessionTracking: { enabled: true, persistent: true },
    });
  } catch (err) {
    console.warn('[demo-shop] Faro failed to initialise, analytics disabled:', err);
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

  // ------------------------------------------------------------- Router ----
  function route() {
    const parts = (location.hash.replace(/^#\/?/, '') || 'home').split('/');
    const page = parts[0] || 'home';
    faro.api.setView({ name: page });
    track('page_view', { page: page, path: '/' + location.hash.replace(/^#\/?/, '') });
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

  identify();
  window.addEventListener('hashchange', route);
  route();
})();
