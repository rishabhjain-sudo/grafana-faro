/* Insights: shared helpers for the product analytics pages.
 *
 * Data comes straight from Loki (through the read-only /loki proxy on the same
 * origin). Heavy lifting (ordered funnels, retention cohorts, user paths,
 * heatmaps) happens here in the browser over compact event rows. That's fine
 * for POC volumes. At scale you'd move it to ClickHouse or Loki recording rules.
 *
 * The analysis functions are pure, so tools/test_insights.mjs can run them
 * against a real Loki.
 */
(function (root) {
  'use strict';

  const params = typeof location !== 'undefined' ? new URLSearchParams(location.search) : new URLSearchParams();
  const CFG = {
    loki: (root.INSIGHTS_LOKI_URL || '') + '/loki/api/v1',
    app: params.get('app') || 'demo-shop',
    env: params.get('env') || 'poc',
  };
  const EV = () => `{source="faro", app_name="${CFG.app}", app_env="${CFG.env}", kind="event"}`;
  const NS = 1e6; // ns per ms

  // ------------------------------------------------------------------ Loki --
  async function get(path, q) {
    const url = `${CFG.loki}/${path}?${new URLSearchParams(q)}`;
    const r = await fetch(url);
    const body = await r.json().catch(() => ({}));
    if (!r.ok || body.status !== 'success') throw new Error(`Loki ${path}: ${body.error || body.message || r.status}`);
    return body.data;
  }

  /** Instant metric query -> [{metric, value}] */
  async function instant(expr, atMs = Date.now()) {
    const d = await get('query', { query: expr, time: String(Math.round(atMs) * NS) });
    return d.result.map((s) => ({ metric: s.metric, value: parseFloat(s.value[1]) }));
  }

  /** Range metric query -> [{metric, points: [[ms, value]]}] */
  async function range(expr, startMs, endMs, stepSec) {
    const d = await get('query_range', { query: expr, start: String(startMs * NS), end: String(endMs * NS), step: `${stepSec}s` });
    return d.result.map((s) => ({ metric: s.metric, points: s.values.map(([t, v]) => [t * 1000, parseFloat(v)]) }));
  }

  /** Log query, paginated backwards through time -> [{ts (ms), line, labels}] sorted ascending.
   *  Stops at maxLines. Returns {rows, truncated}. */
  async function logs(expr, startMs, endMs, { maxLines = 200000, page = 20000 } = {}) {
    const rows = [];
    let end = BigInt(Math.round(endMs)) * 1000000n;
    const start = BigInt(Math.round(startMs)) * 1000000n;
    let truncated = false;
    for (;;) {
      const d = await get('query_range', { query: expr, start: start.toString(), end: end.toString(), limit: String(page), direction: 'backward' });
      let oldest = null, n = 0;
      for (const s of d.result) {
        for (const [t, line] of s.values) {
          const tn = BigInt(t);
          rows.push({ ts: Number(tn / 1000000n), line, labels: s.stream, _ns: tn });
          if (oldest === null || tn < oldest) oldest = tn;
          n++;
        }
      }
      if (n < page || oldest === null) break;
      if (rows.length >= maxLines) { truncated = true; break; }
      end = oldest; // `end` is exclusive in Loki, so we continue strictly before the oldest row
    }
    rows.sort((a, b) => (a._ns < b._ns ? -1 : a._ns > b._ns ? 1 : 0));
    return { rows, truncated };
  }

  // --------------------------------------------------------------- logfmt --
  function parseLogfmt(line) {
    const out = {};
    let i = 0;
    const n = line.length;
    while (i < n) {
      while (i < n && line[i] === ' ') i++;
      let k = '';
      while (i < n && line[i] !== '=' && line[i] !== ' ') k += line[i++];
      if (line[i] !== '=') { if (k) out[k] = ''; continue; }
      i++;
      let v = '';
      if (line[i] === '"') {
        i++;
        while (i < n && line[i] !== '"') {
          if (line[i] === '\\' && i + 1 < n) {
            const c = line[i + 1];
            v += c === 'n' ? '\n' : c === 't' ? '\t' : c;
            i += 2;
          } else v += line[i++];
        }
        i++;
      } else {
        while (i < n && line[i] !== ' ') v += line[i++];
      }
      out[k] = v;
    }
    return out;
  }

  /** Tab-separated rows produced by `| line_format "{{.a}}\t{{.b}}"` */
  function tsv(rows, fields) {
    return rows.map((r) => {
      const parts = r.line.split('\t');
      const o = { ts: r.ts };
      fields.forEach((f, i) => { o[f] = parts[i] === undefined ? '' : parts[i]; });
      return o;
    });
  }
  const fmtLine = (fields) => `| line_format "${fields.map((f) => `{{.${f}}}`).join('\\t')}"`;

  // ------------------------------------------------------- data fetchers ---
  const SESSION_FIELDS = ['session_id', 'user_id', 'event_name', 'event_data_path', 'event_data_page', 'browser_mobile', 'session_attr_campaign', 'browser_name', 'event_data_revenue'];

  /** Compact product events (excludes replay payloads and clicks). */
  async function fetchEvents(startMs, endMs, names) {
    const filter = names ? `| event_name=~"${names.map((s) => s.replace(/\./g, '\\\\.')).join('|')}"`
      : '| event_name!~"faro\\\\.session_recording\\\\.event|ui_click"';
    const expr = `${EV()} | logfmt ${filter} ${fmtLine(SESSION_FIELDS)}`;
    const { rows, truncated } = await logs(expr, startMs, endMs);
    return { events: tsv(rows, SESSION_FIELDS), truncated };
  }

  // ------------------------------------------------------------ analysis ---
  function bySession(events) {
    const m = new Map();
    for (const e of events) {
      if (!e.session_id) continue;
      let s = m.get(e.session_id);
      if (!s) { s = { id: e.session_id, user: e.user_id, events: [], mobile: e.browser_mobile === 'true', campaign: e.session_attr_campaign || 'direct', browser: e.browser_name }; m.set(e.session_id, s); }
      s.events.push(e);
    }
    for (const s of m.values()) s.events.sort((a, b) => a.ts - b.ts);
    return m;
  }

  /** Strict ordered funnel: a session counts for step i only if it did step 1..i
   *  in that order (each step after the previous one). */
  function computeFunnel(sessions, steps, segmentFn) {
    const segs = new Map();
    for (const s of sessions.values()) {
      const seg = segmentFn ? segmentFn(s) : 'all';
      if (!segs.has(seg)) segs.set(seg, { counts: steps.map(() => 0), times: steps.map(() => []), stuck: steps.map(() => []) });
      const g = segs.get(seg);
      let at = -Infinity, reached = 0, prevTs = null;
      for (let i = 0; i < steps.length; i++) {
        const hit = s.events.find((e) => e.event_name === steps[i] && e.ts >= at);
        if (!hit) break;
        g.counts[i]++;
        if (prevTs !== null) g.times[i].push(hit.ts - prevTs);
        prevTs = hit.ts; at = hit.ts; reached = i + 1;
      }
      if (reached > 0 && reached < steps.length) g.stuck[reached - 1].push(s);
    }
    const out = [];
    for (const [seg, g] of segs) {
      out.push({
        segment: seg,
        steps: steps.map((name, i) => ({
          name, count: g.counts[i],
          fromStart: g.counts[0] ? g.counts[i] / g.counts[0] : 0,
          fromPrev: i === 0 ? 1 : (g.counts[i - 1] ? g.counts[i] / g.counts[i - 1] : 0),
          dropped: i === 0 ? 0 : g.counts[i - 1] - g.counts[i],
          medianMs: median(g.times[i]),
          stuckSessions: g.stuck[i],
        })),
      });
    }
    return out.sort((a, b) => b.steps[0].count - a.steps[0].count);
  }

  /** Last page each session saw (for sessions in `sessions`). */
  function exitPages(sessionList, key = 'event_data_path') {
    const c = new Map();
    for (const s of sessionList) {
      const pv = s.events.filter((e) => e.event_name === 'page_view');
      const last = pv.length ? (pv[pv.length - 1][key] || '(unknown)') : '(no page view)';
      c.set(last, (c.get(last) || 0) + 1);
    }
    return [...c.entries()].sort((a, b) => b[1] - a[1]);
  }

  /** Cohort retention from per-user activity series.
   *  series: [{metric:{user_id}, points:[[ms, count]]}] with one point per bucket. */
  function computeRetention(series, bucketMs, nowMs) {
    const firstBucket = new Map(); // user -> first active bucket index
    const active = new Map();      // user -> Set(bucket index)
    let minT = Infinity;
    for (const s of series) for (const [t, v] of s.points) if (v > 0) minT = Math.min(minT, t);
    if (!isFinite(minT)) return { cohorts: [], buckets: 0 };
    const idx = (t) => Math.round((t - minT) / bucketMs);
    let maxIdx = 0;
    for (const s of series) {
      const u = s.metric.user_id;
      for (const [t, v] of s.points) {
        if (v <= 0) continue;
        const i = idx(t);
        maxIdx = Math.max(maxIdx, i);
        if (!active.has(u)) active.set(u, new Set());
        active.get(u).add(i);
        if (!firstBucket.has(u) || i < firstBucket.get(u)) firstBucket.set(u, i);
      }
    }
    const cohorts = [];
    for (let c = 0; c <= maxIdx; c++) {
      const users = [...firstBucket.entries()].filter(([, f]) => f === c).map(([u]) => u);
      const row = { start: minT + c * bucketMs - bucketMs, size: users.length, cells: [] };
      for (let k = 0; c + k <= maxIdx; k++) {
        const n = users.filter((u) => active.get(u).has(c + k)).length;
        const complete = (minT + (c + k) * bucketMs) <= nowMs; // bucket fully elapsed?
        row.cells.push({ k, n, pct: users.length ? n / users.length : 0, partial: !complete });
      }
      cohorts.push(row);
    }
    return { cohorts, buckets: maxIdx + 1 };
  }

  /** Page sequences per session. Consecutive duplicates collapsed, '(exit)' appended. */
  function sessionPaths(sessions, key = 'event_data_page', maxSteps = 6) {
    const paths = [];
    for (const s of sessions.values()) {
      const seq = [];
      for (const e of s.events) {
        if (e.event_name !== 'page_view') continue;
        const p = e[key] || '(unknown)';
        if (seq[seq.length - 1] !== p) seq.push(p);
      }
      if (!seq.length) continue;
      const trimmed = seq.slice(0, maxSteps);
      trimmed.push(seq.length > maxSteps ? '…' : '(exit)');
      paths.push({ session: s, seq: trimmed });
    }
    return paths;
  }

  function topSequences(paths, n = 15) {
    const c = new Map();
    for (const p of paths) { const k = p.seq.join(' → '); c.set(k, (c.get(k) || 0) + 1); }
    return [...c.entries()].sort((a, b) => b[1] - a[1]).slice(0, n);
  }

  /** Transitions around a page: what came next / before it. */
  function transitions(paths, page) {
    const next = new Map(), prev = new Map();
    let total = 0;
    for (const { seq } of paths) {
      seq.forEach((p, i) => {
        if (p !== page) return;
        total++;
        const nx = seq[i + 1] || '(exit)';
        const pv = i === 0 ? '(entry)' : seq[i - 1];
        next.set(nx, (next.get(nx) || 0) + 1);
        prev.set(pv, (prev.get(pv) || 0) + 1);
      });
    }
    const sort = (m) => [...m.entries()].sort((a, b) => b[1] - a[1]);
    return { total, next: sort(next), prev: sort(prev) };
  }

  /** Sankey-style flow: nodes per step (top K + other), links between steps. */
  function flow(paths, steps = 5, topK = 6) {
    const cols = [];
    for (let i = 0; i < steps; i++) {
      const c = new Map();
      for (const { seq } of paths) if (seq[i] !== undefined) c.set(seq[i], (c.get(seq[i]) || 0) + 1);
      const sorted = [...c.entries()].sort((a, b) => b[1] - a[1]);
      const keep = new Set(sorted.slice(0, topK).map(([k]) => k));
      cols.push({ keep });
    }
    const name = (i, p) => (p === undefined ? null : cols[i].keep.has(p) ? p : 'other');
    const nodes = cols.map(() => new Map());
    const links = new Map();
    for (const { seq } of paths) {
      for (let i = 0; i < steps; i++) {
        const a = name(i, seq[i]);
        if (a === null) break;
        nodes[i].set(a, (nodes[i].get(a) || 0) + 1);
        const b = i + 1 < steps ? name(i + 1, seq[i + 1]) : null;
        if (b !== null) { const k = `${i}|${a}|${b}`; links.set(k, (links.get(k) || 0) + 1); }
      }
    }
    return {
      columns: nodes.map((m) => [...m.entries()].sort((a, b) => (a[0] === 'other') - (b[0] === 'other') || b[1] - a[1])),
      links: [...links.entries()].map(([k, v]) => { const [i, a, b] = k.split('|'); return { step: +i, from: a, to: b, value: v }; }),
    };
  }

  /** Heatmap: aggregate clicks per element. */
  function clickSummary(clicks) {
    const m = new Map();
    for (const c of clicks) {
      const k = c.event_data_selector;
      if (!m.has(k)) m.set(k, { selector: k, label: c.event_data_label, count: 0, sessions: new Set() });
      const o = m.get(k); o.count++; o.sessions.add(c.session_id);
    }
    return [...m.values()].sort((a, b) => b.count - a.count);
  }

  function median(a) {
    if (!a.length) return null;
    const s = [...a].sort((x, y) => x - y);
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  // ------------------------------------------------------------------ UI ---
  const RANGES = [['1h', 3600e3], ['6h', 6 * 3600e3], ['24h', 86400e3], ['7d', 7 * 86400e3], ['14d', 14 * 86400e3]];
  const NAV = [['index.html', 'Overview'], ['replay.html', 'Session replay'], ['funnel.html', 'Funnels & drop-off'],
    ['retention.html', 'Retention'], ['paths.html', 'User paths'], ['heatmap.html', 'Heatmaps']];

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v !== false && v !== null && v !== undefined) el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) el.append(c.nodeType ? c : document.createTextNode(String(c)));
    return el;
  }

  /** Renders the header (nav + range picker). Calls onChange(rangeMs) on change. */
  function header(current, defaultRange, onChange, extra) {
    const sel = h('select', { id: 'range' }, RANGES.map(([l, ms]) => h('option', { value: ms, selected: l === (params.get('range') || defaultRange) }, 'Last ' + l)));
    sel.onchange = () => { const p = new URLSearchParams(location.search); p.set('range', RANGES.find(([, ms]) => String(ms) === sel.value)[0]); history.replaceState(null, '', '?' + p); onChange(+sel.value); };
    const bar = h('header', { class: 'bar' },
      h('a', { class: 'logo', href: 'index.html' }, '◎ Insights'),
      h('nav', null, NAV.map(([href, label]) => h('a', { href, class: href === current ? 'on' : '' }, label))),
      h('div', { class: 'controls' }, extra || null, sel,
        h('a', { class: 'ext', href: 'http://localhost:3000', target: '_blank' }, 'Grafana ↗'),
        h('a', { class: 'ext', href: '/', target: '_blank' }, 'Demo shop ↗')));
    document.body.prepend(bar);
    return () => +sel.value;
  }

  function status(msg, kind) {
    let el = document.getElementById('status');
    if (!el) { el = h('div', { id: 'status' }); document.querySelector('main').prepend(el); }
    el.className = kind || '';
    el.textContent = msg || '';
    el.style.display = msg ? 'block' : 'none';
  }

  const pct = (x, d = 1) => (x * 100).toFixed(d) + '%';
  const num = (x) => Number(x).toLocaleString();
  const dur = (ms) => (ms === null || ms === undefined ? '–' : ms < 60e3 ? Math.round(ms / 1e3) + 's' : ms < 3600e3 ? (ms / 60e3).toFixed(1) + 'm' : (ms / 3600e3).toFixed(1) + 'h');
  const when = (ms) => new Date(ms).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const STEP_LABELS = { page_view: 'Visited', product_viewed: 'Viewed product', add_to_cart: 'Added to cart', checkout_started: 'Started checkout', purchase: 'Purchased', signup: 'Signed up', feature_used: 'Used a feature' };

  const api = { CFG, EV, instant, range, logs, parseLogfmt, tsv, fmtLine, fetchEvents, bySession, computeFunnel, exitPages, computeRetention, sessionPaths, topSequences, transitions, flow, clickSummary, median, h, header, status, pct, num, dur, when, STEP_LABELS, RANGES };
  root.Insights = api;
  if (typeof module !== 'undefined') module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
