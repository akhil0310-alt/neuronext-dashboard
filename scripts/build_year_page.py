# -*- coding: utf-8 -*-
"""Builds the static dashboard page for a CLOSED past year (e.g. dashboard/2025.html),
reached from the live page's Year dropdown. Added 2026-10-02.

Run from the NeuroNext_Integration ROOT, after pulling that year's data:
    python scripts/dashboard_refresh_data.py --year 2025   (writes scripts/dashboard_data_2025.json)
    python scripts/build_year_page.py 2025                 (writes dashboard/2025.html)

Uses the live page as the template and the exact same generators + splice/validate as
the daily refresh, just pointed at that year's data and its own fragment directory - so
the year page can never drift from the live page's methodology or layout. A closed year
doesn't change, so this only needs re-running if the dashboard layout/logic changes.
"""
import os
import shutil
import subprocess
import sys

TEMPLATE = "dashboard/neuronext_amazon_dashboard.html"


def run(cmd, cwd=None, extra_env=None):
    env = dict(os.environ, **(extra_env or {}))
    r = subprocess.run([sys.executable] + cmd, cwd=cwd, env=env)
    if r.returncode != 0:
        sys.exit(f"FAILED: {' '.join(cmd)}")


def main():
    year = sys.argv[1]
    data = f"dashboard_data_{year}.json"
    frag_dir = f"gen_{year}"
    page = f"dashboard/{year}.html"
    if not os.path.exists(os.path.join("scripts", data)):
        sys.exit(f"scripts/{data} missing - run dashboard_refresh_data.py --year {year} first")
    os.makedirs(os.path.join("scripts", frag_dir), exist_ok=True)

    gen_env = {"NN_DATA": data, "NN_OUT_DIR": frag_dir}
    run(["gen_dashboard_pieces.py"], cwd="scripts", extra_env=gen_env)
    run(["gen_dashboard_html.py"], cwd="scripts", extra_env=gen_env)

    shutil.copyfile(TEMPLATE, page)
    with open(page, encoding="utf-8") as f:
        html = f.read()
    live_year = html.split('const monthKeys = ["', 1)[1][:4]
    # static section-note wording that names the live year
    html = html.replace(f"YTD {live_year}", f"FY {year}")
    html = html.replace(f"any {live_year} return", f"any {year} return")
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)

    run(["scripts/splice_and_validate.py"],
        extra_env={"DASHBOARD_HTML_PATH": page, "NN_FRAG_DIR": f"scripts/{frag_dir}"})
    print(f"Built {page}")


if __name__ == "__main__":
    main()
