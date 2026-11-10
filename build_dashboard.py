"""Build dashboard.html: one self-contained page from your traces, alerts and eval results.

    python build_dashboard.py                                # from traces.db
    TRACES_DB=demo_traces.db python build_dashboard.py       # from the practice week

No libraries and no internet: charts are plain SVG written by this script. Before saving, the page is
scanned for personal data and the script refuses to write it if any is found.
"""
import datetime as dt
import html
import json
import os
import sys
from collections import Counter, defaultdict

from alerts import evaluate
from metrics import SGT, by_day, load_requests, summarize
from redact import PATTERNS, _card

BUDGET = float(os.environ.get("BUDGET_USD", "20.00"))
esc = html.escape


def bar_chart(labels, values, highlight=None, width=640, height=180, fmt=str):
    """Vertical bars with a value on each bar. Bars in `highlight` (indexes) are drawn in the alert colour."""
    top = max(values) or 1
    left, bottom, gap = 8, 26, 10
    bw = (width - left * 2) / len(values) - gap
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="bar chart">']
    for i, (label, v) in enumerate(zip(labels, values)):
        h = (height - bottom - 18) * v / top
        x = left + i * (bw + gap)
        y = height - bottom - h
        cls = "bar alert" if highlight and i in highlight else "bar"
        parts.append(f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="3">'
                     f'<title>{esc(label)}: {esc(fmt(v))}</title></rect>')
        parts.append(f'<text class="val" x="{x + bw / 2:.1f}" y="{y - 4:.1f}">{esc(fmt(v))}</text>')
        parts.append(f'<text class="lbl" x="{x + bw / 2:.1f}" y="{height - 8}">{esc(label)}</text>')
    return "".join(parts) + "</svg>"


def hbars(rows, unit=""):
    """Horizontal bars for a few labelled counts."""
    if not rows:
        return '<p class="muted">None.</p>'
    top = max(v for _, v in rows) or 1
    return "".join(f'<div class="hb"><span>{esc(k)}</span><div><i style="width:{100 * v / top:.0f}%"></i></div>'
                   f'<b>{esc(str(v))}{unit}</b></div>' for k, v in rows)


def pii_in(text):
    hits = []
    for kind, pattern, _ in PATTERNS:
        for m in pattern.finditer(text):
            if kind != "card" or _card(m) != m.group():
                hits.append(f"{kind}: {m.group()}")
    return hits


class DashboardError(Exception):
    pass


def render():
    """Return (page_html, alerts_firing). Raises DashboardError if there are no traces or the page holds PII."""
    reqs = load_requests()
    if not reqs:
        raise DashboardError("No traces found. Run the agent (or python simulate_traffic.py) first.")
    now = dt.datetime.now(SGT)
    daily = by_day(reqs)
    days = list(daily)[-7:]
    week = summarize([r for r in reqs if r["start"] >= reqs[-1]["start"] - 7 * 86400])
    alerts, last_hour = evaluate(reqs, 60)
    firing = [(n, sev, msg) for n, (f, sev, msg) in alerts.items() if f]

    # model spend from the traces (same as models.py)
    by_model = defaultdict(float)
    import sqlite3
    for (attrs,) in sqlite3.connect(os.environ.get("TRACES_DB", "traces.db")).execute("SELECT attrs FROM spans"):
        a = json.loads(attrs)
        if "llm.model" in a:
            by_model[a["llm.model"]] += a.get("llm.cost_usd") or 0

    evals = json.load(open("evals/results/latest.json")) if os.path.exists("evals/results/latest.json") else None

    # ---------- page ----------
    kpi = [
        ("Complaints, last 7 days", f"{week['requests']}", f"{week['requests'] / 7:.0f} a day"),
        ("Refused by a guard", f"{week['refused'] / week['requests']:.0%}", f"{week['refused']} complaints, no model call"),
        ("Handed to a person", f"{week['escalated_rate']:.0%}", "escalate or ask the customer"),
        ("Couldn't be checked", f"{week['degraded_rate']:.0%}", "a tool or the model failed"),
        ("Slowest 5%", f"{week['p95_ms'] / 1000:.1f} s", "p95 time to answer"),
        ("Model spend", f"US${week['cost_usd']:.4f}", f"{week['cost_usd'] / BUDGET:.2%} of US${BUDGET:.0f} budget"),
    ]
    alert_html = ("".join(f'<li class="{sev}"><b>{esc(sev.upper())}</b> {esc(n.replace("_", " "))}: {esc(msg)}</li>'
                          for n, sev, msg in firing)
                  if firing else '<li class="ok"><b>OK</b> No alerts firing in the last hour.</li>')
    worst = {i for i, d in enumerate(days) if daily[d]["degraded_rate"] > 0.05 or daily[d]["escalated_rate"] > 0.30}
    day_labels = [dt.date.fromisoformat(d).strftime("%a %d") for d in days]

    eval_html = '<p class="muted">No eval results yet. Run <code>python run_evals.py</code>.</p>'
    if evals:
        rows = "".join(f'<tr><td>{esc(k.replace("_", " "))}</td><td class="r">'
                       f'{esc(str(round(v, 4) if isinstance(v, float) else v))}</td>'
                       f'<td><span class="pill {g}">{esc(g)}</span></td></tr>'
                       for k, g in evals["gates"].items() for v in [evals["metrics"][k]])
        bad = [r for r in evals["results"] if r["result"] in ("fail", "flaky")]
        fails = "".join(f'<li><code>{esc(r["id"])}</code> {esc(r["result"])}: {esc("; ".join(r["why"]))}</li>' for r in bad)
        tags = Counter(t for r in evals["results"] for t in r["tags"])
        eval_html = (f'<p><span class="pill {"pass" if evals["passed"] else "fail"}">'
                     f'{"PASS" if evals["passed"] else "FAIL"}</span> {esc(evals["when"])} · commit {esc(evals["commit"])} · '
                     f'{esc(evals["mode"])} · {len(evals["results"])} cases '
                     f'({", ".join(f"{v} {k}" for k, v in tags.most_common(5))})</p>'
                     f'<table><tr><th>Gate</th><th class="r">Value</th><th>Result</th></tr>{rows}</table>'
                     + (f'<h3>Failing cases</h3><ul>{fails}</ul>' if fails else "")
                     + (f'<p><b>Regressions:</b> {esc(", ".join(evals["regressions"]))}</p>' if evals["regressions"] else ""))

    recent = "".join(
        f'<tr><td>{dt.datetime.fromtimestamp(r["start"], SGT):%d %b %H:%M}</td>'
        f'<td><span class="pill {"fail" if r["refused_by"] else "warn" if r["degraded"] else "info" if r["resolution"] in ("escalate", "clarify") else "pass"}">'
        f'{esc(r["refused_by"] or r["resolution"] or "")}</span></td>'
        f'<td>{esc(r["degraded"] or "")}</td><td class="r">{r["model_calls"]}</td>'
        f'<td class="r">{r["latency_ms"]:.0f} ms</td><td class="r">{r["cost"]:.6f}</td>'
        f'<td><code>{esc(r["trace_id"])}</code></td></tr>' for r in reversed(reqs[-15:]))

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Merlion Triage Health</title>
<style>
:root {{ --bg:#f5f6f8; --card:#fff; --ink:#1b2330; --muted:#5d6878; --line:#dfe3ea;
  --bar:#2a78d6; --alert:#d03b3b; --ok:#0a7a0a; --warn:#a86b00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --card:#1d2128; --ink:#e8ebf0; --muted:#9aa4b2;
  --line:#2f3540; --bar:#4b93e8; --alert:#ef6b6b; --ok:#46c46b; --warn:#e3b04b; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif }}
main {{ max-width:1080px; margin:0 auto; padding:24px 16px 48px; display:grid; gap:18px }}
h1 {{ margin:0; font-size:24px }} h2 {{ margin:0 0 10px; font-size:17px }} h3 {{ font-size:15px; margin:14px 0 6px }}
.muted {{ color:var(--muted) }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; min-width:0 }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px }}
.kpi b {{ display:block; font-size:24px; font-variant-numeric:tabular-nums }}
.kpi span {{ font-size:13px; color:var(--muted) }}
.two {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr)); gap:18px }}
svg {{ width:100%; height:auto }}
.bar {{ fill:var(--bar) }} .bar.alert {{ fill:var(--alert) }}
.val, .lbl {{ fill:var(--muted); font-size:11px; text-anchor:middle; font-variant-numeric:tabular-nums }}
ul.alerts {{ list-style:none; padding:0; margin:0; display:grid; gap:8px }}
ul.alerts li {{ padding:8px 10px; border-radius:6px; border-left:4px solid var(--muted); background:var(--bg) }}
ul.alerts li.page {{ border-color:var(--alert) }} ul.alerts li.ticket {{ border-color:var(--warn) }} ul.alerts li.ok {{ border-color:var(--ok) }}
.hb {{ display:grid; grid-template-columns:150px 1fr 40px; gap:8px; align-items:center; font-size:13px; margin:4px 0 }}
.hb div {{ background:var(--bg); border-radius:3px; height:10px }} .hb i {{ display:block; height:10px; border-radius:3px; background:var(--bar) }}
.hb b {{ text-align:right; font-variant-numeric:tabular-nums }}
.scroll {{ overflow-x:auto }}
table {{ border-collapse:collapse; width:100%; font-size:13px }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap }}
th {{ color:var(--muted); font-weight:500 }} .r {{ text-align:right; font-variant-numeric:tabular-nums }}
code {{ font-size:12px }}
.pill {{ font-size:12px; padding:2px 7px; border-radius:10px; border:1px solid currentColor }}
.pill.pass {{ color:var(--ok) }} .pill.fail {{ color:var(--alert) }} .pill.warn, .pill.skipped {{ color:var(--warn) }} .pill.info {{ color:var(--muted) }}
</style></head><body><main>
<div><h1>Merlion triage health</h1>
<p class="muted">Built {now:%d %b %Y, %H:%M} SGT from {esc(os.environ.get("TRACES_DB", "traces.db"))} ·
{len(reqs)} complaints traced · personal data removed before storage</p></div>

<section><h2>Alerts in the last hour</h2><ul class="alerts">{alert_html}</ul></section>

<section class="kpis">{"".join(f'<div class="kpi"><span>{esc(a)}</span><b>{esc(b)}</b><span>{esc(c)}</span></div>' for a, b, c in kpi)}</section>

<div class="two">
<section><h2>Complaints per day</h2><p class="muted">Red: more than 5% couldn't be checked, or over 30% handed to a person.</p>
{bar_chart(day_labels, [daily[d]["requests"] for d in days], worst)}</section>
<section><h2>Handed to a person, per day</h2><p class="muted">Escalations and requests for more detail.</p>
{bar_chart(day_labels, [round(100 * daily[d]["escalated_rate"]) for d in days], worst, fmt=lambda v: f"{v}%")}</section>
</div>

<div class="two">
<section><h2>Why complaints were refused (7 days)</h2><p class="muted">Decided by code before any model call.</p>
{hbars(Counter(week["refused_by"]).most_common())}</section>
<section><h2>Model spend by model</h2><p class="muted">US$, all traced calls. {week["cost_usd"] / BUDGET:.2%} of the budget this week.</p>
{hbars([(m, round(c, 5)) for m, c in sorted(by_model.items(), key=lambda x: -x[1])])}</section>
</div>

<section><h2>Release gate: latest eval run</h2>{eval_html}</section>

<section><h2>Latest complaints</h2><p class="muted">Look up any trace with <code>python show_trace.py</code>.</p>
<div class="scroll"><table><tr><th>When (SGT)</th><th>Outcome</th><th>Degraded</th><th class="r">Model calls</th>
<th class="r">Time</th><th class="r">US$</th><th>Trace</th></tr>{recent}</table></div></section>
</main></body></html>"""

    leaks = pii_in(page)
    if leaks:
        raise DashboardError(f"Personal data found in the page: {leaks[:5]}")
    return page, len(firing)


def main():
    try:
        page, firing = render()
    except DashboardError as e:
        sys.exit(f"Not written: {e}")
    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"dashboard.html written ({len(page) / 1024:.0f} KB, {firing} alert(s) firing, PII check passed)")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()