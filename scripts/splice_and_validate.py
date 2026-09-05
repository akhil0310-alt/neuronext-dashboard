# -*- coding: utf-8 -*-
"""Splices every generated fragment into neuronext_amazon_dashboard.html and validates
the result structurally, exiting non-zero on any failure. Designed to run unattended
(GitHub Actions or the local scheduled task) with no human watching, so every check here
is a hard gate - a bad run must fail loudly, never publish silently.

Lessons baked in from real incidents this file's earlier ad-hoc versions hit:
- Never let a replacement's regex match span consume text you need to keep (e.g. the
  <table>/<thead> wrapper before a <tbody>) - a plain pattern.sub(regex, replacement)
  discards everything the pattern matched, including any prefix used only to anchor the
  search. Always slice the original string around a captured inner group instead.
- A fragment file that bundles more than one `const X = ...;` statement (gen_monthly.txt
  bundles monthly/ytdBar/monthKeys/monthLabels) must be split and each statement matched
  and replaced individually - substituting the whole fragment in place of just the first
  const silently duplicates the other three.
"""
import os
import re
import sys

# Local runs (SKILL.md's scheduled task) publish under dashboard/; the GitHub repo
# serves the same file as index.html at its root instead - CI sets this env var.
HTML_PATH = os.environ.get("DASHBOARD_HTML_PATH", "dashboard/neuronext_amazon_dashboard.html")
SCRIPTS_DIR = "scripts"


def read(name):
    with open(f"{SCRIPTS_DIR}/{name}", encoding="utf-8") as f:
        return f.read()


def sub_whole(html, pattern_str, replacement, label):
    """Replace an entire regex match with `replacement` (safe only when nothing before
    the match needs to be preserved - e.g. a standalone `const X = [...];` statement)."""
    pattern = re.compile(pattern_str, re.DOTALL)
    matches = list(pattern.finditer(html))
    if len(matches) != 1:
        raise RuntimeError(f"{label}: expected exactly 1 match, found {len(matches)}")
    return pattern.sub(lambda m: replacement, html, count=1), True


def sub_inner(html, anchor_regex, frag, label):
    """Replace only the CONTENT of a <tbody>...</tbody> or <thead>...</thead> block that
    follows `anchor_regex`, preserving the anchor prefix and the tag pair itself verbatim
    via string slicing (never regex substitution) - the fix for the real bug this caused
    once already."""
    tag = "tbody" if "tbody" in label else "thead"
    pattern = re.compile(anchor_regex + rf"(<{tag}[^>]*>)(.*?)(</{tag}>)", re.DOTALL)
    matches = list(pattern.finditer(html))
    if len(matches) != 1:
        raise RuntimeError(f"{label}: expected exactly 1 match, found {len(matches)}")
    m = matches[0]
    prefix_end = m.start(1)
    new_region = m.group(1) + "\n" + frag.strip() + "\n" + m.group(3)
    return html[:prefix_end] + new_region + html[m.end():], True


def main():
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    # ---- header (date range + snapshot date) ----
    header_frag = read("gen_header.html")
    sub_span = re.search(r'<span class="sub">(.*?)</span>', header_frag).group(1)
    snapshot = header_frag.split("<!--SNAPSHOT_DATE-->", 1)[1].strip()
    html, _ = sub_whole(html, r'<span class="sub">.*?</span>', f'<span class="sub">{sub_span}</span>', "header_sub")
    html, _ = sub_whole(html, r"snapshot taken <b>[^<]*</b>", f"snapshot taken <b>{snapshot}</b>", "snapshot_date")

    # ---- KPI strip (fragment already includes the <section class="kpis"> wrapper) ----
    html, _ = sub_whole(html, r'<section class="kpis">.*?</section>', read("gen_kpi_strip.html").strip(), "kpi_strip")

    # ---- SKU matrix table: header row + body rows, table-open anchor keeps them scoped ----
    html, _ = sub_inner(html, r'<table class="sku-table">.*?', read("gen_sku_header.html"), "sku_matrix_thead")
    html, _ = sub_inner(html, r'<table class="sku-table">.*?', read("gen_sku_matrix.html"), "sku_matrix_tbody")

    # ---- Monthwise Profitability table: header row + body rows ----
    html, _ = sub_inner(html, r'<table class="fee-table fee-breakdown-table">.*?', read("gen_monthwise_header.html"), "monthwise_thead")
    html, _ = sub_inner(html, r'<table class="fee-table fee-breakdown-table">.*?', read("gen_monthwise_profitability.html"), "monthwise_tbody")

    # ---- Other costs breakdown (data rows only, header never changes) ----
    html, _ = sub_inner(html, r'"Other costs" breakdown.*?', read("gen_other_costs.html"), "other_costs_tbody")

    # ---- Standalone JS consts (each fragment is exactly one const, safe to fully swap) ----
    for const_name, frag_file in [("skuData", "gen_skudata.txt"), ("costTable", "gen_costtable.txt"),
                                   ("invData", "gen_inventory.txt"), ("returnsData", "gen_returns.txt")]:
        html, _ = sub_whole(html, r"const " + const_name + r" = \[.*?\];", read(frag_file).strip(), const_name)

    # ---- gen_monthly.txt bundles 4 consts - split and replace each individually ----
    monthly_frag = read("gen_monthly.txt")
    for const_name, kind in [("monthly", "["), ("ytdBar", "{"), ("monthKeys", "["), ("monthLabels", "{")]:
        close = "]" if kind == "[" else "}"
        pattern = "const " + const_name + " = " + re.escape(kind) + ".*?" + re.escape(close) + ";"
        stmt = re.search("(" + pattern + ")", monthly_frag, re.DOTALL).group(1)
        html, _ = sub_whole(html, pattern, stmt, const_name)

    # ---- waterfall (wfMax + steps as one unit) ----
    html, _ = sub_whole(html, r"const wfMax = [\d.]+;\s*\nconst steps = \[.*?\];", read("gen_waterfall.txt").strip(), "waterfall")

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print("Spliced all regions successfully.")

    validate(html)


def validate(html):
    """Hard gate: any failure here must stop the pipeline before commit/push."""
    errors = []

    for op, cl, name in [("{", "}", "brace"), ("(", ")", "paren"), ("[", "]", "bracket")]:
        if html.count(op) != html.count(cl):
            errors.append(f"{name} mismatch: {html.count(op)} vs {html.count(cl)}")

    for tag in ["tbody", "table", "section", "thead"]:
        o, c = html.count(f"<{tag}"), html.count(f"</{tag}>")
        if o != c:
            errors.append(f"<{tag}> mismatch: {o} open vs {c} close")

    for const in ["monthly", "ytdBar", "skuData", "costTable", "invData", "returnsData", "monthKeys", "monthLabels"]:
        n = len(re.findall(r"const " + const + r" =", html))
        if n != 1:
            errors.append(f"const {const}: expected 1 declaration, found {n}")

    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    print("Validation passed: all structural checks OK.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"SPLICE FAILED: {e}")
        sys.exit(1)
