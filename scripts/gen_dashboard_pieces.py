# -*- coding: utf-8 -*-
"""One-off generator: reads dashboard_data.json, writes JS/HTML fragments to
gen_*.txt files for splicing into neuronext_amazon_dashboard.html. Scratch
script, not part of the regular pipeline."""
import json

d = json.load(open('dashboard_data.json', encoding='utf-8'))


def jn(v, nd=None):
    if v is None:
        return 'null'
    if nd is not None:
        return repr(round(v, nd))
    return repr(v)


def js_str(s):
    return json.dumps(s)


month_label = {'01': 'Jan', '02': 'Feb', '03': 'Mar', '04': 'Apr', '05': 'May',
               '06': 'Jun', '07': 'Jul', '08': 'Aug', '09': 'Sep', '10': 'Oct',
               '11': 'Nov', '12': 'Dec'}

# ---------- monthly / ytdBar / monthKeys / monthLabels ----------
monthly = d['monthly']
lines = []
for m in monthly:
    mm = m['month'].split('-')[1]
    lines.append(
        f"    {{ m: \"{month_label[mm]}\", revenue: {jn(m['gross_revenue'],2)}, "
        f"profitBefore: {jn(m['profit_before_ads'],2)}, profit: {jn(m['net_profit'],2)}, "
        f"orders: {m['orders']}, units: {m['units']} }},"
    )
monthly_js = "const monthly = [\n" + "\n".join(lines) + "\n  ];"

ytd = d['ytd']
orders_total = sum(m['orders'] for m in monthly)  # unique orders WITH financial events, matches monthly rows
units_ytd = sum(m['units'] for m in monthly)
ytdbar_js = (
    f"const ytdBar = {{ m: \"YTD\", revenue: {jn(ytd['gross_revenue'],2)}, "
    f"profitBefore: {jn(ytd['profit_before_ads'],2)}, profit: {jn(ytd['net_profit'],2)}, "
    f"orders: {orders_total}, units: {units_ytd} }};"
)

monthkeys = d['months']
monthkeys_js = "const monthKeys = [" + ",".join(js_str(m) for m in monthkeys) + "];"
monthlabels_js = "const monthLabels = {" + ",".join(
    f"{js_str(m)}:{js_str(month_label[m.split('-')[1]] + ' ' + m.split('-')[0])}" for m in monthkeys
) + "};"

with open('gen_monthly.txt', 'w', encoding='utf-8') as f:
    f.write(monthly_js + "\n" + ytdbar_js + "\n\n" + monthkeys_js + "\n" + monthlabels_js + "\n")

# ---------- skuData ----------
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
    'RH228DBPZ': 'Birth Electric Pump RH228',
    'RH1008BSZ': 'Birth Bottle Sterilizer RH1008',
    'VG-HNBC-EKIS': 'Airfryer 10L',
}

# Canonical SKU order for every table on the dashboard, so a SKU sits in the same row
# position everywhere - primary: YTD gross-revenue descending (the order sku_rows/the
# "Revenue by SKU" matrix already uses); secondary (SKUs with no 2026 sales, so absent
# from sku_rows): landed-cost descending, appended after all the revenue-ranked ones.
CANONICAL_ORDER = [r['sku'] for r in d['sku_rows']]
_extra_skus = sorted(
    (r for r in d['cost_table'] if r['sku'] not in CANONICAL_ORDER),
    key=lambda r: -(r['landed_cost'] or 0),
)
CANONICAL_ORDER += [r['sku'] for r in _extra_skus]


def canonical_key(sku):
    return CANONICAL_ORDER.index(sku) if sku in CANONICAL_ORDER else len(CANONICAL_ORDER)


def month_obj(m):
    return (
        "{" +
        f"gross_revenue:{jn(m['revenue'],2)},refunds:{jn(m['refunds'],2)},net_revenue:{jn(m['net_revenue'],2)},"
        f"cogs:{jn(m['cogs'],2)},gross_margin:{jn(m['gross_margin'],2)},margin_pct:{jn(m['margin_pct'])},"
        f"amazon_fees:{jn(m['amazon_fees'],2)},ad_alloc:{jn(m['ad_spend_allocated'],2)},"
        f"net_profit:{jn(m['net_profit'],2)},net_margin_pct:{jn(m['net_margin_pct'])},asp:{jn(m['asp'],2)},units:{m['units']}"
        "}"
    )


sku_lines = []
for r in d['sku_rows']:
    name = SKU_SHORT_NAME.get(r['sku'], r['sku'])
    ytd_obj = (
        "{" +
        f"gross_revenue:{jn(r['gross_revenue'],2)},refunds:{jn(r['refunds'],2)},net_revenue:{jn(r['net_revenue'],2)},"
        f"cogs:{jn(r['cogs'],2)},cogs_est:{'true' if r['cogs_is_estimated'] else 'false'},"
        f"gross_margin:{jn(r['gross_margin'],2)},margin_pct:{jn(r['margin_pct'])},"
        f"amazon_fees:{jn(r['amazon_fees'],2)},ad_alloc:{jn(r['ad_spend_allocated'],2)},"
        f"net_profit:{jn(r['net_profit'],2)},net_margin_pct:{jn(r['net_margin_pct'])},"
        f"asp:{jn(r['asp'],2)},cpu:{jn(r['cost_per_unit'],2)},units:{r['units']}"
        "}"
    )
    months_obj = "{" + ",".join(f"{js_str(mk)}:{month_obj(mv)}" for mk, mv in sorted(r['months'].items())) + "}"
    sku_lines.append(f"    {{ sku:{js_str(r['sku'])}, name:{js_str(name)}, ytd:{ytd_obj}, months:{months_obj} }},")

skudata_js = "const skuData = [\n" + "\n".join(sku_lines) + "\n  ];"
with open('gen_skudata.txt', 'w', encoding='utf-8') as f:
    f.write(skudata_js + "\n")

# ---------- costTable ----------
cost_lines = []
ct_sorted = sorted(d['cost_table'], key=lambda r: canonical_key(r['sku']))
for r in ct_sorted:
    name = SKU_SHORT_NAME.get(r['sku'], r['sku'])
    cost_lines.append(
        f"    {{ sku:{js_str(r['sku'])}, name:{js_str(name)}, is_est:{'true' if r['is_estimated'] else 'false'}, "
        f"product_cost_usd:{jn(r['product_cost_usd'])}, freight_usd:{jn(r['freight_usd'])}, "
        f"duty_usd:{jn(r['duty_usd'])}, vat_usd:{jn(r['vat_usd'])}, "
        f"landed_cost_usd:{jn(r['landed_cost_usd'])}, landed_cost:{jn(r['landed_cost'])}, "
        f"rec_asp:{jn(r['recommended_asp_60pct_margin'])}, actual_asp:{jn(r['actual_asp'])} }},"
    )
costtable_js = "const costTable = [\n" + "\n".join(cost_lines) + "\n  ];"
with open('gen_costtable.txt', 'w', encoding='utf-8') as f:
    f.write(costtable_js + "\n")

# ---------- waterfall steps ----------
wf_max = ytd['gross_revenue']
steps_js = (
    "const steps = [\n"
    f"    {{ label: \"Gross revenue\", value: {jn(ytd['gross_revenue'],2)}, type: \"total\" }},\n"
    f"    {{ label: \"Refunds\", value: {jn(ytd['refunds'],2)}, type: \"drop\" }},\n"
    f"    {{ label: \"Fees, storage & adj.\", value: {jn(ytd['other_costs'],2)}, type: \"drop\" }},\n"
    f"    {{ label: \"Ad spend\", value: {jn(ytd['ad_spend'],2)}, type: \"drop\" }},\n"
    f"    {{ label: \"Net proceeds\", value: {jn(ytd['net_proceeds'],2)}, type: \"total final\" }},\n"
    "  ];"
)
with open('gen_waterfall.txt', 'w', encoding='utf-8') as f:
    f.write(f"const wfMax = {jn(wf_max,2)};\n" + steps_js + "\n")

# ---------- inventory ----------
# Drop dead/legacy duplicate SKUs (0 stock everywhere, never sold) - noise, not signal.
inv_filtered = [r for r in d['inventory'] if (r['total_inv'] or 0) > 0 or r['units_sold_ytd'] > 0]
inv_lines = []
for r in sorted(inv_filtered, key=lambda x: canonical_key(x['sku'])):
    name = SKU_SHORT_NAME.get(r['sku'], r['sku'])
    inv_lines.append(
        f"    {{ sku:{js_str(r['sku'])}, name:{js_str(name)}, az_inv:{r['az_inv']}, "
        f"wh_inv:{jn(r['wh_inv'])}, total_inv:{jn(r['total_inv'])}, "
        f"units_sold_ytd:{r['units_sold_ytd']}, avg_daily_sales:{jn(r['avg_daily_sales'],3)}, "
        f"reorder_point:{jn(r['reorder_point_90d'])}, days_of_cover:{jn(r['days_of_cover'])}, "
        f"suggested_reorder_qty:{r['suggested_reorder_qty']}, needs_reorder:{'true' if r['needs_reorder'] else 'false'} }},"
    )
inv_js = "const invData = [\n" + "\n".join(inv_lines) + "\n  ];"
with open('gen_inventory.txt', 'w', encoding='utf-8') as f:
    f.write(inv_js + "\n")

# ---------- KPI numbers ----------
mtd = monthly[-1]
refund_rate = abs(ytd['refunds']) / ytd['gross_revenue'] * 100 if ytd['gross_revenue'] else 0
net_proceeds_pct = ytd['net_proceeds'] / ytd['net_revenue'] * 100 if ytd['net_revenue'] else 0
net_profit_pct = ytd['net_profit'] / ytd['net_revenue'] * 100 if ytd['net_revenue'] else 0
ad_pct = ytd['ad_spend'] / ytd['gross_revenue'] * 100 if ytd['gross_revenue'] else 0
kpi = {
    'mtd_revenue': mtd['gross_revenue'], 'mtd_orders': mtd['orders'], 'mtd_units': mtd['units'],
    'net_revenue': ytd['net_revenue'], 'refund_rate': round(refund_rate, 1),
    'net_proceeds': ytd['net_proceeds'], 'net_proceeds_pct': round(net_proceeds_pct, 1),
    'net_profit': ytd['net_profit'], 'net_profit_pct': round(net_profit_pct, 1),
    'ad_spend': ytd['ad_spend'], 'ad_pct': round(ad_pct, 1),
    'revenue_per_day': ytd['revenue_per_day'], 'units_per_day': ytd['units_per_day'],
    'days_elapsed': ytd['days_elapsed'],
}
# ---------- returns breakdown (sellable vs non-sellable) ----------
returns_lines = []
for r in sorted(d.get('returns_breakdown', []), key=lambda x: canonical_key(x['sku'])):
    name = SKU_SHORT_NAME.get(r['sku'], r['sku'])
    returns_lines.append(
        f"    {{ sku:{js_str(r['sku'])}, name:{js_str(name)}, sellable:{r['sellable']}, "
        f"non_sellable:{r['non_sellable']}, total_returns:{r['total_returns']}, "
        f"cogs_reversed:{jn(r['cogs_reversed'])} }},"
    )
returns_js = "const returnsData = [\n" + "\n".join(returns_lines) + "\n  ];"
with open('gen_returns.txt', 'w', encoding='utf-8') as f:
    f.write(returns_js + "\n")

kpi['cogs_reversed_ytd'] = d.get('cogs_reversed_ytd', 0)

with open('gen_kpi.json', 'w', encoding='utf-8') as f:
    json.dump(kpi, f, indent=2)

print("All fragments generated.")
print(json.dumps(kpi, indent=2))
