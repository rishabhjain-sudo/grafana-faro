# Query cookbook: product analytics with LogQL

Run these in **Grafana → Explore** (data source *Loki*) or copy them into new dashboard panels. Every Faro event is a logfmt line in Loki, so you query it with LogQL.

## How the data looks

Each `faro.api.pushEvent('add_to_cart', {product: 'mug', price: '12'})` becomes:

```
kind=event event_name=add_to_cart event_domain=shop event_data_product=mug event_data_price=12
app_name=demo-shop user_id=user-42 session_id=7YXVuWEy page_url=http://localhost:8080/#/product/mug
browser_name=Chrome browser_os="Mac OS 15" browser_mobile=false view_name=product ...
```

| Stream labels (fast filters) | Fields in the line (use `\| logfmt`) |
|---|---|
| `source="faro"`, `app_name`, `app_env`, `kind` = `event` / `measurement` / `exception` / `log` | `event_name`, `event_data_<attr>`, `user_id`, `user_attr_<attr>`, `session_id`, `session_attr_<attr>`, `page_url`, `view_name`, `browser_name`, `browser_os`, `browser_mobile`, web vitals as `value_lcp`, `value_inp`, `value_cls`, … |

> Use plain `| logfmt`. In our tests, the field-list form `| logfmt a, b` didn't reliably extract the second field.
> When a field becomes a grouping label (e.g. `session_id`), aggregate percentiles with `by (...)` so you don't compute them per session.

Shorthand below: `S = {source="faro", app_name="demo-shop", kind="event"}`

## Users and engagement

```logql
# Unique users in the selected range (use as an instant query)
count(sum by (user_id) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt [$__range])))

# Daily active users over time (set query to range, interval 1d)
count(sum by (user_id) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt [1d])))

# Average events per session
sum(count_over_time({source="faro", app_name="demo-shop", kind="event"} [$__range]))
  / count(sum by (session_id) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt [$__range])))

# Returning users (users with more than 1 session)
count(count by (user_id) (sum by (user_id, session_id) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt [$__range]))) > 1)
```

## Funnels and conversion

```logql
# Unique sessions reaching a step (repeat per step)
count(sum by (session_id) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt | event_name="add_to_cart" [$__range])))

# Cart → purchase conversion %
count(sum by (session_id) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt | event_name="purchase" [$__range])))
  / count(sum by (session_id) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt | event_name="add_to_cart" [$__range]))) * 100

# Conversion by device (mobile vs desktop)
count by (browser_mobile) (sum by (browser_mobile, session_id) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt | event_name="purchase" [$__range])))
```

## Revenue and products

```logql
# Revenue over time
sum(sum_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt | event_name="purchase" | unwrap event_data_revenue | __error__="" [$__auto]))

# Revenue by acquisition campaign (no campaign = direct)
sum by (session_attr_campaign) (sum_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt | event_name="purchase"
  | label_format session_attr_campaign=`{{ or .session_attr_campaign "direct" }}` | unwrap event_data_revenue | __error__="" [$__range]))

# Product view → add-to-cart rate per product
sum by (event_data_product) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt | event_name="add_to_cart" [$__range]))
  / sum by (event_data_product) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt | event_name="product_viewed" [$__range]))
```

## Performance and quality

```logql
# p75 LCP per page (view)
quantile_over_time(0.75, {source="faro", app_name="demo-shop", kind="measurement"} | logfmt | type="web-vitals" | value_lcp!=""
  | unwrap value_lcp | __error__="" [$__range]) by (view_name)

# Error rate per 100 sessions
sum(count_over_time({source="faro", app_name="demo-shop", kind="exception"} [$__range]))
  / count(sum by (session_id) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt [$__range]))) * 100

# Rage / dead clicks by page (detected automatically by Faro)
sum by (event_name, view_name) (count_over_time({source="faro", app_name="demo-shop", kind="event"} | logfmt | event_name=~"faro.frustration.*" [$__range]))
```

## Debug a single user's journey

```logql
{source="faro", app_name="demo-shop"} | logfmt | user_id="user-6sxdbk67" | line_format "{{.kind}} {{.event_name}}{{.type}} {{.view_name}} {{.page_url}}"
```
