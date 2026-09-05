# -*- coding: utf-8 -*-
"""
NeuroNext Amazon Performance Dashboard - data refresh pipeline.

Pulls fresh Amazon SP-API data (Orders, Finances, FBA Inventory) for the
current calendar-year-to-date window, aggregates it, and writes a single
dashboard_data.json with every number the dashboard HTML needs, already
computed using the same methodology validated in the 2026-08-14 build:

- Revenue/refunds/fees/ad spend from the Finances API (settlement-level).
- COGS from PRODUCT_COST_USD, sourced 2026-08-15 directly from real NEXTWAVE
  proforma invoices/SOA (14 SKUs, all of them - no estimated SKUs remain as
  of the RH218SBPZ reinstatement later that day). Any SKU that shows up in
  sales with no entry in PRODUCT_COST_USD still falls back to a 40%-of-net-
  revenue placeholder, flagged with is_estimated=true (do NOT silently treat
  as real) - this is now a safety net, not an active case.
- FBA storage/inbound fees have no per-event date in the API response, so
  they're prorated evenly across the months in range (flagged as such).
- Per-SKU "Amazon fees" use real per-shipment-item fee data (SellerSKU is
  present on each shipment/refund item) - not an allocation.
- Per-SKU ad spend IS an allocation (Amazon's Finances API only reports ad
  spend in aggregate, not per SKU) - pro-rata by each SKU's share of GROSS
  revenue. Flagged as allocated, not real.
- Revenue shown next to any refund-adjusted profit figure is NET of refunds
  - never show gross bookings beside a refund-adjusted profit number (this
  produced a real bug on 2026-08-14: RH218SBPZ showed "revenue 99" next to
  a profit calc that used net revenue 0, since the unit was fully refunded).
- Note: per-SKU "amazon fees" only include commission + fulfillment fees
  (tied to specific shipment/refund items via SellerSKU). Chargebacks,
  shipping/payment charges, refund fee credits, adjustments, and storage
  are company-level costs with no SKU attribution, so they live only in
  the monthly/YTD "other_costs" aggregate and cost_breakdown_ytd - the
  per-SKU net_profit column will NOT sum to ytd.net_profit, by design
  (added 2026-08-15 after this tripped a sanity check during a refresh).
- cost_breakdown_ytd in the output gives the YTD total of each "other
  costs" component (commission, fulfillment, chargebacks, shipcharges,
  refund_credits, adjustments, storage), for the dashboard's "Other costs
  breakdown" detail table (added 2026-08-15 - previously not exported).
- Refund COGS treatment (added 2026-08-15, via the FBA Customer Returns
  report): a refund alone does NOT reverse COGS - only a SELLABLE-disposition
  physical return does (the unit goes back to stock, so it was never really
  consumed). DEFECTIVE/CUSTOMER_DAMAGED/etc. returns keep their COGS charged
  in full, same as a refund with no return at all. See parse_returns() and
  returns_breakdown in the output for the sellable/non-sellable split per SKU.
  If the returns report fails to fetch (rare - Amazon can be slow to generate
  it), fetch_returns_report() returns [] and COGS silently falls back to the
  pre-2026-08-15 behavior (no reversal) rather than failing the whole refresh.

Run standalone: python dashboard_refresh_data.py
Output: dashboard_data.json in this same folder.

To extend the landed-cost map (LANDED_COST below) with a new SKU or to fix
the 40%-estimate placeholders, edit the constants near the top of this file.
"""
import json
import time
import gzip
import csv
import io
import urllib.request
import urllib.parse
import urllib.error
import datetime
from collections import defaultdict

import os
# Env-var overrides let the same script run unattended in GitHub Actions (which has no
# access to the local C:\ paths below) - each defaults to today's local path, so the
# local scheduled task keeps working exactly as before with no env vars set at all.
AMZ_CRED_PATH = os.environ.get("AMZ_CRED_PATH", r"C:\Users\akhil\OneDrive\Desktop\NeuroNext_Integration\credentials\amazon_app.json")
MARKETPLACE_ID = "A2VIGQ35RCS4UG"
GSHEET_CRED_PATH = os.environ.get("GSHEET_CRED_PATH", r"C:\Users\akhil\OneDrive\Desktop\NeuroNext_Integration\credentials\google_sheets_service_account.json")
GSHEET_ID = "1wEWshv9dW8yGgEvWgYcZv-JmPlRO9bfj6hMe-65qbR8"
BASE = "https://sellingpartnerapi-eu.amazon.com"
OUT_PATH = os.environ.get("DASHBOARD_DATA_OUT", r"C:\Users\akhil\OneDrive\Desktop\NeuroNext_Integration\scripts\dashboard_data.json")

# Product cost per unit, re-sourced 2026-08-15 directly from the real NEXTWAVE proforma
# invoices/SOA in "Munaffa SOA & PI.zip" (Air Fryer PI, Zeimetsu/RH-series PI, Dearbaby
# breast pump PI x2 batches), replacing the earlier "SKU Costing - Full" sheet figures -
# that sheet had the 9L Air Fryer overstated by ~38% (see below). AED_USD_PEG is the
# fixed UAE dirham/dollar peg used only to convert the final landed cost to AED (never
# floats). Landed cost = product cost (USD) -> +10% freight -> +5% duty on CIF -> +5%
# import VAT on CIF+duty, all in USD, converted to AED only at the very end.
# RH218SBPZ is back IN this real-cost map as of 2026-08-15 (user reversed the earlier
# exclusion - "since you have real cost please use that"). No SKU is estimated anymore.
AED_USD_PEG = 3.6725
FREIGHT_PCT, DUTY_PCT, VAT_PCT = 0.10, 0.05, 0.05

# Air Fryer PI/SOA: 2 SKUs shipped with FOC ("free of charge") sample units bundled into
# the same paid batch - true per-unit cost is total batch $ / total qty INCLUDING the
# FOC units (per explicit user instruction), not the nominal per-piece invoice rate.
# 62-0VFC-64SJ (9L): (390u incl. 7 FOC @ $12,830.50) + (773u incl. 14 FOC @ $25,426.50)
#   = $38,257.00 / 1,163u = $32.8951 - this REPLACES the old $45.29 figure, which was
#   ~38% too high (traced to the old "SKU Costing" sheet, not this invoice).
# VG-HNBC-EKIS (10L, "KDF-5521DTW wifi"): 390u incl. 7 FOC @ $15,128.50 = $38.791.
# TJ-FDH7-PNAI (6L, "KDF-681DW 6.7L") and AEROSC-6 (5.7L, "BIYI AF-600C") had no FOC
# units - straight total-$/qty from the same invoice.
#
# Zeimetsu PI (RH-series): no FOC units, straight $/qty - RH188SBPZ ($2.48) already
# matched the old figure exactly; RH218SBPZ ($7.15), RH228DBPZ ($8.53) and RH1008BSZ
# ($18.29) are all real costs now on file (RH228/RH1008 have no 2026 sales yet, but
# now have a real cost on file instead of none).
#
# Dearbaby PI (S39/S12A/S12 breast pump families): two batches exist, 2024-06-11 and
# 2025-08-01, no FOC units in either. User confirmed (2026-08-15): use a QTY-WEIGHTED
# BLEND of both batches (total $ across both batches / total qty across both batches),
# not just one batch, since both are assumed to be in current mixed inventory. The 2024
# batch includes a $0.50/pump-piece customization-logo charge (a double-pump unit uses
# 2 pieces = $1.00/set); the 2025 batch's logo was free. 7J-5E02-AAAN (S39 Bluetooth
# Double) only exists in the 2025 batch, so no blending needed for it.
PRODUCT_COST_USD = {
    "62-0VFC-64SJ": (12830.50 + 25426.50) / (390 + 773),
    "VG-HNBC-EKIS": 15128.50 / 390,
    "TJ-FDH7-PNAI": 11352.00 / 528,
    "AEROSC-6": 19175.00 / 570,
    "RH228DBPZ": 818.88 / 96,
    "RH188SBPZ": 446.40 / 180,
    "RH1008BSZ": 2304.54 / 126,
    "PY-51U9-NDO7": ((10482.48 + 1653.24 + 552 * 0.5) + (13312.80 + 2156.40)) / (276 + 360),
    "LN-WRP7-RIMK": ((3798.00 + 598.00 + 200 * 0.5) + (4437.60 + 717.60)) / (200 + 240),
    "NR-O1LQ-E1UK": ((5396.40 + 1078.20 + 360 * 0.5) + (335.76 + 71.88)) / (180 + 12),
    "D4-2UK7-AM2Z": ((2398.40 + 478.40 + 160 * 0.5) + (1399.00 + 299.00)) / (160 + 100),
    "S39DBPDB": ((4856.76 + 324 * 0.5) + 5036.40) / (162 + 180),
    "2D-JIOC-DYZ2": ((7554.96 + 504 * 0.5) + 335.76) / (504 + 24),
    "7J-5E02-AAAN": (1775.04 + 288.00 + 287.52) / 48,
    "RH218SBPZ": 1029.60 / 144,
}
PRODUCT_COST_AED = {sku: usd * AED_USD_PEG for sku, usd in PRODUCT_COST_USD.items()}


def cost_components_usd(product_cost_usd):
    """Freight/duty/VAT computed entirely in USD (the source currency) - only the
    final landed cost is converted to AED, at the very end, via the fixed peg."""
    freight = product_cost_usd * FREIGHT_PCT
    cif = product_cost_usd + freight
    duty = cif * DUTY_PCT
    vat = (cif + duty) * VAT_PCT
    landed_usd = cif + duty + vat
    return {
        "product_cost_usd": product_cost_usd, "freight_usd": freight, "duty_usd": duty, "vat_usd": vat,
        "landed_cost_usd": landed_usd, "landed_cost_aed": landed_usd * AED_USD_PEG,
    }


LANDED_COST = {sku: cost_components_usd(usd)["landed_cost_aed"] for sku, usd in PRODUCT_COST_USD.items()}
COGS_ESTIMATE_PCT_OF_NET_REVENUE = 0.40  # placeholder for SKUs not in LANDED_COST

# Warehouse inventory from the "Neuronext SOH & Outbound" Google Sheet, 'Current Summary'
# tab, "Remaining" row per Model block - pulled 2026-08-15. The tracker has 14 Model rows
# but Amazon has 20 SKU codes because several Models have duplicate/legacy Amazon listings.
# Where a Model maps to >1 SKU, the Remaining qty is assigned to whichever SKU has real
# 2026 YTD sales/current FBA stock (confirmed with the user); the dead duplicate gets 0.
# VG-HNBC-EKIS (10L air fryer) has no matching tracker row at all - left out entirely, so
# wh_inv resolves to None ("not tracked"), per explicit user instruction, not fabricated.
# Dead/legacy duplicate Amazon SKUs with no matching row in the tracker sheet at all -
# always 0, never looked up live (see MODEL_TO_SKU for the active SKU each duplicates).
WH_INV_DEAD_DUPLICATES = {
    "2Z-0A8G-EGU0": 0,   # dup of S12A Single (D4-2UK7-AM2Z)
    "HV-JE48-ORFB": 0,   # dup of S39 Double, non-app (PY-51U9-NDO7)
    "7Q-285B-KTIO": 0,   # dup of 5.7L Aerofry (AEROSC-6)
    "2X-VGKU-V16T": 0,   # dup of 6L Aerofry (TJ-FDH7-PNAI)
    "CG-7IMP-NEVU": 0,   # dup of S39 Double App-controlled (7J-5E02-AAAN)
}

# Maps the "Model" label used in the "Neuronext SOH & Outbound" Google Sheet's
# "Current Summary" tab to the active Amazon SKU it corresponds to (confirmed with
# the user 2026-08-15, re-verified 2026-09-04 when live-fetch replaced the hardcoded
# snapshot - the S12 Double model label mapping to a SKU literally named "S39DBPDB" is
# not a typo, that's genuinely how Amazon's own SKU code is misnamed for that listing).
MODEL_TO_SKU = {
    "RH228": "RH228DBPZ",
    "RH218": "RH218SBPZ",
    "RH1008": "RH1008BSZ",
    "RH188": "RH188SBPZ",
    "S12A Single": "D4-2UK7-AM2Z",
    "S12A Double": "NR-O1LQ-E1UK",
    "S39 Single": "LN-WRP7-RIMK",
    "S39 Double": "PY-51U9-NDO7",
    "S12 Single": "2D-JIOC-DYZ2",
    "S12 Double": "S39DBPDB",
    "9L": "62-0VFC-64SJ",
    "5.7L": "AEROSC-6",
    "6L": "TJ-FDH7-PNAI",
    "S39 Double App": "7J-5E02-AAAN",
}

# Last-known-good snapshot (2026-09-04, from the first live fetch) - used only if the
# live Google Sheets fetch fails (network issue, sheet moved, credentials revoked,
# etc.), so a transient failure doesn't take the Inventory tab's WH numbers to zero.
WH_INV_FALLBACK_SNAPSHOT = {
    "RH228DBPZ": 53, "RH218SBPZ": 31, "RH1008BSZ": 71, "RH188SBPZ": 105,
    "D4-2UK7-AM2Z": 122, "NR-O1LQ-E1UK": 115, "LN-WRP7-RIMK": 165,
    "PY-51U9-NDO7": 399, "2D-JIOC-DYZ2": 269, "S39DBPDB": 271,
    "62-0VFC-64SJ": 492, "AEROSC-6": 372, "TJ-FDH7-PNAI": 178, "7J-5E02-AAAN": 16,
}


def fetch_wh_inv_tracker():
    """Live-fetches warehouse stock from the 'Neuronext SOH & Outbound' Google Sheet's
    'Current Summary' tab via a service account, replacing the old hardcoded snapshot.
    The tab's layout is 14 fixed-width blocks of columns, one per model; row 0 holds
    each block's 'Remaining' qty one cell to the right of that label, and row 4 holds
    the 'Model' name the same way, at the same column offset - block order/positions
    are not assumed stable across sheet edits, so both are located by scanning for
    their label text each run, not by a hardcoded column index."""
    try:
        import gspread
        gc = gspread.service_account(filename=GSHEET_CRED_PATH)
        sh = gc.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Current Summary")
        rows = ws.get_all_values()
        row_remaining, row_model = rows[0], rows[4]
        remaining_idx = [i for i, v in enumerate(row_remaining) if v == "Remaining"]
        model_idx = [i for i, v in enumerate(row_model) if v == "Model"]
        tracker = dict(WH_INV_DEAD_DUPLICATES)
        for ri, mi in zip(remaining_idx, model_idx):
            model = row_model[mi + 1].strip()
            qty_str = row_remaining[ri + 1].strip()
            sku = MODEL_TO_SKU.get(model)
            if sku is None or not qty_str.isdigit():
                continue
            tracker[sku] = int(qty_str)
        missing = set(MODEL_TO_SKU.values()) - set(tracker)
        if missing:
            print(f"WARNING: WH inventory sheet fetch missing SKUs {missing}, "
                  f"falling back to last-known snapshot for those only")
            for sku in missing:
                tracker[sku] = WH_INV_FALLBACK_SNAPSHOT.get(sku, 0)
        return tracker
    except Exception as e:
        print(f"WARNING: WH inventory live sheet fetch failed ({e}), "
              f"using last-known snapshot (2026-09-04) instead")
        return dict(WH_INV_FALLBACK_SNAPSHOT, **WH_INV_DEAD_DUPLICATES)


WH_INV_TRACKER = fetch_wh_inv_tracker()


def get_amz_access_token():
    with open(AMZ_CRED_PATH) as f:
        creds = json.load(f)
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id": creds["lwa_client_id"],
        "client_secret": creds["lwa_client_secret"],
    }).encode()
    req = urllib.request.Request("https://api.amazon.com/auth/o2/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["access_token"]


def spapi_get(access_token, path, params, retries=6):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        req = urllib.request.Request(url, method="GET")
        req.add_header("x-amz-access-token", access_token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"{e.code}: {body}")
        except urllib.error.URLError:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("failed after retries")


def month_chunks(start, end):
    chunks = []
    cur = start
    while cur < end:
        nxt = min((cur.replace(day=1) + datetime.timedelta(days=32)).replace(day=1), end)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks


def fetch_orders(token, start, now):
    orders = []
    params = {"MarketplaceIds": MARKETPLACE_ID, "CreatedAfter": start.strftime("%Y-%m-%dT%H:%M:%SZ")}
    while True:
        result = spapi_get(token, "/orders/v0/orders", params)
        payload = result["payload"]
        orders.extend(payload["Orders"])
        next_token = payload.get("NextToken")
        print(f"orders so far: {len(orders)}")
        if not next_token:
            break
        params = {"NextToken": next_token}
        time.sleep(1)
    return orders


def fetch_finance_events(token, start, now):
    all_events = []
    for after, before in month_chunks(start, now - datetime.timedelta(minutes=10)):
        params = {
            "PostedAfter": after.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "PostedBefore": before.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "MaxResultsPerPage": "100",
        }
        page = 0
        while True:
            page += 1
            result = spapi_get(token, "/finances/v0/financialEvents", params)
            payload = result["payload"]
            all_events.append(payload["FinancialEvents"])
            next_token = payload.get("NextToken")
            print(f"finances {after.date()}..{before.date()} page {page}")
            if not next_token:
                break
            params = {"NextToken": next_token}
            time.sleep(1.5)
            if page > 100:
                break
        time.sleep(1.5)
    return all_events


def spapi_request(access_token, method, path, params=None, body=None):
    """Generic SP-API request (GET/POST) returning (status_code, parsed_or_raw_body).
    Used for the Reports API, which needs POST + polling, unlike the other GET-only
    endpoints above."""
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-amz-access-token", access_token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def fetch_returns_report(token, start, now):
    """Pulls the FBA Customer Returns report (real per-return disposition data -
    SELLABLE vs DEFECTIVE/CUSTOMER_DAMAGED/etc.) and returns a list of dict rows
    (return-date, sku, quantity, detailed-disposition, ...). Verified 2026-08-15:
    this report type does NOT need the Reports-API Seller-Central role that the
    Sessions/Buy-Box report needs (that one 403s) - this one works with current creds.
    Report generation can take ~1-2 minutes; polls up to 5 minutes before giving up."""
    status, resp = spapi_request(token, "POST", "/reports/2021-06-30/reports", body={
        "reportType": "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA",
        "marketplaceIds": [MARKETPLACE_ID],
        "dataStartTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataEndTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    if status != 202:
        print(f"returns report request failed ({status}): {resp} - skipping, no COGS reversal this run")
        return []
    report_id = resp["reportId"]
    report_doc_id = None
    for attempt in range(30):
        time.sleep(10)
        s, r = spapi_request(token, "GET", f"/reports/2021-06-30/reports/{report_id}")
        proc_status = r.get("processingStatus") if isinstance(r, dict) else None
        print(f"returns report poll {attempt}: {proc_status}")
        if proc_status == "DONE":
            report_doc_id = r.get("reportDocumentId")
            break
        if proc_status in ("CANCELLED", "FATAL"):
            print(f"returns report failed: {r} - skipping, no COGS reversal this run")
            return []
    if not report_doc_id:
        print("returns report did not finish in time - skipping, no COGS reversal this run")
        return []
    s, doc = spapi_request(token, "GET", f"/reports/2021-06-30/documents/{report_doc_id}")
    with urllib.request.urlopen(doc["url"]) as resp2:
        raw = resp2.read()
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return list(reader)


def fetch_inventory(token):
    all_summaries = []
    params = {
        "granularityType": "Marketplace", "granularityId": MARKETPLACE_ID,
        "marketplaceIds": MARKETPLACE_ID, "details": "true",
    }
    while True:
        result = spapi_get(token, "/fba/inventory/v1/summaries", params)
        payload = result["payload"]
        all_summaries.extend(payload.get("inventorySummaries", []))
        next_token = (result.get("pagination") or {}).get("nextToken")
        if not next_token:
            break
        params = {**params, "nextToken": next_token}
        time.sleep(1)
    return all_summaries


def aggregate(finance_pages):
    sku_month = defaultdict(lambda: defaultdict(lambda: {"revenue": 0.0, "units": 0, "fees": 0.0, "adjustments": 0.0}))
    sku_totals = defaultdict(lambda: {"revenue": 0.0, "units": 0, "fees": 0.0, "refunds": 0.0, "refund_fees": 0.0, "adjustments": 0.0})
    fee_month = defaultdict(lambda: defaultdict(float))
    month_totals = defaultdict(lambda: {"revenue": 0.0, "refunds": 0.0, "ad_spend": 0.0})
    month_refund_fee_credits = defaultdict(float)
    month_adjustments = defaultdict(float)
    refund_sku_month = defaultdict(lambda: defaultdict(float))
    refund_fees_sku_month = defaultdict(lambda: defaultdict(float))
    service_fee_total = 0.0
    all_orders_with_events = set()
    orders_by_month = defaultdict(set)  # unique order IDs per posted/settled month - matches units/revenue month bucketing

    for page in finance_pages:
        for shp in page.get("ShipmentEventList", []) or []:
            posted = (shp.get("PostedDate") or "")[:7]
            order_id = shp.get("AmazonOrderId")
            if order_id:
                all_orders_with_events.add(order_id)
                orders_by_month[posted].add(order_id)
            for item in shp.get("ShipmentItemList", []) or []:
                sku = item.get("SellerSKU") or "UNKNOWN"
                qty = item.get("QuantityShipped") or 0
                sku_month[sku][posted]["units"] += qty
                sku_totals[sku]["units"] += qty
                for chg in item.get("ItemChargeList", []) or []:
                    ctype = chg.get("ChargeType") or ""
                    val = (chg.get("ChargeAmount", {}) or {}).get("CurrencyAmount", 0) or 0
                    if ctype == "Principal":
                        month_totals[posted]["revenue"] += val
                        sku_month[sku][posted]["revenue"] += val
                        sku_totals[sku]["revenue"] += val
                    elif "Tax" not in ctype:
                        fee_month[posted][f"Charge:{ctype}"] += val
                        sku_totals[sku]["fees"] += val
                        sku_month[sku][posted]["fees"] += val
                for fee in item.get("ItemFeeList", []) or []:
                    val = (fee.get("FeeAmount", {}) or {}).get("CurrencyAmount", 0) or 0
                    fee_month[posted][fee.get("FeeType")] += val
                    sku_totals[sku]["fees"] += val
                    sku_month[sku][posted]["fees"] += val

        for ref in page.get("RefundEventList", []) or []:
            posted = (ref.get("PostedDate") or "")[:7]
            for item in ref.get("ShipmentItemAdjustmentList", []) or []:
                sku = item.get("SellerSKU") or "UNKNOWN"
                for chg in item.get("ItemChargeAdjustmentList", []) or []:
                    if chg.get("ChargeType") == "Principal":
                        val = chg.get("ChargeAmount", {}).get("CurrencyAmount", 0) or 0
                        month_totals[posted]["refunds"] += val
                        sku_totals[sku]["refunds"] += val
                        refund_sku_month[sku][posted] += val
                for fee in item.get("ItemFeeAdjustmentList", []) or []:
                    val = (fee.get("FeeAmount", {}) or {}).get("CurrencyAmount", 0) or 0
                    month_refund_fee_credits[posted] += val
                    sku_totals[sku]["refund_fees"] += val
                    refund_fees_sku_month[sku][posted] += val

        for sf in page.get("ServiceFeeEventList", []) or []:
            for fee in sf.get("FeeList", []) or []:
                service_fee_total += (fee.get("FeeAmount", {}) or {}).get("CurrencyAmount", 0) or 0

        for adj in page.get("AdjustmentEventList", []) or []:
            posted = (adj.get("PostedDate") or "")[:7]
            for item in adj.get("AdjustmentItemList", []) or []:
                val = (item.get("TotalAmount", {}) or {}).get("CurrencyAmount", 0) or 0
                month_adjustments[posted] += val
                # AdjustmentItemList DOES carry SellerSKU (verified 2026-08-15 against raw
                # payload) - real per-SKU data, not an allocation. Falls back to a company-
                # level bucket only if a future adjustment type omits SellerSKU.
                sku = item.get("SellerSKU")
                if sku:
                    sku_totals[sku]["adjustments"] += val
                    sku_month[sku][posted]["adjustments"] += val

        for ad in page.get("ProductAdsPaymentEventList", []) or []:
            posted = (ad.get("postedDate") or ad.get("PostedDate") or "")[:7]
            val = (ad.get("transactionValue", {}) or {}).get("CurrencyAmount", 0) or 0
            if posted:
                month_totals[posted]["ad_spend"] += val

    return {
        "sku_month": sku_month, "sku_totals": sku_totals, "fee_month": fee_month,
        "month_totals": month_totals, "month_refund_fee_credits": month_refund_fee_credits,
        "month_adjustments": month_adjustments, "refund_sku_month": refund_sku_month,
        "refund_fees_sku_month": refund_fees_sku_month,
        "service_fee_total": service_fee_total,
        "orders_with_events": len(all_orders_with_events),
        "orders_by_month": orders_by_month,
    }


def parse_returns(returns_rows):
    """Splits FBA return rows into sellable vs non-sellable per SKU (total and per
    return-month). 'Sellable' = disposition SELLABLE (unit goes back to resellable
    stock, so its COGS should be reversed - it wasn't actually consumed). Everything
    else (DEFECTIVE, CUSTOMER_DAMAGED, CARRIER_DAMAGED, etc.) is non-sellable - the
    unit is genuinely gone, so its COGS stays charged, same as before this feature."""
    by_sku = defaultdict(lambda: {"sellable": 0, "non_sellable": 0})
    by_sku_month = defaultdict(lambda: {"sellable": 0, "non_sellable": 0})
    for row in returns_rows:
        sku = row.get("sku") or "UNKNOWN"
        qty = int(row.get("quantity") or 1)
        month = (row.get("return-date") or "")[:7]
        key = "sellable" if row.get("detailed-disposition") == "SELLABLE" else "non_sellable"
        by_sku[sku][key] += qty
        if month:
            by_sku_month[(sku, month)][key] += qty
    return by_sku, by_sku_month


def build_dashboard_data(agg, orders, inventory, months, returns_rows=None):
    sellable_by_sku, sellable_by_sku_month = parse_returns(returns_rows or [])

    total_gross = sum(v["revenue"] for v in agg["sku_totals"].values())
    total_ad = sum(v["ad_spend"] for v in agg["month_totals"].values())
    storage_per_month = agg["service_fee_total"] / max(len(months), 1)

    def sku_cost_per_unit(sku, is_est, net_rev_for_est, units):
        if not is_est:
            return LANDED_COST[sku]
        return (COGS_ESTIMATE_PCT_OF_NET_REVENUE * net_rev_for_est / units) if units else 0.0

    # Chargebacks, shipping/payment charges, and refund-fee credits are ALREADY real
    # per-SKU data (verified 2026-08-15 against raw payload - ItemFeeList/ItemChargeList
    # entries carry SellerSKU) and already flow into tot["fees"]/tot["refund_fees"] below.
    # Adjustments (reimbursements) also carry SellerSKU - real, added via tot["adjustments"].
    # Only FBA storage/inbound genuinely has NO SKU or date field anywhere in the API
    # response, so it alone is allocated pro-rata by revenue share (like ad spend),
    # never claimed as real.
    total_storage = agg["service_fee_total"]

    sku_rows = []
    for sku, tot in agg["sku_totals"].items():
        if tot["revenue"] == 0 and tot["units"] == 0:
            continue
        net_rev = tot["revenue"] + tot["refunds"]
        net_fees = tot["fees"] + tot["refund_fees"] + tot["adjustments"]
        is_est = sku not in LANDED_COST
        sellable_returns = sellable_by_sku.get(sku, {}).get("sellable", 0)
        cogs_units = max(0, tot["units"] - sellable_returns)
        cogs = (LANDED_COST[sku] * cogs_units) if not is_est else (COGS_ESTIMATE_PCT_OF_NET_REVENUE * net_rev)
        gross_margin = net_rev - cogs
        rev_share = tot["revenue"] / total_gross if total_gross else 0
        ad_alloc = total_ad * rev_share
        storage_alloc = total_storage * rev_share
        net_profit = gross_margin + net_fees + ad_alloc + storage_alloc
        cost_per_unit = sku_cost_per_unit(sku, is_est, net_rev, tot["units"])

        months_detail = {}
        for m, v in agg["sku_month"].get(sku, {}).items():
            if m not in months:
                continue
            m_gross = v["revenue"]
            m_refund = agg["refund_sku_month"].get(sku, {}).get(m, 0.0)
            m_net_rev = m_gross + m_refund
            m_fees = v["fees"] + agg["refund_fees_sku_month"].get(sku, {}).get(m, 0.0) + v.get("adjustments", 0.0)
            if is_est:
                m_cogs = COGS_ESTIMATE_PCT_OF_NET_REVENUE * m_net_rev
            else:
                m_sellable_returns = sellable_by_sku_month.get((sku, m), {}).get("sellable", 0)
                m_cogs_units = max(0, v["units"] - m_sellable_returns)
                m_cogs = LANDED_COST[sku] * m_cogs_units
            m_margin = m_net_rev - m_cogs
            m_total_gross = agg["month_totals"].get(m, {}).get("revenue", 0.0)
            m_total_ad = agg["month_totals"].get(m, {}).get("ad_spend", 0.0)
            m_rev_share = m_gross / m_total_gross if m_total_gross else 0
            m_ad_alloc = m_total_ad * m_rev_share
            m_storage_alloc = storage_per_month * m_rev_share
            m_net_profit = m_margin + m_fees + m_ad_alloc + m_storage_alloc
            months_detail[m] = {
                "revenue": round(m_gross, 2), "units": v["units"],
                "refunds": round(m_refund, 2),
                "net_revenue": round(m_net_rev, 2), "cogs": round(m_cogs, 2),
                "gross_margin": round(m_margin, 2),
                "margin_pct": round(m_margin / m_net_rev * 100, 1) if m_net_rev else None,
                "amazon_fees": round(m_fees, 2), "ad_spend_allocated": round(m_ad_alloc, 2),
                "storage_allocated": round(m_storage_alloc, 2),
                "net_profit": round(m_net_profit, 2),
                "net_margin_pct": round(m_net_profit / m_net_rev * 100, 1) if m_net_rev else None,
                "asp": round(m_net_rev / v["units"], 2) if v["units"] else None,
            }

        sku_rows.append({
            "sku": sku, "gross_revenue": round(tot["revenue"], 2), "units": tot["units"],
            "refunds": round(tot["refunds"], 2),
            "net_revenue": round(net_rev, 2), "cogs": round(cogs, 2), "cogs_is_estimated": is_est,
            "gross_margin": round(gross_margin, 2),
            "margin_pct": round(gross_margin / net_rev * 100, 1) if net_rev else None,
            "amazon_fees": round(net_fees, 2), "ad_spend_allocated": round(ad_alloc, 2),
            "storage_allocated": round(storage_alloc, 2),
            "net_profit": round(net_profit, 2),
            "net_margin_pct": round(net_profit / net_rev * 100, 1) if net_rev else None,
            "asp": round(net_rev / tot["units"], 2) if tot["units"] else None,
            "gross_asp": round(tot["revenue"] / tot["units"], 2) if tot["units"] else None,
            "cost_per_unit": round(cost_per_unit, 2),
            "months": months_detail,
        })
    sku_rows.sort(key=lambda r: -r["gross_revenue"])

    cost_table = []
    all_skus = set(PRODUCT_COST_AED.keys()) | {r["sku"] for r in sku_rows}
    for sku in all_skus:
        is_est = sku not in PRODUCT_COST_AED
        row = next((r for r in sku_rows if r["sku"] == sku), None)
        actual_asp = row["gross_asp"] if row else None
        if is_est:
            # Pricing reference, not a P&L figure - use GROSS ASP (not net-of-refund
            # revenue) as the estimate basis, so a fully-refunded period (e.g.
            # RH218SBPZ, net revenue 0) doesn't produce a nonsensical zero cost.
            # No USD source exists for these SKUs, so freight/duty/VAT/product-cost
            # stay None - only a directly-estimated AED landed cost is possible.
            landed_aed = (COGS_ESTIMATE_PCT_OF_NET_REVENUE * actual_asp) if actual_asp else None
            landed_usd = (landed_aed / AED_USD_PEG) if landed_aed is not None else None
            comp = {"product_cost_usd": None, "freight_usd": None, "duty_usd": None, "vat_usd": None, "landed_cost_aed": landed_aed, "landed_cost_usd": landed_usd}
        else:
            comp = cost_components_usd(PRODUCT_COST_USD[sku])
        recommended_asp_60 = (comp["landed_cost_aed"] / 0.40) if comp["landed_cost_aed"] else None
        cost_table.append({
            "sku": sku, "is_estimated": is_est,
            "product_cost_usd": round(comp["product_cost_usd"], 2) if comp["product_cost_usd"] is not None else None,
            "freight_usd": round(comp["freight_usd"], 2) if comp["freight_usd"] is not None else None,
            "duty_usd": round(comp["duty_usd"], 2) if comp["duty_usd"] is not None else None,
            "vat_usd": round(comp["vat_usd"], 2) if comp["vat_usd"] is not None else None,
            "landed_cost_usd": round(comp["landed_cost_usd"], 2) if comp.get("landed_cost_usd") is not None else None,
            "landed_cost": round(comp["landed_cost_aed"], 2) if comp["landed_cost_aed"] is not None else None,
            "recommended_asp_60pct_margin": round(recommended_asp_60, 2) if recommended_asp_60 else None,
            "actual_asp": round(actual_asp, 2) if actual_asp is not None else None,
        })
    cost_table.sort(key=lambda r: -(r["landed_cost"] or 0))

    def month_refund_split(m):
        """Splits a month's total refund $ into sellable vs non-sellable, proportionally
        by each SKU's sellable/non-sellable RETURN COUNT that month (same evidence used
        for the COGS reversal above). A refund $ with no matching return record at all
        (e.g. a goodwill refund, or the returns report simply hasn't caught up yet) has
        no sellable evidence, so it's counted as non-sellable - conservative, matches the
        COGS-reversal rule of only crediting sellable when there's positive proof of it."""
        sellable_refund = 0.0
        non_sellable_refund = 0.0
        for sku, mv in agg["refund_sku_month"].items():
            refund_amt = mv.get(m, 0.0)
            if not refund_amt:
                continue
            counts = sellable_by_sku_month.get((sku, m), {"sellable": 0, "non_sellable": 0})
            total_returns = counts["sellable"] + counts["non_sellable"]
            sellable_frac = (counts["sellable"] / total_returns) if total_returns else 0.0
            sellable_refund += refund_amt * sellable_frac
            non_sellable_refund += refund_amt * (1 - sellable_frac)
        return sellable_refund, non_sellable_refund

    monthly_rows = []
    cost_breakdown_ytd = {
        "commission": 0.0, "fulfillment": 0.0, "chargebacks": 0.0,
        "shipcharges": 0.0, "refund_credits": 0.0, "adjustments": 0.0, "storage": 0.0,
    }
    for m in months:
        mt = agg["month_totals"].get(m, {"revenue": 0.0, "refunds": 0.0, "ad_spend": 0.0})
        gross = mt["revenue"]
        refunds = mt["refunds"]
        refunds_sellable, refunds_non_sellable = month_refund_split(m)
        net_rev = gross + refunds
        cogs_m = 0.0
        for sku, mv in agg["sku_month"].items():
            v = mv.get(m)
            if not v:
                continue
            if sku in LANDED_COST:
                m_sellable = sellable_by_sku_month.get((sku, m), {}).get("sellable", 0)
                cogs_m += LANDED_COST[sku] * max(0, v["units"] - m_sellable)
            else:
                sku_net_rev_m = v["revenue"] + agg["refund_sku_month"].get(sku, {}).get(m, 0.0)
                cogs_m += COGS_ESTIMATE_PCT_OF_NET_REVENUE * sku_net_rev_m
        fm = agg["fee_month"].get(m, {})
        commission = fm.get("Commission", 0.0)
        fulfillment = fm.get("FBAPerUnitFulfillmentFee", 0.0)
        chargebacks = fm.get("ShippingChargeback", 0.0) + fm.get("CODChargeback", 0.0)
        shipcharges = fm.get("Charge:ShippingCharge", 0.0) + fm.get("Charge:PaymentMethodFee", 0.0)
        refund_credits = agg["month_refund_fee_credits"].get(m, 0.0)
        adjustments = agg["month_adjustments"].get(m, 0.0)
        other_costs = commission + fulfillment + chargebacks + shipcharges + refund_credits + adjustments + storage_per_month
        cost_breakdown_ytd["commission"] += commission
        cost_breakdown_ytd["fulfillment"] += fulfillment
        cost_breakdown_ytd["chargebacks"] += chargebacks
        cost_breakdown_ytd["shipcharges"] += shipcharges
        cost_breakdown_ytd["refund_credits"] += refund_credits
        cost_breakdown_ytd["adjustments"] += adjustments
        cost_breakdown_ytd["storage"] += storage_per_month
        gross_margin_m = net_rev - cogs_m
        profit_before_ads = gross_margin_m + other_costs
        ad_spend_m = mt["ad_spend"]
        net_profit_m = profit_before_ads + ad_spend_m
        monthly_rows.append({
            "month": m, "gross_revenue": round(gross, 2), "refunds": round(refunds, 2),
            "refunds_sellable": round(refunds_sellable, 2), "refunds_non_sellable": round(refunds_non_sellable, 2),
            "net_revenue": round(net_rev, 2), "cogs": round(cogs_m, 2),
            "gross_margin": round(gross_margin_m, 2), "other_costs": round(other_costs, 2),
            "profit_before_ads": round(profit_before_ads, 2), "ad_spend": round(ad_spend_m, 2),
            "net_profit": round(net_profit_m, 2),
            "orders": len(agg["orders_by_month"].get(m, set())),
            "units": sum(v["units"] for mv in agg["sku_month"].values() for mm, v in mv.items() if mm == m),
        })

    gross_revenue_ytd = sum(r["gross_revenue"] for r in monthly_rows)
    net_revenue_ytd = sum(r["net_revenue"] for r in monthly_rows)
    other_costs_ytd = sum(r["other_costs"] for r in monthly_rows)
    ad_spend_ytd = sum(r["ad_spend"] for r in monthly_rows)
    units_ytd = sum(r["units"] for r in monthly_rows)
    year_start = datetime.datetime(datetime.datetime.utcnow().year, 1, 1)
    days_elapsed = max(1, (datetime.datetime.utcnow() - year_start).days + 1)

    ytd = {
        "gross_revenue": round(gross_revenue_ytd, 2),
        "refunds": round(sum(r["refunds"] for r in monthly_rows), 2),
        "refunds_sellable": round(sum(r["refunds_sellable"] for r in monthly_rows), 2),
        "refunds_non_sellable": round(sum(r["refunds_non_sellable"] for r in monthly_rows), 2),
        "net_revenue": round(net_revenue_ytd, 2),
        "cogs": round(sum(r["cogs"] for r in monthly_rows), 2),
        "gross_margin": round(sum(r["gross_margin"] for r in monthly_rows), 2),
        "other_costs": round(other_costs_ytd, 2),
        "profit_before_ads": round(sum(r["profit_before_ads"] for r in monthly_rows), 2),
        "ad_spend": round(ad_spend_ytd, 2),
        "net_profit": round(sum(r["net_profit"] for r in monthly_rows), 2),
        # Amazon-fees/ads-only bottom line (excludes COGS) - matches the waterfall,
        # NOT the same thing as net_profit (which is COGS-inclusive).
        "net_proceeds": round(net_revenue_ytd + other_costs_ytd + ad_spend_ytd, 2),
        "days_elapsed": days_elapsed,
        "revenue_per_day": round(gross_revenue_ytd / days_elapsed, 2),
        "units_per_day": round(units_ytd / days_elapsed, 3),
    }

    shipped = sum(1 for o in orders if o.get("OrderStatus") == "Shipped")
    canceled = sum(1 for o in orders if o.get("OrderStatus") == "Canceled")

    TRANSIT_DAYS = 90
    sku_units_ytd = {r["sku"]: r["units"] for r in sku_rows}
    inv_rows = []
    for r in inventory:
        d = r.get("inventoryDetails", {})
        sku = r.get("sellerSku")
        az_inv = r.get("totalQuantity", 0)
        units_sold = sku_units_ytd.get(sku, 0)
        avg_daily_sales = units_sold / days_elapsed if units_sold else 0.0
        wh_inv = WH_INV_TRACKER.get(sku)  # None only for SKUs absent from the tracker entirely
        total_inv = az_inv if wh_inv is None else az_inv + wh_inv
        reorder_point = avg_daily_sales * TRANSIT_DAYS
        days_of_cover = (total_inv / avg_daily_sales) if avg_daily_sales else None
        suggested_reorder_qty = max(0, round(reorder_point - total_inv)) if avg_daily_sales else 0
        inv_rows.append({
            "sku": sku, "asin": r.get("asin"), "name": r.get("productName"),
            "fulfillable": d.get("fulfillableQuantity", 0),
            "inbound": (d.get("inboundWorkingQuantity", 0) + d.get("inboundShippedQuantity", 0) + d.get("inboundReceivingQuantity", 0)),
            "reserved": (d.get("reservedQuantity", {}) or {}).get("totalReservedQuantity", 0),
            "unfulfillable": (d.get("unfulfillableQuantity", {}) or {}).get("totalUnfulfillableQuantity", 0),
            "az_inv": az_inv, "wh_inv": wh_inv, "total_inv": total_inv,
            "units_sold_ytd": units_sold,
            "avg_daily_sales": round(avg_daily_sales, 3),
            "reorder_point_90d": round(reorder_point, 1),
            "days_of_cover": round(days_of_cover, 1) if days_of_cover is not None else None,
            "suggested_reorder_qty": suggested_reorder_qty,
            "needs_reorder": bool(avg_daily_sales and total_inv <= reorder_point),
        })

    # Returns breakdown (added 2026-08-15, from the FBA Customer Returns report):
    # sellable-disposition returns get their COGS reversed above (the unit goes back
    # to stock, wasn't actually consumed); non-sellable (DEFECTIVE/CUSTOMER_DAMAGED/
    # etc.) keep their COGS charged, same as before this feature existed.
    returns_breakdown = []
    for sku, counts in sellable_by_sku.items():
        sellable = counts["sellable"]
        non_sellable = counts["non_sellable"]
        if sellable == 0 and non_sellable == 0:
            continue
        landed = LANDED_COST.get(sku)
        cogs_reversed = (landed * sellable) if landed is not None else None
        returns_breakdown.append({
            "sku": sku, "sellable": sellable, "non_sellable": non_sellable,
            "total_returns": sellable + non_sellable,
            "cogs_reversed": round(cogs_reversed, 2) if cogs_reversed is not None else None,
        })
    returns_breakdown.sort(key=lambda r: -r["total_returns"])
    cogs_reversed_ytd = round(sum(r["cogs_reversed"] or 0 for r in returns_breakdown), 2)

    return {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "months": months,
        "ytd": ytd,
        "monthly": monthly_rows,
        "sku_rows": sku_rows,
        "cost_table": cost_table,
        "inventory": inv_rows,
        "orders_total": len(orders), "orders_shipped": shipped, "orders_canceled": canceled,
        "storage_ytd": round(agg["service_fee_total"], 2),
        "cost_breakdown_ytd": {k: round(v, 2) for k, v in cost_breakdown_ytd.items()},
        "returns_breakdown": returns_breakdown,
        "cogs_reversed_ytd": cogs_reversed_ytd,
    }


def main():
    token = get_amz_access_token()
    now = datetime.datetime.utcnow()
    start = datetime.datetime(now.year, 1, 1)
    months = [f"{now.year}-{m:02d}" for m in range(1, now.month + 1)]

    print("Fetching orders...")
    orders = fetch_orders(token, start, now)
    print("Fetching order items skipped (not required for dashboard numbers - Finances API covers revenue/units)")

    print("Fetching finance events...")
    finance_pages = fetch_finance_events(token, start, now)

    print("Fetching inventory...")
    inventory = fetch_inventory(token)

    print("Fetching FBA returns report (for sellable-return COGS reversal)...")
    returns_rows = fetch_returns_report(token, start, now)
    print(f"  {len(returns_rows)} return records")

    agg = aggregate(finance_pages)
    data = build_dashboard_data(agg, orders, inventory, months, returns_rows)

    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved {OUT_PATH}")
    print(f"YTD gross revenue: {data['ytd']['gross_revenue']}, net profit: {data['ytd']['net_profit']}")


if __name__ == "__main__":
    main()
