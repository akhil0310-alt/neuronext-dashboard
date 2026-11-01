# -*- coding: utf-8 -*-
"""Builds the unified NeuroNext dashboard (Overall / Amazon / Noon) as ONE standalone HTML file.

Reads dashboard_data_combined.json (amazon / noon / overall, written by merge_dashboard_data.py),
normalises all three channels into the SAME schema (so one renderer draws all of them - the whole
point of the 2026-11 revamp is that a table which exists on two channels is literally the same
code), embeds that as JSON into dashboard_template.html and writes the result.

    python build_dashboard.py [--year 2025] [--out PATH]

Env overrides (CI): COMBINED_DATA_OUT (input json), DASHBOARD_HTML_OUT (output html).
Replaces gen_dashboard_pieces / gen_dashboard_html / splice_and_validate / inject_unified_dashboard
once approved; until then it only writes the preview file and touches nothing live.
"""
import argparse
import calendar
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from merge_dashboard_data import SKU_SHORT_NAME  # noqa: E402  (names shared with the old pipeline)

TEMPLATE_PATH = os.path.join(HERE, "dashboard_template.html")
# Provision applied to landed cost by age bucket (same table as AGEING_BUCKETS in dashboard_refresh_data.py)
AGEING_PROVISION = {"0-90": 0.0, "90-120": 0.10, "120-180": 0.25, "180-270": 0.50, "270-360": 0.75, "360+": 1.0}
YEAR_PAGES = {"2026": "./", "2025": "2025.html"}
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]

# One shared set of cost components, so a fee row means the same thing on every channel.
FEE_COMPONENTS = [
    ("commission", "Referral commission"),
    ("fulfillment", "Fulfillment & logistics"),
    ("storage", "Storage"),
    ("shipping_credits", "Shipping credits"),
    ("refund_credits", "Refund credits"),
    ("chargebacks_adj", "Chargebacks & adjustments"),
    ("promotions", "Promotions & subsidies"),
    ("other", "Other"),
]
AZ_PARTS = {
    "commission": ["commission"], "fulfillment": ["fulfillment"], "storage": ["storage"],
    "shipping_credits": ["shipcharges"], "refund_credits": ["refund_credits"],
    "chargebacks_adj": ["chargebacks", "adjustments"], "promotions": ["promotions"],
    "other": ["other_fees"],
}
NOON_PARTS = {
    "commission": ["referral"], "fulfillment": ["fulfillment"], "shipping_credits": ["shipping_credits"],
    "promotions": ["order_subsidies", "non_order_subsidies"], "other": ["other_order_fees", "others"],
}


def r2(x):
    return round(float(x or 0), 2)


def fee_parts(cb, mapping):
    return {k: r2(sum(cb.get(src, 0) for src in srcs)) for k, srcs in mapping.items()}


def add_parts(a, b):
    return {k: r2(a.get(k, 0) + b.get(k, 0)) for k in set(a) | set(b)}


def month_row(month, gross=0.0, refunds=0.0, fees=0.0, ad=0.0, cogs=0.0, orders=0, units=0, parts=None):
    net_proceeds = gross + refunds + fees + ad
    return {"month": month, "gross": r2(gross), "refunds": r2(refunds), "fees": r2(fees), "ad": r2(ad),
            "net_proceeds": r2(net_proceeds), "cogs": r2(cogs), "net_profit": r2(net_proceeds - cogs),
            "orders": orders, "units": units, "parts": parts or {}}


def amazon_months(az, axis):
    by = {m["month"]: m for m in az["monthly"]}
    out = []
    for mo in axis:
        m = by.get(mo)
        if not m:
            out.append(month_row(mo))
            continue
        out.append(month_row(mo, m["gross_revenue"], m["refunds"], m["other_costs"], m["ad_spend"], m["cogs"],
                             m["orders"], m["units"], fee_parts(m["cost_breakdown"], AZ_PARTS)))
    return out


def noon_months(nn, axis):
    by = {m["month"]: m for m in nn["monthly"]}
    out = []
    for mo in axis:
        m = by.get(mo)
        if not m:
            out.append(month_row(mo))
            continue
        parts = fee_parts(m["cost_breakdown"], NOON_PARTS)
        fees = m["net_before_fees"] - m["gross_revenue"]
        out.append(month_row(mo, m["gross_revenue"], 0.0, fees, m["ad_spend"], m["cogs"], m["orders"], m["units"], parts))
    return out


def overall_months(a_rows, n_rows):
    out = []
    for a, n in zip(a_rows, n_rows):
        row = month_row(a["month"], a["gross"] + n["gross"], a["refunds"] + n["refunds"], a["fees"] + n["fees"],
                        a["ad"] + n["ad"], a["cogs"] + n["cogs"], a["orders"] + n["orders"], a["units"] + n["units"],
                        add_parts(a["parts"], n["parts"]))
        row["split"] = {"gross_amazon": a["gross"], "gross_noon": n["gross"],
                        "net_profit_amazon": a["net_profit"], "net_profit_noon": n["net_profit"],
                        "units_amazon": a["units"], "units_noon": n["units"]}
        out.append(row)
    return out


def sku_month(units=0, gross=0.0, refunds=0.0, fees=0.0, ad=0.0, cogs=0.0):
    return {"units": units, "gross": r2(gross), "refunds": r2(refunds), "fees": r2(fees), "ad": r2(ad),
            "cogs": r2(cogs), "net_profit": r2(gross + refunds + fees + ad - cogs)}


def amazon_skus(az):
    out = {}
    for r in az["sku_rows"]:
        months = {}
        for mo, v in r["months"].items() if isinstance(r["months"], dict) else r["months"]:
            months[mo] = sku_month(v["units"], v["revenue"], v["refunds"],
                                   v["amazon_fees"] + v.get("storage_allocated", 0), v["ad_spend_allocated"], v["cogs"])
        out[r["sku"]] = months
    return out


def noon_skus(nn):
    out = {}
    for r in nn["sku_rows"]:
        sku = r.get("amazon_sku") or r["partner_sku"]
        tgt = out.setdefault(sku, {})
        for mo, v in r["months"].items():
            cur = tgt.get(mo) or sku_month()
            tgt[mo] = sku_month(cur["units"] + v["units"], cur["gross"] + v["gross_revenue"], 0.0,
                                cur["fees"] + v["fees"], cur["ad"] + v["ad_spend_allocated"], cur["cogs"] + v["cogs"])
    return out


def merge_sku_months(a, b):
    out = {}
    for sku in set(a) | set(b):
        months = {}
        for mo in set(a.get(sku, {})) | set(b.get(sku, {})):
            x, y = a.get(sku, {}).get(mo) or sku_month(), b.get(sku, {}).get(mo) or sku_month()
            m = sku_month(x["units"] + y["units"], x["gross"] + y["gross"], x["refunds"] + y["refunds"],
                          x["fees"] + y["fees"], x["ad"] + y["ad"], x["cogs"] + y["cogs"])
            m["gross_amazon"], m["gross_noon"] = x["gross"], y["gross"]
            m["units_amazon"], m["units_noon"] = x["units"], y["units"]
            months[mo] = m
        out[sku] = months
    return out


def sku_list(sku_months, order, names):
    rows = []
    for sku in order:
        months = sku_months.get(sku)
        if not months:
            continue
        rows.append({"sku": sku, "name": names.get(sku, sku), "months": months})
    return rows


def cost_rows(table, names, order):
    by = {r["sku"]: r for r in table}
    out = []
    for sku in order:
        r = by.get(sku)
        if not r:
            continue
        asp = r.get("actual_asp")
        cost = r.get("landed_cost")
        out.append({"sku": sku, "name": names.get(sku, sku), "landed": cost, "asp": asp,
                    "rec_asp": r.get("recommended_asp_60pct_margin"),
                    "margin_pct": round((asp - cost) / asp * 100, 1) if asp and cost is not None else None,
                    "estimated": bool(r.get("is_estimated")), "wac_revised": bool(r.get("wac_revised")),
                    "product_usd": r.get("product_cost_usd"), "freight_usd": r.get("freight_usd"),
                    "duty_usd": r.get("duty_usd"), "vat_usd": r.get("vat_usd"), "landed_usd": r.get("landed_cost_usd")})
    return out


REORDER_WINDOW_DAYS = 30   # sales window behind per-day, cover and reorder
REORDER_COVER_DAYS = 90    # reorder when stock is at or below this many days of sales


def recent_units(daily, sku, as_of_date, window=REORDER_WINDOW_DAYS):
    cutoff = (as_of_date - datetime.timedelta(days=window)).isoformat()
    return sum(d["units"] for d in daily.get(sku, []) if d["date"] > cutoff)


def inventory_block(rows, names, order, kind, daily, as_of_date):
    by = {r["sku"]: r for r in rows}
    out = []
    for sku in order:
        r = by.get(sku)
        if not r:
            continue
        sold = recent_units(daily, sku, as_of_date)
        per_day = sold / REORDER_WINDOW_DAYS
        total = r.get("total_inv", 0)
        reorder_point = per_day * REORDER_COVER_DAYS
        row = {"sku": sku, "name": names.get(sku, sku), "wh": r.get("wh_inv", 0), "total": total,
               "sold_recent": sold, "per_day": round(per_day, 3),
               "cover_days": round(total / per_day, 1) if per_day else None,
               "reorder_qty": max(0, round(reorder_point - total)) if per_day else 0,
               "needs_reorder": bool(per_day and total <= reorder_point)}
        if kind in ("amazon", "overall"):
            row["az"] = r.get("az_inv", 0)
        if kind == "amazon":
            row["az_inbound"] = r.get("inbound", 0)
            row["az_unfulfillable"] = r.get("unfulfillable", 0)
        if kind in ("noon", "overall"):
            row["noon"] = r.get("noon_inv", 0)
        out.append(row)
    cols = {
        "amazon": [("az", "At Amazon"), ("az_inbound", "Inbound"), ("wh", "Warehouse"), ("total", "Total")],
        "noon": [("noon", "At Noon"), ("wh", "Warehouse"), ("total", "Total")],
        "overall": [("az", "At Amazon"), ("noon", "At Noon"), ("wh", "Warehouse"), ("total", "Total")],
    }[kind]
    return {"columns": [{"key": k, "label": l} for k, l in cols], "rows": out}


def merge_daily(a, b):
    out = {k: list(v) for k, v in (a or {}).items()}
    for k, v in (b or {}).items():
        out.setdefault(k, []).extend(v)
    return out


def daily_list(d):
    return [{"sku": k, "days": [{"date": x["date"], "units": x["units"], "price": x["price"]} for x in v]}
            for k, v in d.items() if v]


def ledger_rows(ledger, names):
    out = []
    for r in ledger:
        batches = r["batches"]
        last = batches[-1] if batches else None
        total_qty = last["running_qty"] if last else 0
        wac = r["current_wac_aed"]
        uk = [b for b in batches if str(b.get("note", "")).startswith("UK batch")]
        uk_qty = sum(b["qty"] for b in uk)
        uk_val = sum(b["qty"] * b["landed_cost_aed"] for b in uk)
        uae_qty = total_qty - uk_qty
        uae_val = total_qty * wac - uk_val
        out.append({
            "sku": r["sku"], "name": names.get(r["sku"], r["sku"]), "wac": wac, "last_batch": r["last_batch_date"],
            "days_since": r["days_since_last_batch"], "qty": total_qty,
            "uae_qty": uae_qty, "uae_cost": round(uae_val / uae_qty, 2) if uae_qty > 0 else None,
            "uk_qty": uk_qty, "uk_cost": round(uk_val / uk_qty, 2) if uk_qty else None,
            "batches": [{"date": b["date"], "qty": b["qty"], "cost": b["landed_cost_aed"],
                         "run_qty": b["running_qty"], "run_wac": b["running_wac_aed"], "note": b.get("note", "")}
                        for b in batches],
        })
    return out


def day_block(groups, day, names):
    """Sales on one day. groups = [(label, daily_sales_dict)] where daily_sales_dict is sku -> [{date, units, revenue}]."""
    iso = day.isoformat()
    out = []
    for label, daily in groups:
        skus = []
        for sku, days in daily.items():
            rows = [d for d in days if d["date"] == iso]
            if rows:
                skus.append({"name": names.get(sku, sku), "units": sum(d["units"] for d in rows),
                             "gross": r2(sum(d["revenue"] for d in rows))})
        skus.sort(key=lambda x: -x["gross"])
        out.append({"label": label, "units": sum(x["units"] for x in skus), "gross": r2(sum(x["gross"] for x in skus)), "skus": skus})
    return {"date": iso, "label": f"{day.day} {MONTH_NAMES[day.month - 1][:3]}", "units": sum(g["units"] for g in out),
            "gross": r2(sum(g["gross"] for g in out)), "groups": out}


def kpis(months, days_elapsed, as_of):
    ytd = {k: sum(m[k] for m in months) for k in ("gross", "refunds", "fees", "ad", "net_proceeds", "cogs", "net_profit", "orders", "units")}
    last = months[-1]
    y, mm = (int(x) for x in last["month"].split("-"))
    # MTD day: today's date for the live month; the month's last day for a closed year
    day = as_of.day if (as_of.year, as_of.month) == (y, mm) else calendar.monthrange(y, mm)[1]
    gross = ytd["gross"]
    return {
        "mtd": {"label": MONTH_NAMES[mm - 1], "gross": last["gross"], "orders": last["orders"], "units": last["units"],
                "day": day},
        "gross": r2(gross), "gross_orders": ytd["orders"],
        "refunds_pct": round(-ytd["refunds"] / gross * 100, 1) if gross and ytd["refunds"] else None,
        "net_proceeds": r2(ytd["net_proceeds"]), "net_proceeds_pct": round(ytd["net_proceeds"] / gross * 100, 1) if gross else 0,
        "net_profit": r2(ytd["net_profit"]), "net_profit_pct": round(ytd["net_profit"] / gross * 100, 1) if gross else 0,
        "ad": r2(ytd["ad"]), "ad_pct": round(-ytd["ad"] / gross * 100, 1) if gross else 0,
        "rev_per_day": round(gross / days_elapsed, 2) if days_elapsed else 0,
        "units_per_day": round(ytd["units"] / days_elapsed, 2) if days_elapsed else 0,
        "days": days_elapsed, "units": ytd["units"],
    }


def build(combined, year):
    az, nn, ov = combined["amazon"], combined["noon"], combined["overall"]
    as_of_dt = datetime.datetime.fromisoformat(az["as_of"].replace("Z", "+00:00")).astimezone(
        datetime.timezone(datetime.timedelta(hours=4)))  # Dubai (GST)
    axis = sorted(set(az["months"]) | set(nn.get("months", [])))
    days = az["ytd"]["days_elapsed"]
    closed = bool(az.get("is_full_year"))

    names = dict(SKU_SHORT_NAME)
    for r in az.get("inventory", []) + ov.get("inventory", []):
        names.setdefault(r["sku"], r.get("name") or r["sku"])

    a_months = amazon_months(az, axis)
    n_months = noon_months(nn, axis)
    o_months = overall_months(a_months, n_months)

    a_sku = amazon_skus(az)
    n_sku = noon_skus(nn)
    o_sku = merge_sku_months(a_sku, n_sku)
    gross_total = {s: sum(m["gross"] for m in ms.values()) for s, ms in o_sku.items()}
    order = sorted(o_sku, key=lambda s: -gross_total[s])
    extra = sorted((r for r in az["cost_table"] if r["sku"] not in o_sku), key=lambda r: -(r["landed_cost"] or 0))
    cost_order = order + [r["sku"] for r in extra]

    a_daily, n_daily = az.get("daily_sales", {}), nn.get("daily_sales", {})
    yday = as_of_dt.date() - datetime.timedelta(days=1)
    yday_groups = {"overall": [("Amazon", a_daily), ("Noon", n_daily)], "amazon": [("Amazon", a_daily)], "noon": [("Noon", n_daily)]}
    az_cost = cost_rows(az["cost_table"], names, cost_order)
    noon_cost = cost_rows(nn["cost_table"], names, cost_order)
    ov_cost = cost_rows(ov["cost_table"], names, cost_order)

    def channel(key, label, months, skus, cost, daily, inv, has_refunds, notes):
        k = kpis(months, days, as_of_dt)
        k["yday"] = day_block(yday_groups[key], yday, names)
        return {"key": key, "label": label, "months": months, "kpis": k,
                "has_refunds": has_refunds, "skus": sku_list(skus, order, names), "cost": cost,
                "daily": daily_list(daily), "inventory": inv, "notes": notes}

    az_notes = [
        "Revenue, fees, refunds, storage and reimbursements come from the Amazon Selling Partner API (orders and settlement events).",
        "Refunds reduce revenue; the cost of units returned in sellable condition is reversed out of COGS.",
        "Ad spend is the advertising fee charged in settlements. It is allocated to products by revenue share; per-campaign ad data is not connected.",
        "Cost of goods uses the company-wide weighted-average landed cost.",
    ]
    noon_notes = [
        "Revenue is Net Proceeds per item from the Noon finance export; fees are referral, fulfillment and logistics from the same export.",
        "The Noon export has no returns, reimbursement or removal data, so those lines do not appear for Noon.",
        "Ad spend is the advertising fee charged in settlements, allocated to products by revenue share; per-campaign ad data is not connected.",
        "Cost of goods uses the same company-wide landed cost as Amazon.",
    ]
    ov_notes = [
        "Amazon and Noon added together. Cost of goods is identical on both channels; selling prices differ by channel.",
        "Refunds are Amazon only; Noon does not report them.",
        "Warehouse ageing and costing apply to the whole company and are shown once, under Warehouse.",
    ]
    channels = {
        "overall": channel("overall", "Overall", o_months, o_sku, ov_cost, merge_daily(a_daily, n_daily),
                           inventory_block(ov.get("inventory", []), names, cost_order, "overall", merge_daily(a_daily, n_daily), as_of_dt.date()), True, ov_notes),
        "amazon": channel("amazon", "Amazon", a_months, a_sku, az_cost, a_daily,
                          inventory_block(az.get("inventory", []), names, cost_order, "amazon", a_daily, as_of_dt.date()), True, az_notes),
        "noon": channel("noon", "Noon", n_months, n_sku, noon_cost, n_daily,
                        inventory_block(nn.get("inventory", []), names, cost_order, "noon", n_daily, as_of_dt.date()), False, noon_notes),
    }

    ageing = []
    for r in az.get("inventory_ageing", []):
        # provision = sum over age buckets of (bucket value x that bucket's %), so it ties exactly to the
        # bucket columns shown (0-90 carries 0%; here it all comes from the older buckets)
        provision = round(sum(b["value"] * AGEING_PROVISION.get(k, 0) for k, b in r["buckets"].items()), 2)
        ageing.append({"sku": r["sku"], "name": names.get(r["sku"], r["sku"]), "landed": r["landed_cost"],
                       "marked_down": r["marked_down_cost"], "units": r["total_units"], "value": r["total_value"],
                       "marked_value": round(r["total_value"] - provision, 2),
                       "provision": provision, "buckets": r["buckets"]})
    ageing_order = {s: i for i, s in enumerate(cost_order)}
    ageing.sort(key=lambda r: ageing_order.get(r["sku"], 999))
    ageing_tot = az.get("inventory_ageing_totals", {})

    az_only = {
        "returns": [{"sku": r["sku"], "name": names.get(r["sku"], r["sku"]), "total": r["total_returns"],
                     "sellable": r["sellable"], "non_sellable": r["non_sellable"],
                     "customer_damaged": r.get("customer_damaged", 0), "defective": r.get("defective", 0),
                     "amazon_damaged": r.get("amazon_damaged", 0), "other": r.get("other_non_sellable", 0),
                     "cogs_reversed": r.get("cogs_reversed", 0)} for r in az.get("returns_breakdown", [])],
        "reimbursements": [{"date": r["date"], "sku": r["sku"], "name": names.get(r["sku"], r["sku"]),
                            "reason": r["reason"], "amount": r["amount_aed"], "qty": r["quantity"]}
                           for r in az.get("reimbursements", [])],
        "removals": [{"date": r["date"], "sku": r["sku"], "name": names.get(r["sku"], r["sku"]),
                      "source": r["source"], "aged": bool(r.get("is_aged")), "status": r["status"],
                      "disposition": r["disposition"], "requested": r["requested_qty"], "shipped": r["shipped_qty"],
                      "cancelled": r["cancelled_qty"]} for r in az.get("removal_orders", [])],
    }

    return {
        "meta": {
            "year": year, "closed": closed,
            "as_of": (f"full year {year}, closed" if closed else as_of_dt.strftime("as of %d %b %Y, %H:%M GST")),
            "as_of_iso": as_of_dt.isoformat(), "days": days, "year_pages": YEAR_PAGES,
            "month_names": MONTH_NAMES, "fee_components": [{"key": k, "label": l} for k, l in FEE_COMPONENTS],
        },
        "channels": channels,
        "warehouse": {
            "ageing": ageing, "ageing_buckets": list((ageing_tot.get("buckets") or {}).keys()) or
            (list(ageing[0]["buckets"].keys()) if ageing else []),
            "ageing_provision": AGEING_PROVISION,
            "ageing_totals": {"units": ageing_tot.get("total_units", 0), "value": ageing_tot.get("total_value", 0),
                              "marked_value": round(sum(r["marked_value"] for r in ageing), 2),
                              "provision": round(sum(r["provision"] for r in ageing), 2),
                              "buckets": ageing_tot.get("buckets", {})},
            "ledger": ledger_rows(az.get("cost_ledger", []), names),
            "noon_fbn": {"rows": [{"sku": r["sku"], "partner_sku": r["partner_sku"], "amazon_sku": r.get("amazon_sku"),
                                   "name": names.get(r.get("amazon_sku"), r["partner_sku"]),
                                   "condition": r["condition"], "snapshot": r["snapshot_date"], "qty": r["gross_quantity"],
                                   "buckets": r["buckets"]} for r in nn.get("fbn_held_stock", [])],
                         "totals": nn.get("fbn_held_stock_totals", {})},
        },
        "amazon_only": az_only,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=datetime.datetime.now().year)
    ap.add_argument("--out", default=os.environ.get("DASHBOARD_HTML_OUT"))
    args = ap.parse_args()

    in_path = os.environ.get("COMBINED_DATA_OUT")
    if not in_path:
        suffix = "" if args.year == datetime.datetime.now().year else f"_{args.year}"
        in_path = os.path.join(HERE, f"dashboard_data_combined{suffix}.json")
    out_path = args.out or os.path.join(HERE, "..", "dashboard", "preview", f"dashboard_{args.year}.html")

    with open(in_path, encoding="utf-8") as f:
        combined = json.load(f)
    data = build(combined, args.year)
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = f.read()
    if "__DASHBOARD_DATA__" not in html:
        raise SystemExit("template is missing the __DASHBOARD_DATA__ placeholder")
    html = html.replace("__DASHBOARD_DATA__", payload)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print(f"Wrote {os.path.abspath(out_path)} ({len(html) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
