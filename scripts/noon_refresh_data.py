# -*- coding: utf-8 -*-
"""
NeuroNext Noon.com Performance Dashboard - data refresh pipeline.

Pulls fresh Noon Partner API data (finance/transaction export + FBN inventory
ageing export) for the current calendar-year-to-date window, aggregates it,
and writes noon_data.json with every number the dashboard's Noon section
needs - mirroring the methodology already used for Amazon in
dashboard_refresh_data.py wherever the two platforms' data is comparable.

Auth: Noon's service-account flow signs an RS256 JWT with the private key
from credentials/noon_app.json and POSTs it to /identity/public/v1/api/login,
which returns a session cookie (not a bearer token) used for every
subsequent request. This requires real RSA signing, which the Amazon script
avoids needing (LWA is a plain bearer refresh-token flow) - PyJWT/requests
are used here rather than hand-rolling RS256 via stdlib, same tradeoff this
project already made for Google Sheets (gspread, see dashboard_refresh_data.py).

Revenue/fee source: the finance "transactionviewreportonitemlevel" export,
item-level (one row per unit - confirmed 2026-10-03, no row has qty>1).
"order" rows are real sales; "order_update" rows are post-sale fee
adjustments (returns-related fulfillment re-charges, no revenue) that are
NOT separate units but DO affect the cost_breakdown/other fees; "statement_fee"
rows carry non-order charges (Advertising Fee is Noon's ad spend, real and
material - confirmed 2026-10-03, AED 5,020 YTD); "payment"/"balance_transfer"
rows are bank-settlement bookkeeping, not P&L.

COGS: reuses Amazon's landed-cost figures (same physical SKUs, same NEXTWAVE
invoices) via NOON_SKU_MAP below, which bridges Noon's own opaque `sku` /
seller-assigned `partner_sku` codes to the Amazon SKU each physical product
already has a real landed cost under. Confirmed with the user 2026-10-03
(all 7 SKUs with real Noon 2026 sales are mapped; see dashboard_refresh_data.py's
comments for the AEROSC-6 5.7L vs TJ-FDH7-PNAI 6L panoramic distinction,
which the user explicitly re-confirmed), plus S12ASBPV1DB (confirmed
2026-10-04, S12A Double) added for 2025 coverage. Any future Noon SKU with
sales but no mapping falls back to the same 40%-of-net-revenue placeholder
Amazon uses, flagged is_estimated=true.

**Real bug, fixed 2026-10-04**: NOON_SKU_MAP used to carry a HARDCODED landed
cost per SKU, computed once from PRODUCT_COST_USD's flat formula. That's
wrong whenever a SKU's cost gets revised by a later inventory_cost_ledger.json
batch (e.g. the 04-Sep-2026 UK shipment) - Amazon's own dashboard already
reflects the revised running WAC, so the hardcoded Noon figure silently went
stale and diverged from Amazon's, for 6 of the 7 mapped SKUs (only
TJ-FDH7-PNAI, which has no UK batch, happened to still match - found by
comparing against dashboard_data.json's live cost_table). COGS/net-profit
were wrong by as much as 47% per unit (RH1008BSZ: 81.46 hardcoded vs 119.55
live). Fixed by dropping landed_cost_aed from NOON_SKU_MAP entirely - it now
only maps partner_sku -> amazon_sku, and load_landed_costs() below reads the
CURRENT value straight from dashboard_data.json's cost_table at refresh time,
same source of truth the Amazon tab and the Overall/Noon cost tables already
use. This requires dashboard_data.json to exist and be current when this
script runs - true in the real pipeline (step 1 writes it, step 4.5 runs
this script after), but if you ever run this standalone, refresh Amazon's
data first or costs will be stale.

FBN-held stock (NOT the dashboard's "Inventory Ageing" concept): Noon's
fbn_inventoryv2_aging export reports stock sitting at Noon's own fulfillment
centers, bucketed by age - the Noon-side counterpart to Amazon's `az_inv`,
not a substitute for the existing warehouse ageing tab. The user corrected
this explicitly 2026-10-03: the dashboard's real "Inventory Ageing" is
already channel-agnostic (simulate_fifo_ageing() in dashboard_refresh_data.py
runs off the shared "Neuronext SOH & Outbound" warehouse sheet's own
inward/outbound rows, "to Amazon or another channel" per its own docstring,
NOT derived from either channel's sales API) - so it already covers Noon-
bound outbound movements and needs no Noon-specific equivalent here. This
export's buckets (kept in Noon's own 0-30/31-60/61-90/91-180/181-365/366+
boundaries, not force-mapped onto Amazon's 0-90/90-120/... scheme, since
that mapping isn't real) describe a different, genuinely Noon-specific fact
and are exposed as `fbn_held_stock` below, not `inventory_ageing`. The export
is a full historical snapshot (one row per SKU per month-end since the SKU
started being stocked) - only the latest snapshot_date per SKU is used.

Run standalone: python noon_refresh_data.py
Output: noon_data.json in this same folder.
"""
import json
import time
import uuid
import csv
import io
import os
import datetime
from collections import defaultdict

import requests
import jwt

NOON_CRED_PATH = os.environ.get(
    "NOON_CRED_PATH", r"C:\Users\akhil\OneDrive\Desktop\NeuroNext_Integration\credentials\noon_app.json")
BASE_URL = "https://noon-api-gateway.noon.partners"
USER_AGENT = "NeuroNextDashboard/1.0"
OUT_PATH = os.environ.get(
    "NOON_DATA_OUT", r"C:\Users\akhil\OneDrive\Desktop\NeuroNext_Integration\scripts\noon_data.json")
AMZ_DATA_PATH = os.environ.get(
    "DASHBOARD_DATA_OUT", r"C:\Users\akhil\OneDrive\Desktop\NeuroNext_Integration\scripts\dashboard_data.json")

# Bridges Noon's own `sku` / seller `partner_sku` codes to the Amazon SKU each physical
# product is already tracked under - landed cost is looked up LIVE from
# dashboard_data.json (see load_landed_costs() below), never hardcoded here, so it can
# never drift out of sync with Amazon's own WAC revisions (real bug, fixed 2026-10-04 -
# see module docstring). Confirmed with the user 2026-10-03/-04, including the AEROSC-6
# (5.7L) vs TJ-FDH7-PNAI (6L panoramic) distinction and S12ASBPV1DB/S12DBPV1DB/
# S12ADBPV1DB all = S12A Double - three different Noon partner_sku codes for the SAME
# physical product (not a guess from title text, which is unreliable here: S12ASBPV1DB's
# own title says "Single" despite being Double - confirmed by the user each time).
NOON_SKU_MAP = {
    "KDF-681D":  "TJ-FDH7-PNAI",
    "RH1008":    "RH1008BSZ",
    "F-901":     "62-0VFC-64SJ",
    "AEROSC-6":  "AEROSC-6",
    "RH228":     "RH228DBPZ",
    "RH188":     "RH188SBPZ",
    "S39DBPDB":  "S39DBPDB",
    "S12ASBPV1DB": "NR-O1LQ-E1UK",  # no 2026 sales yet, real in 2025 data
    "S12DBPV1DB":  "NR-O1LQ-E1UK",  # 2025-only, same S12A Double product as the above - confirmed by the user, NOT inferred from its "Single" title
    "S12ADBPV1DB": "NR-O1LQ-E1UK",  # 2025-only, same S12A Double product as the above - confirmed by the user
    "RH218":     "RH218SBPZ",       # no 2026 sales yet, real in 2025 data - same direct RH-series numbering as RH228/RH188/RH1008, high confidence
}
COGS_ESTIMATE_PCT_OF_NET_REVENUE = 0.40  # same placeholder Amazon uses, for an unmapped SKU


def load_landed_costs(path=AMZ_DATA_PATH):
    """Live landed-cost-per-unit (AED) by Amazon SKU, read from the Amazon pipeline's
    own output - the SAME figures the Amazon tab and the Overall/Noon cost tables show,
    including any running-WAC revision (e.g. a UK batch). Never hardcode these - see the
    module docstring for the real bug this caused when they were hardcoded."""
    with open(path, encoding="utf-8") as f:
        amz = json.load(f)
    return {r["sku"]: r["landed_cost"] for r in amz["cost_table"] if r["landed_cost"] is not None}


def _normalize_sku(s):
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


# The finance export's "Partner SKUs" field matches NOON_SKU_MAP's keys exactly
# (e.g. "F-901"), but the fbn_inventoryv2_aging export's "partner_sku" field strips
# punctuation from the same seller-assigned code (e.g. "F901") - confirmed 2026-10-03,
# a real inconsistency in Noon's own platform between the two export types, not a bug
# here. Ageing-table lookups go through this normalized map instead.
NOON_SKU_MAP_NORMALIZED = {_normalize_sku(k): v for k, v in NOON_SKU_MAP.items()}


def get_session():
    with open(NOON_CRED_PATH, "r", encoding="utf-8") as f:
        creds = json.load(f)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    token = jwt.encode(
        {"sub": creds["key_id"], "iat": int(time.time()), "jti": str(uuid.uuid4())},
        creds["private_key"], algorithm="RS256",
    )
    resp = session.post(f"{BASE_URL}/identity/public/v1/api/login",
                         json={"token": token, "default_project_code": creds["project_code"]})
    if resp.status_code != 200:
        raise RuntimeError(f"Noon login failed: {resp.status_code} {resp.text[:500]}")
    return session


def run_export(session, export_category_code, fields, timeout_polls=60):
    r = session.post(f"{BASE_URL}/impex/v1/export/create",
                      json={"export_category_code": export_category_code, "params": {"fields": fields}})
    if r.status_code != 200:
        raise RuntimeError(f"export create failed for {export_category_code}: {r.status_code} {r.text[:500]}")
    export_code = r.json()["export_code"]
    sj = {}
    for _ in range(timeout_polls):
        time.sleep(3)
        s = session.post(f"{BASE_URL}/impex/v1/export/status", json={"export_code": export_code})
        sj = s.json()
        if sj.get("export_status") in ("COMPLETE", "ERROR"):
            break
    if sj.get("export_status") != "COMPLETE":
        raise RuntimeError(f"export {export_category_code} did not complete: {sj}")
    dl = requests.get(sj["download_url"])
    dl.raise_for_status()
    return dl.text


def fetch_finance_rows(session, from_date, to_date):
    csv_text = run_export(session, "noon_financeweb_transactionviewreportonitemlevel",
                           {"from_date": from_date, "to_date": to_date})
    return list(csv.DictReader(io.StringIO(csv_text)))


def fetch_ageing_rows(session):
    csv_text = run_export(session, "fbn_inventoryv2_aging", {})
    return list(csv.DictReader(io.StringIO(csv_text)))


def f(row, key):
    v = (row.get(key) or "0").strip()
    return float(v) if v else 0.0


def build_noon_data(finance_rows, ageing_rows, as_of=None, landed_costs=None):
    as_of = as_of or datetime.datetime.utcnow()
    year = as_of.year
    year_start = datetime.datetime(year, 1, 1)
    days_elapsed = max(1, (as_of - year_start).days + 1)
    landed_costs = landed_costs if landed_costs is not None else load_landed_costs()

    order_rows = [r for r in finance_rows if r["Transaction Type"] == "order"
                  and r.get("Transaction Date", "").startswith(str(year))]
    fee_adjust_rows = [r for r in finance_rows if r["Transaction Type"] in ("order", "order_update")
                       and r.get("Transaction Date", "").startswith(str(year))]
    ad_fee_rows = [r for r in finance_rows if r["Transaction Type"] == "statement_fee"
                   and r.get("Title") == "Advertising Fee" and r.get("Transaction Date", "").startswith(str(year))]

    months = sorted(set(r["Transaction Date"][:7] for r in order_rows))

    def cost_for(partner_sku, asp):
        amazon_sku = NOON_SKU_MAP.get(partner_sku)
        cost = landed_costs.get(amazon_sku) if amazon_sku else None
        if cost is not None:
            return cost, False
        return (COGS_ESTIMATE_PCT_OF_NET_REVENUE * asp if asp else 0.0), True

    def month_key(r):
        return r["Transaction Date"][:7]

    monthly = []
    for month in months:
        m_orders = [r for r in order_rows if month_key(r) == month]
        m_adj = [r for r in fee_adjust_rows if month_key(r) == month]
        m_ads = [r for r in ad_fee_rows if month_key(r) == month]

        gross_revenue = sum(f(r, "Net Proceeds") for r in m_orders)
        referral_fee = sum(f(r, "Referral Fee including VAT") for r in m_adj)
        fulfillment_fee = sum(f(r, "Fullfilment & Logistics Fees including VAT") for r in m_adj)
        shipping_credits = sum(f(r, "Shipping Credits including VAT") for r in m_adj)
        other_order_fees = sum(f(r, "Other Order Fees including VAT") for r in m_adj)
        order_subsidies = sum(f(r, "Order Subsidies including VAT") for r in m_adj)
        non_order_subsidies = sum(f(r, "Non-Order Subsidies including VAT") for r in m_adj)
        others = sum(f(r, "Others including VAT") for r in m_adj)
        ad_spend = sum(f(r, "Total") for r in m_ads)  # Advertising Fee rows are already negative

        units = len(m_orders)
        orders = len(set(r["Order Nr"] for r in m_orders))
        asp = gross_revenue / units if units else 0.0

        cogs = 0.0
        any_estimated = False
        for r in m_orders:
            psku = r.get("Partner SKUs", "")
            c, is_est = cost_for(psku, asp)
            cogs += c
            any_estimated = any_estimated or is_est

        net_before_cogs_and_ads = (gross_revenue + referral_fee + fulfillment_fee + shipping_credits
                                    + other_order_fees + order_subsidies + non_order_subsidies + others)
        net_profit = net_before_cogs_and_ads + ad_spend - cogs

        monthly.append({
            "month": month,
            "gross_revenue": round(gross_revenue, 2),
            "net_before_fees": round(net_before_cogs_and_ads, 2),
            "ad_spend": round(ad_spend, 2),
            "cogs": round(cogs, 2),
            "cogs_is_estimated": any_estimated,
            "net_profit": round(net_profit, 2),
            "orders": orders,
            "units": units,
            "asp": round(asp, 2),
            "cost_breakdown": {
                "referral": round(referral_fee, 2),
                "fulfillment": round(fulfillment_fee, 2),
                "shipping_credits": round(shipping_credits, 2),
                "other_order_fees": round(other_order_fees, 2),
                "order_subsidies": round(order_subsidies, 2),
                "non_order_subsidies": round(non_order_subsidies, 2),
                "others": round(others, 2),
            },
        })

    other_fees_total = sum(m["net_before_fees"] - m["gross_revenue"] for m in monthly)
    ytd = {
        "gross_revenue": round(sum(m["gross_revenue"] for m in monthly), 2),
        "ad_spend": round(sum(m["ad_spend"] for m in monthly), 2),
        "other_fees": round(other_fees_total, 2),
        "cogs": round(sum(m["cogs"] for m in monthly), 2),
        "net_profit": round(sum(m["net_profit"] for m in monthly), 2),
        "orders": sum(m["orders"] for m in monthly),
        "units": sum(m["units"] for m in monthly),
        "days_elapsed": days_elapsed,
    }
    # Net proceeds (added for the waterfall chart): gross revenue through fees + ad
    # spend, EXCLUDING COGS - same "Where the revenue goes" definition the Amazon tab's
    # waterfall uses (its own net_proceeds KPI explicitly excludes COGS too).
    ytd["net_proceeds"] = round(ytd["gross_revenue"] + other_fees_total + ytd["ad_spend"], 2)
    ytd["revenue_per_day"] = round(ytd["gross_revenue"] / days_elapsed, 2)
    ytd["units_per_day"] = round(ytd["units"] / days_elapsed, 3)

    # Per-SKU rollup (partner_sku is the stable seller-assigned key; Noon's own `sku`
    # can differ in format run to run for the same listing, partner_sku does not).
    # Tracks per-month revenue/units/fees/cogs (not just YTD totals) so the dashboard's
    # Product-wise Profitability table can be period-filtered (MTD/YTD/month), same as
    # the Amazon tab's equivalent table - added 2026-10-04 (previous version only had
    # YTD totals, which meant the table couldn't support a period filter at all).
    sku_months = defaultdict(lambda: defaultdict(lambda: {"units": 0, "gross_revenue": 0.0}))
    sku_totals = defaultdict(lambda: {"units": 0, "gross_revenue": 0.0, "title": ""})
    for r in order_rows:
        psku = r.get("Partner SKUs", "")
        month = month_key(r)
        rev = f(r, "Net Proceeds")
        sku_months[psku][month]["units"] += 1
        sku_months[psku][month]["gross_revenue"] += rev
        sku_totals[psku]["units"] += 1
        sku_totals[psku]["gross_revenue"] += rev
        sku_totals[psku]["title"] = r.get("Title", "")

    sku_fee_totals = defaultdict(lambda: {"referral": 0.0, "fulfillment": 0.0})
    sku_month_fees = defaultdict(lambda: defaultdict(lambda: {"referral": 0.0, "fulfillment": 0.0}))
    for r in fee_adjust_rows:
        psku = r.get("Partner SKUs", "")
        month = month_key(r)
        referral = f(r, "Referral Fee including VAT")
        fulfillment = f(r, "Fullfilment & Logistics Fees including VAT")
        sku_fee_totals[psku]["referral"] += referral
        sku_fee_totals[psku]["fulfillment"] += fulfillment
        sku_month_fees[psku][month]["referral"] += referral
        sku_month_fees[psku][month]["fulfillment"] += fulfillment

    ad_spend_by_month = {m["month"]: m["ad_spend"] for m in monthly}
    revenue_by_month = {m["month"]: m["gross_revenue"] for m in monthly}

    def month_detail(psku, month, mv):
        units = mv["units"]
        gross_revenue = mv["gross_revenue"]
        asp = gross_revenue / units if units else 0.0
        c, is_est = cost_for(psku, asp)
        cogs = c * units
        fees = sku_month_fees[psku][month]
        # Ad spend allocated pro-rata by this SKU-month's share of that month's gross
        # revenue - same methodology the Amazon tab already uses for its own ad_alloc
        # column (Noon's finance export also only reports ad spend in aggregate, not
        # per SKU).
        month_rev = revenue_by_month.get(month, 0.0)
        ad_alloc = (ad_spend_by_month.get(month, 0.0) * gross_revenue / month_rev) if month_rev else 0.0
        net_profit = gross_revenue + fees["referral"] + fees["fulfillment"] + ad_alloc - cogs
        return {
            "units": units, "gross_revenue": round(gross_revenue, 2), "asp": round(asp, 2),
            "cost_per_unit": round(c, 2), "cogs": round(cogs, 2), "cogs_is_estimated": is_est,
            "referral_fee": round(fees["referral"], 2), "fulfillment_fee": round(fees["fulfillment"], 2),
            "fees": round(fees["referral"] + fees["fulfillment"], 2),  # single combined column, same role as Amazon's "amazon_fees"
            "ad_spend_allocated": round(ad_alloc, 2),
            "gross_margin": round(gross_revenue - cogs, 2),
            "margin_pct": round((gross_revenue - cogs) / gross_revenue * 100, 1) if gross_revenue else None,
            "net_profit": round(net_profit, 2),
            "net_margin_pct": round(net_profit / gross_revenue * 100, 1) if gross_revenue else None,
        }

    sku_rows = []
    for psku, tot in sku_totals.items():
        mapping = NOON_SKU_MAP.get(psku)
        months_detail = {month: month_detail(psku, month, v) for month, v in sku_months[psku].items()}
        # YTD is the sum of the per-month details (NOT a fresh month_detail() call with
        # month=None - fees/ad-alloc are tracked per month, so YTD must aggregate across
        # them, same principle as the top-level monthly/ytd split above).
        units = tot["units"]
        gross_revenue = tot["gross_revenue"]
        asp = gross_revenue / units if units else 0.0
        c, is_est = cost_for(psku, asp)
        cogs = sum(md["cogs"] for md in months_detail.values())
        referral = sum(md["referral_fee"] for md in months_detail.values())
        fulfillment = sum(md["fulfillment_fee"] for md in months_detail.values())
        ad_alloc = sum(md["ad_spend_allocated"] for md in months_detail.values())
        gross_margin = gross_revenue - cogs
        net_profit = gross_revenue + referral + fulfillment + ad_alloc - cogs
        sku_rows.append({
            "partner_sku": psku,
            "amazon_sku": mapping,
            "title": tot["title"],
            "units": units, "gross_revenue": round(gross_revenue, 2), "asp": round(asp, 2),
            "cost_per_unit": round(c, 2), "cogs": round(cogs, 2), "cogs_is_estimated": is_est,
            "referral_fee": round(referral, 2), "fulfillment_fee": round(fulfillment, 2),
            "fees": round(referral + fulfillment, 2),
            "ad_spend_allocated": round(ad_alloc, 2),
            "gross_margin": round(gross_margin, 2),
            "margin_pct": round(gross_margin / gross_revenue * 100, 1) if gross_revenue else None,
            "net_profit": round(net_profit, 2),
            "net_margin_pct": round(net_profit / gross_revenue * 100, 1) if gross_revenue else None,
            "months": months_detail,
        })
    sku_rows.sort(key=lambda r: -r["gross_revenue"])

    # FBN-held stock (stock physically sitting at Noon's fulfillment centers) - NOT
    # the dashboard's "Inventory Ageing" concept, see module docstring. Latest
    # snapshot_date per (sku, condition).
    latest_by_key = {}
    for r in ageing_rows:
        key = (r["sku"], r.get("inventory_condition", ""))
        if key not in latest_by_key or r["snapshot_date"] > latest_by_key[key]["snapshot_date"]:
            latest_by_key[key] = r

    stock_table = []
    stock_totals = {"gross_quantity": 0, "buckets": defaultdict(int)}
    bucket_fields = [
        ("0-30", "quantity_0_30d"), ("31-60", "quantity_31_60d"), ("61-90", "quantity_61_90d"),
        ("91-180", "quantity_91_180d"), ("181-365", "quantity_181_365d"), ("366+", "quantity_366d"),
    ]
    for r in latest_by_key.values():
        gross_qty = int(float(r.get("gross_quantity") or 0))
        mapping = NOON_SKU_MAP_NORMALIZED.get(_normalize_sku(r.get("partner_sku", "")))
        row = {
            "sku": r["sku"],
            "partner_sku": r.get("partner_sku", ""),
            "amazon_sku": mapping,
            "condition": r.get("inventory_condition", ""),
            "snapshot_date": r["snapshot_date"],
            "gross_quantity": gross_qty,
            "buckets": {},
        }
        for label, field in bucket_fields:
            qty = int(float(r.get(field) or 0))
            row["buckets"][label] = qty
            stock_totals["buckets"][label] += qty
        stock_totals["gross_quantity"] += gross_qty
        stock_table.append(row)
    stock_table.sort(key=lambda r: -r["gross_quantity"])
    stock_totals["buckets"] = dict(stock_totals["buckets"])

    return {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "year": year,
        "as_of": as_of.isoformat() + "Z",
        "months": months,
        "monthly": monthly,
        "ytd": ytd,
        "sku_rows": sku_rows,
        "sku_map": NOON_SKU_MAP,
        "fbn_held_stock": stock_table,
        "fbn_held_stock_totals": stock_totals,
    }


def main():
    # --year YYYY (added 2026-10-04, mirrors dashboard_refresh_data.py's own --year flag):
    # pulls a closed past year (Jan 1 - Dec 31) into noon_data_YYYY.json for the static
    # per-year page (e.g. 2025.html). No flag = current year to date into OUT_PATH.
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int)
    args = ap.parse_args()

    real_now = datetime.datetime.utcnow()
    out_path = OUT_PATH
    amz_path = AMZ_DATA_PATH
    if args.year and args.year < real_now.year:
        year = args.year
        to_date = f"{year}-12-31"
        as_of = datetime.datetime(year, 12, 31, 23, 59, 59)
        out_path = os.path.join(os.path.dirname(OUT_PATH), f"noon_data_{year}.json")
        amz_path = os.path.join(os.path.dirname(AMZ_DATA_PATH), f"dashboard_data_{year}.json")
    else:
        year = real_now.year
        to_date = real_now.strftime("%Y-%m-%d")
        as_of = real_now

    session = get_session()
    finance_rows = fetch_finance_rows(session, f"{year}-01-01", to_date)
    ageing_rows = fetch_ageing_rows(session)
    landed_costs = load_landed_costs(amz_path)
    data = build_noon_data(finance_rows, ageing_rows, as_of=as_of, landed_costs=landed_costs)
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, indent=2)
    print(f"Wrote {out_path}: {len(data['sku_rows'])} SKUs, {len(data['months'])} months, "
          f"YTD gross_revenue={data['ytd']['gross_revenue']}, YTD net_profit={data['ytd']['net_profit']}")


if __name__ == "__main__":
    main()
