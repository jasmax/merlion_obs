"""Alert rules over daily rollups. Wire `evaluate()` output to Slack/PagerDuty/email."""

RULES = [
    # name, metric, op, threshold, severity, why
    ("critical_checks", "critical_fail_rate", ">", 0.05, "page",
     "More than 5% of complaints had a possibly wrong customer-facing decision"),
    ("timezone_misalignment", ("check_fail", "timezone_alignment"), ">=", 1, "page",
     "GPS evidence read in the wrong timezone; 'not received' claims may be wrongly upheld"),
    ("order_collision_unresolved", ("check_fail", "order_disambiguation"), ">=", 1, "page",
     "A v1/v2 order-number collision was resolved without evidence"),
    ("tool_errors", "tool_error_rate", ">", 0.05, "ticket", "A tool is failing or timing out"),
    ("latency_p95", "p95_latency_s", ">", 12.0, "ticket", "Triage p95 latency over budget"),
    ("escalation_spike", "escalation_rate", ">", 0.15, "ticket", "Agent is handing back too much to humans"),
    ("override_rate", "override_rate", ">", 0.12, "ticket", "CS team overriding the agent too often"),
    ("daily_cost", "cost_usd", ">", 1.00, "ticket", "Daily LLM spend over budget"),
]


def _get(day, metric):
    if isinstance(metric, tuple):
        return day[metric[0]].get(metric[1], 0)
    return day[metric]


def evaluate(daily_rows):
    fired = []
    for d in daily_rows:
        for name, metric, op, th, sev, why in RULES:
            v = _get(d, metric)
            if (v > th) if op == ">" else (v >= th):
                fired.append({"day": d["day"], "rule": name, "value": v, "threshold": th,
                              "severity": sev, "why": why, "version": d["version"]})
    return fired
