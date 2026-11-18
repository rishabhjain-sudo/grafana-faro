// Runs the Insights analysis code (insights/common.js) against a real Loki and
// prints what each page would show. Usage: node tools/test_insights.mjs [LOKI_URL]
import { createRequire } from 'module';
globalThis.INSIGHTS_LOKI_URL = process.argv[2] || 'http://localhost:3100';
const I = createRequire(import.meta.url)('../insights/common.js');
const now = Date.now(), DAY = 86400e3, start = now - 14 * DAY;
let fails = 0; const check = (ok, msg) => { console.log((ok ? 'OK   ' : 'FAIL ') + msg); if (!ok) fails++; };

const { events, truncated } = await I.fetchEvents(start, now);
const sessions = I.bySession(events);
check(events.length > 0 && !truncated, `events: ${events.length} rows, ${sessions.size} sessions (truncated=${truncated})`);

const STEPS = ['page_view', 'product_viewed', 'add_to_cart', 'checkout_started', 'purchase'];
const [f] = I.computeFunnel(sessions, STEPS);
check(f.steps.every((s, i) => i === 0 || s.count <= f.steps[i - 1].count), 'funnel (strict order): ' + f.steps.map((s) => `${s.name}=${s.count} (${I.pct(s.fromPrev, 0)}, median ${I.dur(s.medianMs)})`).join(' → '));
const byDevice = I.computeFunnel(sessions, STEPS, (s) => (s.mobile ? 'mobile' : 'desktop'));
check(byDevice.length === 2, 'by device: ' + byDevice.map((g) => `${g.segment} ${I.pct(g.steps[4].fromStart)}`).join(', '));
const stuckCart = f.steps[2].stuckSessions;
check(stuckCart.length === f.steps[2].count - f.steps[3].count, `abandoned after add_to_cart: ${stuckCart.length}; exit pages: ` + I.exitPages(stuckCart).slice(0, 4).map(([p, n]) => `${p}=${n}`).join(' '));

const series = await I.range(`sum by (user_id) (count_over_time(${I.EV()} | logfmt | event_name="page_view" [1d]))`,
  Math.floor(start / DAY) * DAY + DAY, Math.floor(now / DAY) * DAY + DAY, 86400);
const ret = I.computeRetention(series, DAY, now);
const d1 = ret.cohorts.filter((c) => c.cells[1] && !c.cells[1].partial && c.size > 0);
const avgD1 = d1.reduce((a, c) => a + c.cells[1].pct, 0) / (d1.length || 1);
check(ret.cohorts.length >= 13 && avgD1 > 0.05 && avgD1 < 0.6, `retention: ${series.length} users, ${ret.cohorts.length} daily cohorts, avg day-1 retention ${I.pct(avgD1)}; first cohort: size ${ret.cohorts[0].size}, ` + ret.cohorts[0].cells.slice(0, 8).map((c) => I.pct(c.pct, 0)).join(' '));

const paths = I.sessionPaths(sessions);
const top = I.topSequences(paths, 5);
check(top.length > 0, 'top paths: ' + top.map(([p, n]) => `[${n}] ${p}`).join(' | '));
const tr = I.transitions(paths, 'product');
check(tr.total > 0, `after "product": ` + tr.next.slice(0, 4).map(([p, n]) => `${p} ${I.pct(n / tr.total, 0)}`).join(', '));
const fl = I.flow(paths, 5, 5);
check(fl.columns.length === 5 && fl.links.length > 0, `flow: ${fl.columns.map((c) => c.length).join('/')} nodes per step, ${fl.links.length} links`);

const clickRows = I.tsv((await I.logs(`${I.EV()} | logfmt | event_name="ui_click" | event_data_page="product" ${I.fmtLine(['session_id', 'event_data_selector', 'event_data_label', 'event_data_rel_x', 'event_data_rel_y'])}`, now - DAY, now)).rows,
  ['session_id', 'event_data_selector', 'event_data_label', 'event_data_rel_x', 'event_data_rel_y']);
const cs = I.clickSummary(clickRows);
check(cs.length > 0, `heatmap clicks on product pages (24h): ${clickRows.length}; top: ` + cs.slice(0, 4).map((c) => `${c.label}=${c.count}`).join(', '));

const parsed = I.parseLogfmt('kind=event a="x y \\"q\\"" b=2 empty= c="line\\nnext"');
check(parsed.a === 'x y "q"' && parsed.b === '2' && parsed.empty === '' && parsed.c === 'line\nnext', 'logfmt parser');
console.log(fails ? `\n${fails} FAILED` : '\nALL INSIGHTS CHECKS PASSED');
process.exit(fails ? 1 : 0);
