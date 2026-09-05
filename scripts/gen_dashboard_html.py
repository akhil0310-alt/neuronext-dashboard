# -*- coding: utf-8 -*-
"""Second generator pass: builds static HTML fragments (KPI strip, Monthwise
Profitability table, Other-costs breakdown, SKU x month matrix) from
dashboard_data.json, all numbers formatted per the global rule - aggregate
AED figures 0 decimals, % figures 1 decimal. Scratch script."""
import json

d = json.load(open('dashboard_data.json', encoding='utf-8'))
ytd = d['ytd']
monthly = d['monthly']
months = d['months']

SKU_SHORT_NAME = {
    'LN-WRP7-RIMK': 'BIRTH Single Breast Pump S39',
    'TJ-FDH7-PNAI': 'AEROFRY Air Fryer 6L',
    'PY-51U9-NDO7': 'BIRTH Double Breast Pump S39',
    '62-0VFC-64SJ': 'AEROFRY 9L Dual-Zone Air Fryer',
    'NR-O1LQ-E1UK': 'BIRTH Double Breast Pump S12A',
    'S39DBPDB': 'LITTLE MOMMY Double Breast Pump',
    '7J-5E02-AAAN': 'BIRTH Bluetooth Double Pump S39',
    '2D-JIOC-DYZ2': 'LITTLE MOMMY Single Breast Pump',
    'AEROSC-6': 'Aerofry 5.7L Smart Scale Fryer',
    'D4-2UK7-AM2Z': 'BIRTH Single Breast Pump S12A',
    'RH188SBPZ': 'Birth Manual Pump RH188',
    'RH218SBPZ': 'Birth Electric Pump RH218',
    'VG-HNBC-EKIS': 'Airfryer 10L',
}


def fmt0(v):
    """0-decimal, thousands separator, minus sign as unicode −."""
    if v is None:
        return "—"
    neg = v < 0
    s = f"{abs(v):,.0f}"
    return ("−" if neg else "") + s


def fmt1(v):
    if v is None:
        return "—"
    neg = v < 0
    s = f"{abs(v):,.1f}"
    return ("−" if neg else "") + s


def fmtpct(v):
    if v is None:
        return "—"
    neg = v < 0
    return ("−" if neg else "") + f"{abs(v):.1f}%"


def cls0(v):
    return ' class="neg"' if (v is not None and v < 0) else ''


month_label = {'01': 'Jan', '02': 'Feb', '03': 'Mar', '04': 'Apr', '05': 'May',
               '06': 'Jun', '07': 'Jul', '08': 'Aug', '09': 'Sep', '10': 'Oct',
               '11': 'Nov', '12': 'Dec'}


# ================= KPI STRIP =================
import datetime
gen_dt = datetime.datetime.fromisoformat(d['generated_at'].replace('Z', '+00:00'))
mtd = monthly[-1]
mtd_month_name = month_label[mtd['month'][5:7]]
refund_rate = abs(ytd['refunds']) / ytd['gross_revenue'] * 100 if ytd['gross_revenue'] else 0
net_proceeds_pct = ytd['net_proceeds'] / ytd['net_revenue'] * 100 if ytd['net_revenue'] else 0
net_profit_pct = ytd['net_profit'] / ytd['net_revenue'] * 100 if ytd['net_revenue'] else 0
ad_pct = abs(ytd['ad_spend']) / ytd['gross_revenue'] * 100 if ytd['gross_revenue'] else 0

kpi_html = f"""  <section class="kpis">
    <div class="kpi">
      <span class="label">MTD revenue</span>
      <span class="value">AED {fmt0(mtd['gross_revenue'])}</span>
      <span class="delta">{mtd_month_name} 1–{gen_dt.day} · {mtd['orders']} orders · {mtd['units']} units</span>
    </div>
    <div class="kpi accent-critical">
      <span class="label">Gross revenue</span>
      <span class="value">AED {fmt0(ytd['gross_revenue'])}</span>
      <span class="delta crit">{fmtpct(refund_rate)} refund rate</span>
    </div>
    <div class="kpi accent-good">
      <span class="label">Net proceeds</span>
      <span class="value">AED {fmt0(ytd['net_proceeds'])}</span>
      <span class="delta">{fmtpct(net_proceeds_pct)} of net revenue</span>
    </div>
    <div class="kpi{' accent-critical' if ytd['net_profit'] < 0 else ' accent-good'}">
      <span class="label">Net profit</span>
      <span class="value{' neg' if ytd['net_profit'] < 0 else ''}">{'−' if ytd['net_profit'] < 0 else ''}AED {fmt0(abs(ytd['net_profit']))}</span>
      <span class="delta{' crit' if ytd['net_profit'] < 0 else ''}">{fmtpct(net_profit_pct)} of net revenue</span>
    </div>
    <div class="kpi accent-amber">
      <span class="label">Ad spend</span>
      <span class="value">AED {fmt0(abs(ytd['ad_spend']))}</span>
      <span class="delta">{fmtpct(ad_pct)} of gross revenue</span>
    </div>
    <div class="kpi">
      <span class="label">Revenue per day</span>
      <span class="value">AED {fmt1(ytd['revenue_per_day'])}</span>
      <span class="delta">{ytd['units_per_day']:.2f} units / day · {ytd['days_elapsed']}d YTD</span>
    </div>
  </section>"""
open('gen_kpi_strip.html', 'w', encoding='utf-8').write(kpi_html + "\n")

header_html = (
    f'<span class="sub">1 Jan &ndash; {gen_dt.day} {mtd_month_name} {gen_dt.year} &middot; '
    f'Pulled directly from Amazon Selling Partner API</span>\n'
    f'<!--SNAPSHOT_DATE-->{gen_dt.day} {mtd_month_name} {gen_dt.year}'
)
open('gen_header.html', 'w', encoding='utf-8').write(header_html + "\n")

# Table header rows depend on how many months have accumulated YTD - regenerated every
# run so a month rollover (e.g. Aug -> Sep) never again requires a manual HTML edit.
month_ths = "".join(f"<th>{month_label[m[5:7]]}</th>" for m in months)

sku_header_html = (
    "          <tr>\n"
    "            <th>SKU</th>\n"
    f"            {month_ths}\n"
    '            <th class="ytd">YTD Total</th>\n'
    "          </tr>"
)
open('gen_sku_header.html', 'w', encoding='utf-8').write(sku_header_html + "\n")

monthwise_header_html = (
    "          <tr>\n"
    "            <th>Line item</th>\n"
    '            <th class="ytd">YTD Total</th>\n'
    f"            {month_ths}\n"
    "          </tr>"
)
open('gen_monthwise_header.html', 'w', encoding='utf-8').write(monthwise_header_html + "\n")

# ================= MONTHWISE PROFITABILITY (was Fee & cost breakdown) =================
def row(label, key, months, ytd_val, is_total=False, cls_extra=""):
    tds = [f"<td>{label}</td>", f'<td class="ytd{" neg" if ytd_val < 0 else ""}">{fmt0(ytd_val)}</td>']
    for m in months:
        v = next((mm[key] for mm in monthly if mm['month'] == m), 0.0)
        tds.append(f'<td{cls0(v)}>{fmt0(v)}</td>')
    tr_cls = f' class="{cls_extra}"' if cls_extra else ''
    return f"          <tr{tr_cls}>{''.join(tds)}</tr>"


def pct_row(label, numerator_key, denom_key='gross_revenue'):
    ytd_num = ytd[numerator_key]
    ytd_den = ytd[denom_key]
    ytd_pct = (ytd_num / ytd_den * 100) if ytd_den else None
    tds = [f"<td>{label}</td>", f'<td class="ytd">{fmtpct(ytd_pct)}</td>']
    for m in months:
        mm = next((x for x in monthly if x['month'] == m), None)
        v = (mm[numerator_key] / mm[denom_key] * 100) if mm and mm[denom_key] else None
        tds.append(f"<td>{fmtpct(v)}</td>")
    return f'          <tr class="pct-row">{"".join(tds)}</tr>'


rows = []
rows.append(row("Gross revenue", "gross_revenue", months, ytd['gross_revenue']))
rows.append(row("Refunds - sellable returns", "refunds_sellable", months, ytd['refunds_sellable']))
rows.append(row("Refunds - non-sellable returns", "refunds_non_sellable", months, ytd['refunds_non_sellable']))
rows.append(row("Net revenue", "net_revenue", months, ytd['net_revenue'], cls_extra="total"))
rows.append(pct_row("Net revenue % (of gross)", "net_revenue"))

cogs_tds = [f"<td>COGS</td>", f'<td class="ytd neg">{fmt0(-abs(ytd["cogs"]))}</td>']
for m in months:
    mm = next((x for x in monthly if x['month'] == m), None)
    v = -abs(mm['cogs']) if mm else 0.0
    cogs_tds.append(f'<td class="neg">{fmt0(v)}</td>')
rows.append(f"          <tr>{''.join(cogs_tds)}</tr>")

rows.append(row("Gross margin", "gross_margin", months, ytd['gross_margin'], cls_extra="total"))
rows.append(pct_row("Gross margin % (of gross)", "gross_margin"))
rows.append(row("Other costs (fees, storage, adj.)", "other_costs", months, ytd['other_costs']))
rows.append(row("Profit before advertising", "profit_before_ads", months, ytd['profit_before_ads'], cls_extra="total"))
rows.append(pct_row("Profit before ads % (of gross)", "profit_before_ads"))
rows.append(row("Advertising spend", "ad_spend", months, ytd['ad_spend']))
rows.append(row("Net profit", "net_profit", months, ytd['net_profit'], cls_extra="total final"))
rows.append(pct_row("Net profit % (of gross)", "net_profit"))

mwp_html = "\n".join(rows)
open('gen_monthwise_profitability.html', 'w', encoding='utf-8').write(mwp_html + "\n")

# ================= OTHER COSTS BREAKDOWN =================
cb = d['cost_breakdown_ytd']
gross = ytd['gross_revenue']


def cb_row(label, val):
    pct = val / gross * 100 if gross else None
    cls = ' class="neg"' if val < 0 else ' class="pos"'
    return f"          <tr><td>{label}</td><td{cls}>{fmt0(val)}</td><td>{fmtpct(pct)}</td></tr>"


other_rows = [
    cb_row("Referral commission", cb['commission']),
    cb_row("FBA fulfillment fees", cb['fulfillment']),
    cb_row("Shipping &amp; COD chargebacks", cb['chargebacks']),
    cb_row("Shipping / payment charges", cb['shipcharges']),
    cb_row("Refund fee credits", cb['refund_credits']),
    cb_row("FBA storage &amp; inbound (prorated evenly across months — Amazon doesn't date-stamp this per shipment)", cb['storage']),
    cb_row("Reimbursements / adjustments", cb['adjustments']),
]
total_other = sum(cb.values())
total_pct = total_other / gross * 100 if gross else None
other_rows.append(
    f'          <tr><td><b>Total other costs</b></td><td class="neg"><b>{fmt0(total_other)}</b></td><td><b>{fmtpct(total_pct)}</b></td></tr>'
)
open('gen_other_costs.html', 'w', encoding='utf-8').write("\n".join(other_rows) + "\n")

# ================= SKU x MONTH REVENUE MATRIX =================
sku_rows_sorted = d['sku_rows']  # already sorted by -gross_revenue
matrix_rows = []
for r in sku_rows_sorted:
    name = SKU_SHORT_NAME.get(r['sku'], r['sku'])
    cells = [f'<td><div class="sku-name-cell"><span class="sku-name">{name}</span><span class="sku-code">{r["sku"]}</span></div></td>']
    prev_rev = None
    for m in months:
        mv = r['months'].get(m)
        rev = mv['revenue'] if mv else None
        units = mv['units'] if mv else None
        if rev is None:
            mom_cls = ""
            cell = f'<td class="cell-dash">–</td>' if prev_rev is None else f'<td class="mom-down cell-dash">–</td>'
        else:
            if prev_rev is None:
                mom_cls = ""
            elif rev > prev_rev:
                mom_cls = "mom-up "
            elif rev < prev_rev:
                mom_cls = "mom-down "
            else:
                mom_cls = ""
            cell = f'<td class="{mom_cls.strip()}"><span class="cell-rev">{fmt0(rev)}</span><span class="cell-units">{units}u</span></td>'
        cells.append(cell)
        if rev is not None:
            prev_rev = rev
    ytd_rev = r['gross_revenue']
    ytd_units = r['units']
    cells.append(f'<td class="ytd"><span class="cell-rev">{fmt0(ytd_rev)}</span><span class="cell-units">{ytd_units}u</span></td>')
    matrix_rows.append(f"          <tr>{''.join(cells)}</tr>")

open('gen_sku_matrix.html', 'w', encoding='utf-8').write("\n".join(matrix_rows) + "\n")

print("HTML fragments generated:")
for fn in ["gen_kpi_strip.html", "gen_monthwise_profitability.html", "gen_other_costs.html",
           "gen_sku_matrix.html", "gen_header.html", "gen_sku_header.html", "gen_monthwise_header.html"]:
    print(" -", fn)
