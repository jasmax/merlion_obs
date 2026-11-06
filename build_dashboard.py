"""Render out/dashboard.html from out/dashboard_data.json (run after run_demo.py)."""
import json

data = json.load(open("out/dashboard_data.json"))
tpl = open("dashboard/template.html", encoding="utf-8").read()
html = tpl.replace("/*DATA*/null", json.dumps(data, default=str).replace("</", "<\\/"))
open("out/dashboard.html", "w", encoding="utf-8").write(html)
print(f"out/dashboard.html  ({len(html) / 1024:.0f} KB)")
