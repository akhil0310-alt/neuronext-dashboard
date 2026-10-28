# -*- coding: utf-8 -*-
"""
Combines dashboard_data.json (Amazon) + noon_data.json (Noon) into
dashboard_data_combined.json, which carries three top-level keys:

  "amazon": dashboard_data.json, byte-for-byte (the existing Amazon tab's
            data - untouched, so the live Amazon view never changes).
  "noon":   noon_data.json, byte-for-byte.
  "overall": a NEW, computed combination of both - see build_overall() below
            for exactly what is and isn't combined, and why.

Design principle (per the user, 2026-10-03): don't fabricate detail one
channel doesn't have just to force it into the other's schema. Noon's finance
export has no sellable/non-sellable refund-disposition data the way Amazon's
FBA Customer Returns report does, and no per-shipment fee data as granular as
Amazon's - so "Overall" monthwise P&L is a simpler, honestly-scoped combined
table (gross revenue / COGS / gross margin / ad spend / other costs per
channel / net profit), not a forced merge into Amazon's full row structure.

Inventory Ageing Matrix and Inventory Costing/WAC are NOT recomputed here -
both are already channel-agnostic in dashboard_data.json (Ageing comes from
the shared "Neuronext SOH & Outbound" warehouse tracker, which already nets
out outbound movement "to Amazon or another channel"; Costing/landed-cost is
the same NEXTWAVE invoice data regardless of which channel sells the unit) -
confirmed with the user 2026-10-03. They're passed through under "overall"
unchanged, for display there alongside Amazon's own copy.

Run standalone: python merge_dashboard_data.py
Inputs: dashboard_data.json, noon_data.json (same folder)
Output: dashboard_data_combined.json (same folder)
"""
import json
import os
from collections import defaultdict

IN_DIR = os.path.dirname(os.path.abspath(__file__))
AMZ_PATH = os.environ.get("DASHBOARD_DATA_OUT", os.path.join(IN_DIR, "dashboard_data.json"))
NOON_PATH = os.environ.get("NOON_DATA_OUT", os.path.join(IN_DIR, "noon_data.json"))
OUT_PATH = os.environ.get("COMBINED_DATA_OUT", os.path.join(IN_DIR, "dashboard_data_combined.json"))

TRANSIT_DAYS = 90

# Same short display names gen_dashboard_pieces.py uses for the Amazon tab - dashboard_data.json's
# own "name" field is the full raw marketing title (too long for a table cell), so this is
# duplicated here rather than pulled from dashboard_data.json, same reasoning as elsewhere in
# this file (standalone, no cross-import of the Amazon pipeline's internals).
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


def build_overall_inventory(amz, noon):
    """az_inv + noon_inv + wh_inv per SKU - the three physical stock pools
    (Amazon FBA, Noon FBN, NeuroNext's own warehouse), all added together."""
    noon_inv_by_amz_sku = defaultdict(int)
    for row in noon["fbn_held_stock"]:
        if row["amazon_sku"]:
            noon_inv_by_amz_sku[row["amazon_sku"]] += row["gross_quantity"]

    noon_units_by_amz_sku = defaultdict(int)
    for row in noon["sku_rows"]:
        if row["amazon_sku"]:
            noon_units_by_amz_sku[row["amazon_sku"]] += row["units"]

    days_elapsed = amz["ytd"]["days_elapsed"]
    out = []
    for r in amz["inventory"]:
        sku = r["sku"]
        noon_inv = noon_inv_by_amz_sku.get(sku, 0)
        az_inv = r["az_inv"]
        wh_inv = r["wh_inv"]
        total_inv = az_inv + noon_inv + (wh_inv or 0)
        units_sold_ytd = r["units_sold_ytd"] + noon_units_by_amz_sku.get(sku, 0)
        avg_daily_sales = units_sold_ytd / days_elapsed if units_sold_ytd else 0.0
        reorder_point = avg_daily_sales * TRANSIT_DAYS
        days_of_cover = (total_inv / avg_daily_sales) if avg_daily_sales else None
        suggested_reorder_qty = max(0, round(reorder_point - total_inv)) if avg_daily_sales else 0
        out.append({
            "sku": sku, "asin": r.get("asin"), "name": SKU_SHORT_NAME.get(sku, r.get("name")),
            "az_inv": az_inv, "noon_inv": noon_inv, "wh_inv": wh_inv, "total_inv": total_inv,
            "units_sold_ytd": units_sold_ytd,
            "avg_daily_sales": round(avg_daily_sales, 3),
            "reorder_point_90d": round(reorder_point, 1),
            "days_of_cover": round(days_of_cover, 1) if days_of_cover is not None else None,
            "suggested_reorder_qty": suggested_reorder_qty,
            "needs_reorder": bool(avg_daily_sales and total_inv <= reorder_point),
        })
    return out


def build_noon_inventory(amz, noon):
    """wh_inv is the same shared-warehouse figure Amazon's inventory list already
    carries for this SKU - looked up by amazon_sku, not recomputed."""
    wh_inv_by_sku = {r["sku"]: r["wh_inv"] for r in amz["inventory"]}
    name_by_sku = {r["sku"]: SKU_SHORT_NAME.get(r["sku"], r["name"]) for r in amz["inventory"]}
    units_by_amz_sku = defaultdict(int)
    for row in noon["sku_rows"]:
        if row["amazon_sku"]:
            units_by_amz_sku[row["amazon_sku"]] += row["units"]
    days_elapsed = noon["ytd"]["days_elapsed"]

    stock_by_amz_sku = defaultdict(int)
    for row in noon["fbn_held_stock"]:
        if row["amazon_sku"]:
            stock_by_amz_sku[row["amazon_sku"]] += row["gross_quantity"]

    out = []
    for amazon_sku, noon_inv in stock_by_amz_sku.items():
        wh_inv = wh_inv_by_sku.get(amazon_sku)
        total_inv = noon_inv + (wh_inv or 0)
        units_sold_ytd = units_by_amz_sku.get(amazon_sku, 0)
        avg_daily_sales = units_sold_ytd / days_elapsed if units_sold_ytd else 0.0
        reorder_point = avg_daily_sales * TRANSIT_DAYS
        days_of_cover = (total_inv / avg_daily_sales) if avg_daily_sales else None
        suggested_reorder_qty = max(0, round(reorder_point - total_inv)) if avg_daily_sales else 0
        out.append({
            "sku": amazon_sku, "name": name_by_sku.get(amazon_sku, amazon_sku),
            "noon_inv": noon_inv, "wh_inv": wh_inv, "total_inv": total_inv,
            "units_sold_ytd": units_sold_ytd,
            "avg_daily_sales": round(avg_daily_sales, 3),
            "reorder_point_90d": round(reorder_point, 1),
            "days_of_cover": round(days_of_cover, 1) if days_of_cover is not None else None,
            "suggested_reorder_qty": suggested_reorder_qty,
            "needs_reorder": bool(avg_daily_sales and total_inv <= reorder_point),
        })
    out.sort(key=lambda r: -r["total_inv"])
    return out


def build_overall_monthly(amz, noon):
    amz_by_month = {m["month"]: m for m in amz["monthly"]}
    noon_by_month = {m["month"]: m for m in noon["monthly"]}
    months = sorted(set(amz_by_month) | set(noon_by_month))
    rows = []
    for month in months:
        a = amz_by_month.get(month)
        n = noon_by_month.get(month)
        a_rev = a["gross_revenue"] if a else 0.0
        n_rev = n["gross_revenue"] if n else 0.0
        a_cogs = a["cogs"] if a else 0.0
        n_cogs = n["cogs"] if n else 0.0
        a_ads = a["ad_spend"] if a else 0.0
        n_ads = n["ad_spend"] if n else 0.0
        a_other = a["other_costs"] if a else 0.0  # Amazon: fees/storage/adjustments net of refund credits
        a_refunds = a["refunds"] if a else 0.0  # Amazon: sellable + non-sellable refunds
        n_other = (n["net_before_fees"] - n["gross_revenue"]) if n else 0.0  # Noon: referral+fulfillment+etc
        a_net = a["net_profit"] if a else 0.0
        n_net = n["net_profit"] if n else 0.0
        # "Other costs (Amazon), for the waterfall" folds in refunds too - the Monthwise
        # table's own other_costs_amazon column stays refund-exclusive (matches the
        # Amazon tab's own breakdown), this is a separate field just for net_proceeds.
        a_other_incl_refunds = a_other + a_refunds
        rows.append({
            "month": month,
            "gross_revenue": round(a_rev + n_rev, 2),
            "gross_revenue_amazon": round(a_rev, 2), "gross_revenue_noon": round(n_rev, 2),
            "cogs": round(a_cogs + n_cogs, 2),
            "gross_margin": round((a_rev + n_rev) - (a_cogs + n_cogs), 2),
            "other_costs_amazon": round(a_other, 2), "other_costs_noon": round(n_other, 2),
            "other_costs_amazon_incl_refunds": round(a_other_incl_refunds, 2),
            "ad_spend": round(a_ads + n_ads, 2),
            "net_profit": round(a_net + n_net, 2),
            "net_profit_amazon": round(a_net, 2), "net_profit_noon": round(n_net, 2),
            "orders": (a["orders"] if a else 0) + (n["orders"] if n else 0),
            "units": (a["units"] if a else 0) + (n["units"] if n else 0),
        })
    return rows


def _merge_month(a_m, n_m):
    """Combines one Amazon per-SKU-month record (dashboard_data.json's own shape:
    revenue/units/cogs/amazon_fees/ad_spend_allocated/storage_allocated/net_profit/...)
    with one Noon per-SKU-month record (noon_data.json's shape: gross_revenue/units/
    cogs/referral_fee/fulfillment_fee/ad_spend_allocated/net_profit/...) into one
    combined record with the SAME field names as noon_data.json's month_detail() output,
    so the dashboard's Product-wise Profitability table can use one rendering code path
    for Noon and Overall alike (added 2026-10-04, user-requested table parity)."""
    a_rev = a_m["revenue"] if a_m else 0.0
    n_rev = n_m["gross_revenue"] if n_m else 0.0
    units = (a_m["units"] if a_m else 0) + (n_m["units"] if n_m else 0)
    gross_revenue = a_rev + n_rev
    cogs = (a_m["cogs"] if a_m else 0.0) + (n_m["cogs"] if n_m else 0.0)
    fees = ((a_m["amazon_fees"] if a_m else 0.0) + (a_m["storage_allocated"] if a_m else 0.0)
            + (n_m["referral_fee"] if n_m else 0.0) + (n_m["fulfillment_fee"] if n_m else 0.0))
    ad_alloc = (a_m["ad_spend_allocated"] if a_m else 0.0) + (n_m["ad_spend_allocated"] if n_m else 0.0)
    gross_margin = gross_revenue - cogs
    net_profit = gross_revenue + fees + ad_alloc - cogs
    return {
        "units": units, "units_amazon": a_m["units"] if a_m else 0, "units_noon": n_m["units"] if n_m else 0,
        "gross_revenue": round(gross_revenue, 2),
        "gross_revenue_amazon": round(a_rev, 2), "gross_revenue_noon": round(n_rev, 2),
        "asp": round(gross_revenue / units, 2) if units else None,
        "cogs": round(cogs, 2), "fees": round(fees, 2), "ad_spend_allocated": round(ad_alloc, 2),
        "gross_margin": round(gross_margin, 2),
        "margin_pct": round(gross_margin / gross_revenue * 100, 1) if gross_revenue else None,
        "net_profit": round(net_profit, 2),
        "net_margin_pct": round(net_profit / gross_revenue * 100, 1) if gross_revenue else None,
    }


def _combine_noon_rows(rows):
    """Sums multiple Noon sku_rows that map to the SAME Amazon SKU (e.g. S12ASBPV1DB /
    S12DBPV1DB / S12ADBPV1DB, three different Noon partner_sku codes confirmed by the
    user to all be the same physical S12A Double product) into one combined record with
    the same shape as a single sku_rows entry - real bug, fixed 2026-10-04: a plain
    dict keyed by amazon_sku (`{row["amazon_sku"]: row}`) silently OVERWRITES on
    collision, keeping only whichever row was processed last and dropping the other
    SKUs' units/revenue/fees entirely from every Overall figure - never caught earlier
    because only one Noon partner_sku per Amazon SKU had 2026 sales; multiple 2025-only
    codes resolving to the same product is what exposed it."""
    units = sum(r["units"] for r in rows)
    gross_revenue = sum(r["gross_revenue"] for r in rows)
    cogs = sum(r["cogs"] for r in rows)
    referral_fee = sum(r["referral_fee"] for r in rows)
    fulfillment_fee = sum(r["fulfillment_fee"] for r in rows)
    ad_spend_allocated = sum(r["ad_spend_allocated"] for r in rows)
    month_fields = ["units", "gross_revenue", "cogs", "referral_fee", "fulfillment_fee", "ad_spend_allocated"]
    months = defaultdict(lambda: {k: 0 if k == "units" else 0.0 for k in month_fields})
    for r in rows:
        for m, v in r["months"].items():
            for k in month_fields:
                months[m][k] += v[k]
    return {
        "units": units, "gross_revenue": round(gross_revenue, 2), "cogs": round(cogs, 2),
        "referral_fee": round(referral_fee, 2), "fulfillment_fee": round(fulfillment_fee, 2),
        "ad_spend_allocated": round(ad_spend_allocated, 2),
        "cost_per_unit": rows[0].get("cost_per_unit"),
        "months": {m: {k: (v[k] if k == "units" else round(v[k], 2)) for k in month_fields}
                   for m, v in months.items()},
    }


def build_overall_sku_rows(amz, noon):
    noon_rows_by_amz_sku = defaultdict(list)
    for row in noon["sku_rows"]:
        if row["amazon_sku"]:
            noon_rows_by_amz_sku[row["amazon_sku"]].append(row)
    noon_by_amz_sku = {amazon_sku: _combine_noon_rows(rows) for amazon_sku, rows in noon_rows_by_amz_sku.items()}

    rows = []
    seen_amz_skus = set()
    for a in amz["sku_rows"]:
        sku = a["sku"]
        seen_amz_skus.add(sku)
        n = noon_by_amz_sku.get(sku)
        all_months = sorted(set(a["months"]) | set(n["months"] if n else {}))
        months = {m: _merge_month(a["months"].get(m), n["months"].get(m) if n else None) for m in all_months}
        ytd = _merge_month(
            {"revenue": a["gross_revenue"], "units": a["units"], "cogs": a["cogs"],
             "amazon_fees": a["amazon_fees"], "storage_allocated": a["storage_allocated"],
             "ad_spend_allocated": a["ad_spend_allocated"]},
            {"gross_revenue": n["gross_revenue"], "units": n["units"], "cogs": n["cogs"],
             "referral_fee": n["referral_fee"], "fulfillment_fee": n["fulfillment_fee"],
             "ad_spend_allocated": n["ad_spend_allocated"]} if n else None,
        )
        rows.append({"sku": sku, "cost_per_unit": a.get("cost_per_unit"), **ytd, "months": months})
    # Noon SKUs mapped to an Amazon SKU not present in amz['sku_rows'] at all (possible if
    # the SKU has Noon sales but zero Amazon sales YTD) - shown Noon-only, Amazon side 0.
    for amazon_sku, n in noon_by_amz_sku.items():
        if amazon_sku in seen_amz_skus:
            continue
        months = {m: _merge_month(None, v) for m, v in n["months"].items()}
        ytd = _merge_month(None, {
            "gross_revenue": n["gross_revenue"], "units": n["units"], "cogs": n["cogs"],
            "referral_fee": n["referral_fee"], "fulfillment_fee": n["fulfillment_fee"],
            "ad_spend_allocated": n["ad_spend_allocated"],
        })
        rows.append({"sku": amazon_sku, "cost_per_unit": n.get("cost_per_unit"), **ytd, "months": months})
    rows.sort(key=lambda r: -r["gross_revenue"])
    return rows


def build_cost_table(amz_cost_table, sku_asp_by_sku):
    """Cost-per-unit & recommended ASP table for a non-Amazon channel view (Noon or
    Overall): reuses the EXACT SAME product_cost/freight/duty/vat/landed_cost figures
    from the Amazon tab's own cost_table (channel-agnostic, see the Channel switcher's
    2026-10-03 note on costing) - only `actual_asp` and the margin computed from it
    differ, since ASP is genuinely a per-channel fact. `sku_asp_by_sku` maps Amazon SKU
    -> that channel's own ASP (None if the SKU isn't sold on that channel at all)."""
    rows = []
    for c in amz_cost_table:
        asp = sku_asp_by_sku.get(c["sku"])
        margin_at_actual = ((asp - c["landed_cost"]) / asp * 100) if (asp and c["landed_cost"] is not None) else None
        rows.append({**c, "name": SKU_SHORT_NAME.get(c["sku"], c["sku"]),
                     "actual_asp": round(asp, 2) if asp is not None else None,
                     "margin_at_actual_pct": round(margin_at_actual, 1) if margin_at_actual is not None else None})
    return rows


def build_overall(amz, noon):
    monthly = build_overall_monthly(amz, noon)
    ytd = {
        "gross_revenue": round(sum(m["gross_revenue"] for m in monthly), 2),
        "cogs": round(sum(m["cogs"] for m in monthly), 2),
        "gross_margin": round(sum(m["gross_margin"] for m in monthly), 2),
        "ad_spend": round(sum(m["ad_spend"] for m in monthly), 2),
        "net_profit": round(sum(m["net_profit"] for m in monthly), 2),
        "net_profit_amazon": round(sum(m["net_profit_amazon"] for m in monthly), 2),
        "net_profit_noon": round(sum(m["net_profit_noon"] for m in monthly), 2),
        "orders": sum(m["orders"] for m in monthly),
        "units": sum(m["units"] for m in monthly),
        "days_elapsed": amz["ytd"]["days_elapsed"],
    }
    other_costs_amazon_incl_refunds = sum(m["other_costs_amazon_incl_refunds"] for m in monthly)
    other_costs_noon = sum(m["other_costs_noon"] for m in monthly)
    # Net proceeds (for the waterfall): gross revenue through fees/refunds + ad spend,
    # EXCLUDING COGS - same definition as Amazon's own "Where the revenue goes" waterfall.
    ytd["net_proceeds"] = round(ytd["gross_revenue"] + other_costs_amazon_incl_refunds + other_costs_noon + ytd["ad_spend"], 2)
    ytd["other_costs_amazon_incl_refunds"] = round(other_costs_amazon_incl_refunds, 2)
    ytd["other_costs_noon"] = round(other_costs_noon, 2)
    ytd["revenue_per_day"] = round(ytd["gross_revenue"] / ytd["days_elapsed"], 2)
    ytd["units_per_day"] = round(ytd["units"] / ytd["days_elapsed"], 3)
    overall_sku_rows = build_overall_sku_rows(amz, noon)
    return {
        "months": [m["month"] for m in monthly],
        "monthly": monthly,
        "ytd": ytd,
        "sku_rows": overall_sku_rows,
        "cost_table": build_cost_table(amz["cost_table"], {r["sku"]: r["asp"] for r in overall_sku_rows}),
        "inventory": build_overall_inventory(amz, noon),
        "inventory_ageing": amz["inventory_ageing"],
        "inventory_ageing_totals": amz["inventory_ageing_totals"],
    }


def main():
    with open(AMZ_PATH, encoding="utf-8") as f:
        amz = json.load(f)
    with open(NOON_PATH, encoding="utf-8") as f:
        noon = json.load(f)
    noon["inventory"] = build_noon_inventory(amz, noon)
    # Noon's own cost-per-unit table: same landed-cost figures as Amazon's (see
    # build_cost_table docstring), scoped to only the SKUs actually listed on Noon
    # (most Amazon SKUs aren't), with Noon's own ASP.
    # Units-weighted blend across multiple Noon partner_skus mapping to the same Amazon
    # SKU (e.g. the 3 S12A Double codes) - same collision class as build_overall_sku_rows'
    # noon_by_amz_sku, fixed 2026-10-04. A plain {amazon_sku: r["asp"]} dict would silently
    # keep only one partner_sku's ASP and drop the others.
    noon_rows_by_amz_sku_for_asp = defaultdict(list)
    for r in noon["sku_rows"]:
        if r["amazon_sku"]:
            noon_rows_by_amz_sku_for_asp[r["amazon_sku"]].append(r)
    noon_asp_by_sku = {
        amazon_sku: (sum(r["gross_revenue"] for r in rows) / sum(r["units"] for r in rows))
        for amazon_sku, rows in noon_rows_by_amz_sku_for_asp.items() if sum(r["units"] for r in rows)
    }
    noon["cost_table"] = [row for row in build_cost_table(amz["cost_table"], noon_asp_by_sku)
                          if row["sku"] in noon_asp_by_sku]
    combined = {
        "generated_at": amz["generated_at"],
        "amazon": amz,
        "noon": noon,
        "overall": build_overall(amz, noon),
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2)
    print(f"Wrote {OUT_PATH}")
    print(f"  Overall YTD gross_revenue={combined['overall']['ytd']['gross_revenue']} "
          f"net_profit={combined['overall']['ytd']['net_profit']}")
    print(f"  Noon inventory SKUs: {len(combined['noon']['inventory'])}")
    print(f"  Overall inventory SKUs: {len(combined['overall']['inventory'])}")
    print(f"  Overall sku_rows: {len(combined['overall']['sku_rows'])}")


if __name__ == "__main__":
    main()
