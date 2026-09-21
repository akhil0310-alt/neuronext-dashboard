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

# Running weighted-average-cost ledger (added 2026-09-06, first use: a UK-origin batch
# landing 04-Sep-2026 at a different fully-loaded cost than existing UAE stock, with more
# such batches expected in future). inventory_cost_ledger.json holds only the EXTRA dated
# batches (e.g. the UK shipment) - LANDED_COST above is treated as an implicit "opening"
# batch per SKU, back-dated to 2026-01-01, whose qty is derived live (see
# sku_cost_batches()) rather than tracked by hand. This is a single blended WAC per SKU,
# not a FIFO/lot system - ageing is "time since last batch," not per-unit lot age (user
# confirmed this tradeoff explicitly when this feature was scoped).
COST_LEDGER_PATH = os.environ.get(
    "COST_LEDGER_PATH", r"C:\Users\akhil\OneDrive\Desktop\NeuroNext_Integration\scripts\inventory_cost_ledger.json")


def load_cost_ledger(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    return {sku: batches for sku, batches in data.items() if sku != "_readme"}


COST_LEDGER = load_cost_ledger(COST_LEDGER_PATH)


def sku_cost_batches(sku, total_inv_now):
    """Full batch history for a SKU's running WAC: an implicit opening batch (everything
    already in stock, at the existing LANDED_COST) plus any dated batches from
    COST_LEDGER. The opening batch's qty = total_inv_now minus the sum of ledger batch
    qtys - NOT a tracked historical fact, since this is a blended-WAC model, recomputed
    from today's live tracked inventory rather than a perpetual per-unit record."""
    if sku not in LANDED_COST:
        return []
    extra = COST_LEDGER.get(sku, [])
    opening_qty = max(0, total_inv_now - sum(b["qty"] for b in extra))
    batches = [{"date": "2026-01-01", "qty": opening_qty, "landed_cost_aed": LANDED_COST[sku]}] + extra
    return sorted(batches, key=lambda b: b["date"])


def wac_as_of(batches, date_str):
    """Qty-weighted average landed cost (AED) of every batch dated on or before
    date_str. Falls back to the earliest batch if date_str precedes all of them
    (shouldn't happen given the 2026-01-01 opening batch, but guards divide-by-zero)."""
    applicable = [b for b in batches if b["date"] <= date_str] or batches[:1]
    qty = sum(b["qty"] for b in applicable)
    if not qty:
        return None
    return sum(b["qty"] * b["landed_cost_aed"] for b in applicable) / qty

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

# Inventory ageing (added 2026-09-21, user-requested): buckets + write-down provision
# %ages the user specified explicitly - not a standard/derived schedule, don't change
# without being told. Applied to LANDED COST (current running WAC where one exists),
# same cost basis as Inventory Value elsewhere on the dashboard.
AGEING_BUCKETS = [
    ("0-90", 0, 90, 0.00),
    ("90-120", 91, 120, 0.10),
    ("120-180", 121, 180, 0.25),
    ("180-270", 181, 270, 0.50),
    ("270-360", 271, 360, 0.75),
    ("360+", 361, 10**6, 1.00),
]


def _parse_ddmmyyyy(s):
    d, m, y = s.split(".")
    return datetime.date(int(y), int(m), int(d))


def fetch_wh_ageing_events():
    """Live-fetches the FULL dated Inward/Outbound event history per model from the
    same 'Neuronext SOH & Outbound' sheet/tab used by fetch_wh_inv_tracker() (added
    2026-09-21, for the inventory ageing matrix - user asked to reconstruct ageing
    from this sheet). Same block-scanning approach as fetch_wh_inv_tracker() (14
    fixed-width 5-column blocks, located by label text not column index) but reads
    every dated row in each block (not just the 'Remaining' summary row): row offset
    +0 is the event date (DD.MM.YYYY), +2 is the event label ('Inward Qty' or 'Sent
    to'), +3 is the qty. Returns {model: [{"date","type","qty"}, ...]} - on any
    failure returns {} and ageing is simply omitted from that run's output (non-fatal,
    matches the WH Inv tracker's own fail-soft pattern) rather than failing the whole
    refresh."""
    try:
        import gspread
        gc = gspread.service_account(filename=GSHEET_CRED_PATH)
        sh = gc.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Current Summary")
        rows = ws.get_all_values()
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        nblocks = (ncols + 4) // 5
        events_by_model = {}
        for b in range(nblocks):
            off = b * 5
            if off + 3 >= ncols:
                continue
            if rows[4][off + 2].strip() != "Model":
                continue
            model = rows[4][off + 3].strip()
            if not model:
                continue
            evs = []
            for r in rows[5:]:
                date_s = r[off + 0].strip()
                label = r[off + 2].strip()
                qty_s = r[off + 3].strip()
                if date_s and label and qty_s.isdigit():
                    evs.append({"date": date_s, "type": label, "qty": int(qty_s)})
            events_by_model[model] = evs
        return events_by_model
    except Exception as e:
        print(f"WARNING: WH ageing event-history fetch failed ({e}), skipping ageing matrix this run")
        return {}


WH_AGEING_EVENTS = fetch_wh_ageing_events()


def simulate_fifo_ageing(events, as_of):
    """FIFO depletion simulation for one model's event list: oldest inward batch
    assumed consumed first by ANY outbound event (to Amazon or another channel),
    since the sheet has no per-unit lot tracking. Returns {bucket_name: remaining_qty}
    as of `as_of` (a datetime.date). Verified 2026-09-21: reconstructed totals tie to
    the sheet's own 'Remaining' figures to within 0-2 units across all 14 models
    (rounding-level noise, not a systemic error)."""
    evs_sorted = sorted(events, key=lambda e: _parse_ddmmyyyy(e["date"]))
    batches = []  # [[date, remaining_qty], ...]
    for e in evs_sorted:
        d = _parse_ddmmyyyy(e["date"])
        if e["type"] == "Inward Qty":
            batches.append([d, e["qty"]])
        else:
            qty_to_remove = e["qty"]
            for batch in batches:
                if qty_to_remove <= 0:
                    break
                take = min(batch[1], qty_to_remove)
                batch[1] -= take
                qty_to_remove -= take
            # qty_to_remove > 0 here means outbound exceeded tracked inward (a data
            # gap in the sheet, e.g. an inward row predating this tracker's start) -
            # silently dropped rather than going negative; not expected in practice.
    bucket_qty = {name: 0 for name, *_ in AGEING_BUCKETS}
    for d, qty in batches:
        if qty <= 0:
            continue
        age = (as_of - d).days
        for name, lo, hi, _ in AGEING_BUCKETS:
            if lo <= age <= hi:
                bucket_qty[name] += qty
                break
        else:
            bucket_qty["360+"] += qty
    return bucket_qty


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


def _fetch_report_rows(token, report_type, start, now, label):
    """Shared request/poll/download logic for any SP-API tab-separated report type.
    Polls up to 5 minutes before giving up; returns [] (never raises) on any failure
    so a slow/failed report degrades the feature that needs it rather than the whole
    refresh - each caller's own docstring explains what it loses in that case."""
    status, resp = spapi_request(token, "POST", "/reports/2021-06-30/reports", body={
        "reportType": report_type,
        "marketplaceIds": [MARKETPLACE_ID],
        "dataStartTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataEndTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    if status != 202:
        print(f"{label} report request failed ({status}): {resp} - skipping")
        return []
    report_id = resp["reportId"]
    report_doc_id = None
    for attempt in range(30):
        time.sleep(10)
        s, r = spapi_request(token, "GET", f"/reports/2021-06-30/reports/{report_id}")
        proc_status = r.get("processingStatus") if isinstance(r, dict) else None
        print(f"{label} report poll {attempt}: {proc_status}")
        if proc_status == "DONE":
            report_doc_id = r.get("reportDocumentId")
            break
        if proc_status in ("CANCELLED", "FATAL"):
            print(f"{label} report failed: {r} - skipping")
            return []
    if not report_doc_id:
        print(f"{label} report did not finish in time - skipping")
        return []
    s, doc = spapi_request(token, "GET", f"/reports/2021-06-30/documents/{report_doc_id}")
    with urllib.request.urlopen(doc["url"]) as resp2:
        raw = resp2.read()
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return list(reader)


def fetch_returns_report(token, start, now):
    """Pulls the FBA Customer Returns report (real per-return disposition data -
    SELLABLE vs DEFECTIVE/CUSTOMER_DAMAGED/etc.) and returns a list of dict rows
    (return-date, sku, quantity, detailed-disposition, ...). Verified 2026-08-15:
    this report type does NOT need the Reports-API Seller-Central role that the
    Sessions/Buy-Box report needs (that one 403s) - this one works with current creds.
    On failure: [] and COGS silently falls back to no-reversal behavior (pre-2026-08-15)."""
    return _fetch_report_rows(token, "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA", start, now, "returns")


def fetch_reimbursements_report(token, start, now):
    """Pulls the FBA Reimbursements report (added 2026-09-06, user asked whether
    Amazon-caused warehouse damage could be identified for reimbursement claims -
    this is the report that actually tracks that, NOT the Customer Returns report's
    disposition field). Verified against this account: access works (202 accepted),
    columns are approval-date/reimbursement-id/case-id/amazon-order-id/reason/sku/
    fnsku/asin/product-name/condition/currency-unit/amount-per-unit/amount-total/
    quantity-reimbursed-cash/quantity-reimbursed-inventory/quantity-reimbursed-total/
    original-reimbursement-id/original-reimbursement-type. Only 1 row exists YTD as
    of 2026-09-06 (AED 107, reason CustomerReturn, already reflected in the existing
    "Reimbursements / adjustments" cost-breakdown line from ServiceFeeEventList - this
    report and that figure are NOT summed together, they're two views of the same
    money) - so don't be surprised if this stays a short list. On failure: []."""
    return _fetch_report_rows(token, "GET_FBA_REIMBURSEMENTS_DATA", start, now, "reimbursements")


REMOVAL_ORDER_SOURCE_LABELS = {
    "Amazon-initiated Automated Aged fulfillable Removal System": "Aged (Amazon-initiated)",
    "Seller-configured Automated Unfulfillable Removal System": "Unfulfillable (auto)",
    "Seller-initiated Manual Removal": "Manual",
}


def fetch_removal_orders_report(token, start, now):
    """Pulls the FBA Removal Order Detail report (added 2026-09-06, user asked where
    Amazon's ageing-threshold auto-returns to warehouse are captured - this is that
    report; nothing else in this pipeline touches it). Verified against this account:
    access works (202 accepted), 52 real rows YTD across 11 SKUs. The `order-source`
    field is what actually answers the user's question - values seen are 'Amazon-
    initiated Automated Aged fulfillable Removal System' (the ageing-threshold auto-
    return), 'Seller-configured Automated Unfulfillable Removal System' (a DIFFERENT
    thing - the seller's own auto-removal setting for unsellable stock), and 'Seller-
    initiated Manual Removal'. See REMOVAL_ORDER_SOURCE_LABELS for the display
    shorthand - don't relabel 'Aged (Amazon-initiated)' to something vaguer, that's the
    one the user actually cares about. `order-type` has only ever been 'Return' for
    this account (nothing disposed) and `removal-fee` has always been 0.00 - don't
    assume either stays that way forever. Columns: request-date/order-id/order-source/
    order-type/service-speed/order-status/last-updated-date/sku/fnsku/disposition/
    requested-quantity/cancelled-quantity/disposed-quantity/shipped-quantity/
    in-process-quantity/removal-fee/currency. On failure: []."""
    return _fetch_report_rows(token, "GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA", start, now, "removal orders")


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
    # SKU Analysis tab (added 2026-09-05): day-level units/revenue, separate from the
    # month-level dicts below which everything else on the dashboard uses. Day-level
    # data is ONLY used for that one tab - never fed into any other calculation, so a
    # gap here can't silently affect the rest of the dashboard's numbers.
    sku_day = defaultdict(lambda: defaultdict(lambda: {"units": 0, "revenue": 0.0}))
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
            posted_full = shp.get("PostedDate") or ""
            posted = posted_full[:7]
            posted_day = posted_full[:10]
            order_id = shp.get("AmazonOrderId")
            if order_id:
                all_orders_with_events.add(order_id)
                orders_by_month[posted].add(order_id)
            for item in shp.get("ShipmentItemList", []) or []:
                sku = item.get("SellerSKU") or "UNKNOWN"
                qty = item.get("QuantityShipped") or 0
                sku_month[sku][posted]["units"] += qty
                sku_totals[sku]["units"] += qty
                if posted_day:
                    sku_day[sku][posted_day]["units"] += qty
                for chg in item.get("ItemChargeList", []) or []:
                    ctype = chg.get("ChargeType") or ""
                    val = (chg.get("ChargeAmount", {}) or {}).get("CurrencyAmount", 0) or 0
                    if ctype == "Principal":
                        month_totals[posted]["revenue"] += val
                        sku_month[sku][posted]["revenue"] += val
                        sku_totals[sku]["revenue"] += val
                        if posted_day:
                            sku_day[sku][posted_day]["revenue"] += val
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
        "sku_day": sku_day,
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
    unit is genuinely gone, so its COGS stays charged, same as before this feature.

    Also returns a third dict, by_sku_disposition (YTD only, added 2026-09-06), for the
    Returns Breakdown table's Customer Damaged / Defective / Amazon Damaged split -
    checked against a real fetch of this account's 2026 returns (46 rows): the only
    detailed-disposition values actually seen are SELLABLE, DEFECTIVE, CUSTOMER_DAMAGED
    - amazon_damaged and other_non_sellable are both 0 for every SKU right now, kept
    as explicit always-present columns (not omitted) so the split stays visible and
    ready to populate the moment a return actually lands in one of them, rather than
    silently appearing out of nowhere later. AMAZON_DAMAGED_DISPOSITIONS below is
    Amazon's documented set of dispositions that mean AMAZON'S OWN FAULT (damaged in
    Amazon's own carrier/warehouse handling, before or during return processing) and
    ARE reimbursement-eligible - unverified against this account (none have occurred),
    based on Amazon's published FBA disposition taxonomy. DEFECTIVE is a product/
    manufacturing issue - NOT Amazon's fault and NOT reimbursable via this report; kept
    as its own bucket from CUSTOMER_DAMAGED for visibility only, not a reimbursement
    claim list. Real Amazon-caused warehouse loss/damage reimbursement (inventory lost/
    damaged inside Amazon's warehouse, never mind a customer return at all) lives in a
    completely different report (GET_FBA_REIMBURSEMENTS_DATA), not here."""
    AMAZON_DAMAGED_DISPOSITIONS = {"CARRIER_DAMAGED", "WAREHOUSE_DAMAGED", "FULFILLMENT_CENTER_DAMAGED"}
    by_sku = defaultdict(lambda: {"sellable": 0, "non_sellable": 0})
    by_sku_month = defaultdict(lambda: {"sellable": 0, "non_sellable": 0})
    by_sku_disposition = defaultdict(lambda: {
        "sellable": 0, "customer_damaged": 0, "defective": 0,
        "amazon_damaged": 0, "other_non_sellable": 0,
    })
    for row in returns_rows:
        sku = row.get("sku") or "UNKNOWN"
        qty = int(row.get("quantity") or 1)
        month = (row.get("return-date") or "")[:7]
        disp = row.get("detailed-disposition")
        key = "sellable" if disp == "SELLABLE" else "non_sellable"
        by_sku[sku][key] += qty
        if month:
            by_sku_month[(sku, month)][key] += qty
        if disp == "SELLABLE":
            dkey = "sellable"
        elif disp == "CUSTOMER_DAMAGED":
            dkey = "customer_damaged"
        elif disp == "DEFECTIVE":
            dkey = "defective"
        elif disp in AMAZON_DAMAGED_DISPOSITIONS:
            dkey = "amazon_damaged"
        else:
            dkey = "other_non_sellable"
        by_sku_disposition[sku][dkey] += qty
    return by_sku, by_sku_month, by_sku_disposition


def build_dashboard_data(agg, orders, inventory, months, returns_rows=None, reimbursement_rows=None, removal_order_rows=None):
    sellable_by_sku, sellable_by_sku_month, disposition_by_sku = parse_returns(returns_rows or [])

    total_gross = sum(v["revenue"] for v in agg["sku_totals"].values())
    total_ad = sum(v["ad_spend"] for v in agg["month_totals"].values())
    storage_per_month = agg["service_fee_total"] / max(len(months), 1)

    # Chargebacks, shipping/payment charges, and refund-fee credits are ALREADY real
    # per-SKU data (verified 2026-08-15 against raw payload - ItemFeeList/ItemChargeList
    # entries carry SellerSKU) and already flow into tot["fees"]/tot["refund_fees"] below.
    # Adjustments (reimbursements) also carry SellerSKU - real, added via tot["adjustments"].
    # Only FBA storage/inbound genuinely has NO SKU or date field anywhere in the API
    # response, so it alone is allocated pro-rata by revenue share (like ad spend),
    # never claimed as real.
    total_storage = agg["service_fee_total"]

    # Running-WAC support (added 2026-09-06): total_inv_by_sku mirrors the az_inv+wh_inv
    # logic used for the Inventory tab further below, computed early here so the cost
    # ledger's implicit "opening batch" qty (see sku_cost_batches()) is available before
    # COGS is computed. sku_batches_map/current_wac are built once per refresh, not once
    # per month, since a SKU's batch history doesn't change within a single run.
    total_inv_by_sku = {}
    for r in inventory:
        r_sku = r.get("sellerSku")
        r_az = r.get("totalQuantity", 0)
        r_wh = WH_INV_TRACKER.get(r_sku)
        total_inv_by_sku[r_sku] = r_az if r_wh is None else r_az + r_wh
    today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    today_date = datetime.datetime.utcnow().date()
    sku_batches_map = {sku: sku_cost_batches(sku, total_inv_by_sku.get(sku, 0)) for sku in LANDED_COST}
    current_wac = {sku: wac_as_of(batches, today_str) for sku, batches in sku_batches_map.items()}

    # Inventory ageing matrix (added 2026-09-21, user-requested): warehouse stock only
    # (WH_AGEING_EVENTS is scoped to the SOH sheet, which tracks warehouse - not Amazon
    # FBA - inventory; Amazon's API exposes no per-lot receipt date, so units "Sent to
    # Amazon" can't be aged further once they leave the warehouse). Cost basis is the
    # same landed cost (current WAC where one exists) used for Inventory Value.
    inventory_ageing = []
    ageing_bucket_names = [b[0] for b in AGEING_BUCKETS]
    for model, sku in MODEL_TO_SKU.items():
        events = WH_AGEING_EVENTS.get(model)
        if not events:
            continue
        bucket_qty = simulate_fifo_ageing(events, today_date)
        cost = current_wac.get(sku)
        if cost is None:
            cost = LANDED_COST.get(sku)
        if cost is None:
            continue
        buckets_out = {}
        total_units = 0
        total_value = 0.0
        total_provision = 0.0
        for name, lo, hi, pct in AGEING_BUCKETS:
            qty = bucket_qty.get(name, 0)
            value = qty * cost
            buckets_out[name] = {"units": qty, "value": round(value, 2)}
            total_units += qty
            total_value += value
            total_provision += value * pct
        blended_provision_pct = (total_provision / total_value) if total_value else 0.0
        marked_down_cost = cost * (1 - blended_provision_pct)
        inventory_ageing.append({
            "sku": sku,
            "landed_cost": round(cost, 2),
            "marked_down_cost": round(marked_down_cost, 2),
            "total_units": total_units,
            "total_value": round(total_value, 2),
            "buckets": buckets_out,
        })
    inventory_ageing.sort(key=lambda r: -r["total_value"])
    ageing_totals = {
        "total_units": sum(r["total_units"] for r in inventory_ageing),
        "total_value": round(sum(r["total_value"] for r in inventory_ageing), 2),
        "buckets": {
            name: {
                "units": sum(r["buckets"][name]["units"] for r in inventory_ageing),
                "value": round(sum(r["buckets"][name]["value"] for r in inventory_ageing), 2),
            }
            for name in ageing_bucket_names
        },
    }

    def month_gross_cogs_and_rate(sku, m, month_units):
        """Gross COGS for a SKU's month, WAC-aware: each day's units are costed at
        whatever batch was in effect on that day (sku_cost_batches/wac_as_of), so a
        mid-month batch change (e.g. the 04-Sep-2026 UK batch) blends correctly instead
        of one flat rate for the whole month. Returns (gross_cogs, effective_rate) -
        effective_rate is the month's own qty-weighted average, used to reverse sellable
        returns at the SAME blended rate actually charged that month (return-disposition
        data is only available at month granularity, not per-day, so we can't know
        exactly which day's units came back)."""
        batches = sku_batches_map.get(sku)
        if not batches or not month_units:
            rate = LANDED_COST.get(sku, 0.0)
            return rate * month_units, rate
        days = agg["sku_day"].get(sku, {})
        total_units, total_cost = 0, 0.0
        for day, v in days.items():
            if v["units"] <= 0 or not day.startswith(m):
                continue
            total_units += v["units"]
            total_cost += v["units"] * (wac_as_of(batches, day) or 0.0)
        if total_units == 0:
            # sku_day missing data sku_month has units for (shouldn't happen per
            # aggregate()'s sku_day/sku_month docstring) - fail safe to the flat rate.
            rate = LANDED_COST.get(sku, 0.0)
            return rate * month_units, rate
        return total_cost, (total_cost / total_units)

    sku_rows = []
    for sku, tot in agg["sku_totals"].items():
        if tot["revenue"] == 0 and tot["units"] == 0:
            continue
        net_rev = tot["revenue"] + tot["refunds"]
        net_fees = tot["fees"] + tot["refund_fees"] + tot["adjustments"]
        is_est = sku not in LANDED_COST

        # Same proportional refund split as month_refund_split(), applied per SKU-month
        # then summed to YTD - see that function's docstring for the "no evidence ->
        # non-sellable" rule. gross_cogs/cogs_reversed are ALSO summed from months_detail
        # (not recomputed flat from tot["units"]) - the exact same "sum-of-months must
        # equal YTD" discipline used for refunds above, now extended to COGS so a
        # mid-year WAC change can't produce a months-vs-YTD tie-out mismatch.
        refunds_sellable_total = 0.0
        refunds_non_sellable_total = 0.0
        gross_cogs_total = 0.0
        cogs_reversed_total = 0.0

        months_detail = {}
        # Union of months with sales AND months with only a refund (e.g. a return posted
        # in a later calendar month than the original sale, with zero units sold that
        # month for this SKU) - a sales-only iteration silently drops that refund's
        # sellable/non-sellable split from the YTD total (real bug, caught 2026-09-05:
        # summed refunds_non_sellable across SKUs was 149.00 AED short of the ytd total).
        sku_months_all = set(agg["sku_month"].get(sku, {}).keys()) | set(agg["refund_sku_month"].get(sku, {}).keys())
        for m in sku_months_all:
            if m not in months:
                continue
            v = agg["sku_month"].get(sku, {}).get(m, {"revenue": 0.0, "units": 0, "fees": 0.0, "adjustments": 0.0})
            m_gross = v["revenue"]
            m_refund = agg["refund_sku_month"].get(sku, {}).get(m, 0.0)
            m_net_rev = m_gross + m_refund
            m_fees = v["fees"] + agg["refund_fees_sku_month"].get(sku, {}).get(m, 0.0) + v.get("adjustments", 0.0)
            m_sellable_returns = sellable_by_sku_month.get((sku, m), {}).get("sellable", 0)
            if is_est:
                m_cogs = COGS_ESTIMATE_PCT_OF_NET_REVENUE * m_net_rev
                m_gross_cogs = m_cogs
                m_cogs_reversed = 0.0  # no reversal basis for estimated SKUs (unchanged)
            else:
                m_gross_cogs, m_effective_rate = month_gross_cogs_and_rate(sku, m, v["units"])
                m_cogs_reversed = m_effective_rate * m_sellable_returns
                m_cogs = m_gross_cogs - m_cogs_reversed
            gross_cogs_total += m_gross_cogs
            cogs_reversed_total += m_cogs_reversed
            m_counts = sellable_by_sku_month.get((sku, m), {"sellable": 0, "non_sellable": 0})
            m_total_returns = m_counts["sellable"] + m_counts["non_sellable"]
            m_sellable_frac = (m_counts["sellable"] / m_total_returns) if m_total_returns else 0.0
            m_refund_sellable = m_refund * m_sellable_frac
            m_refund_non_sellable = m_refund * (1 - m_sellable_frac)
            refunds_sellable_total += m_refund_sellable
            refunds_non_sellable_total += m_refund_non_sellable
            m_net_sales = m_gross + m_refund_sellable
            m_margin_new = m_net_sales - m_cogs
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
                "refunds_sellable": round(m_refund_sellable, 2),
                "refunds_non_sellable": round(m_refund_non_sellable, 2),
                "net_revenue": round(m_net_rev, 2), "cogs": round(m_cogs, 2),
                "net_sales": round(m_net_sales, 2),
                "gross_cogs": round(m_gross_cogs, 2),
                "cogs_reversed_sellable": round(m_cogs_reversed, 2),
                "gross_margin": round(m_margin, 2),
                "margin_pct": round(m_margin / m_net_rev * 100, 1) if m_net_rev else None,
                "gross_margin_new": round(m_margin_new, 2),
                "margin_pct_new": round(m_margin_new / m_net_sales * 100, 1) if m_net_sales else None,
                "amazon_fees": round(m_fees, 2), "ad_spend_allocated": round(m_ad_alloc, 2),
                "storage_allocated": round(m_storage_alloc, 2),
                "net_profit": round(m_net_profit, 2),
                "net_margin_pct": round(m_net_profit / m_net_rev * 100, 1) if m_net_rev else None,
                "net_margin_pct_new": round(m_net_profit / m_net_sales * 100, 1) if m_net_sales else None,
                "asp": round(m_net_rev / v["units"], 2) if v["units"] else None,
                "gross_asp": round(m_gross / v["units"], 2) if v["units"] else None,
            }

        # Derived from the sum of months_detail, NOT recomputed flat from tot["units"]
        # (see the note above the months_detail loop) - this is what makes cost_per_unit
        # the SKU's actual YTD-blended rate rather than a fixed constant, so it moves
        # when a mid-year batch (like the 04-Sep-2026 UK shipment) changes the WAC.
        gross_cogs = gross_cogs_total
        cogs_reversed_sellable = cogs_reversed_total
        cogs = gross_cogs - cogs_reversed_sellable
        gross_margin = net_rev - cogs
        rev_share = tot["revenue"] / total_gross if total_gross else 0
        ad_alloc = total_ad * rev_share
        storage_alloc = total_storage * rev_share
        net_profit = gross_margin + net_fees + ad_alloc + storage_alloc
        if tot["units"]:
            cost_per_unit = (gross_cogs / tot["units"]) if not is_est else (COGS_ESTIMATE_PCT_OF_NET_REVENUE * net_rev / tot["units"])
        else:
            cost_per_unit = 0.0

        net_sales = tot["revenue"] + refunds_sellable_total
        gross_margin_new = net_sales - cogs
        sku_rows.append({
            "sku": sku, "gross_revenue": round(tot["revenue"], 2), "units": tot["units"],
            "refunds": round(tot["refunds"], 2),
            "refunds_sellable": round(refunds_sellable_total, 2),
            "refunds_non_sellable": round(refunds_non_sellable_total, 2),
            "net_revenue": round(net_rev, 2), "cogs": round(cogs, 2), "cogs_is_estimated": is_est,
            "net_sales": round(net_sales, 2),
            "gross_cogs": round(gross_cogs, 2),
            "cogs_reversed_sellable": round(cogs_reversed_sellable, 2),
            "gross_margin": round(gross_margin, 2),
            "margin_pct": round(gross_margin / net_rev * 100, 1) if net_rev else None,
            "gross_margin_new": round(gross_margin_new, 2),
            "margin_pct_new": round(gross_margin_new / net_sales * 100, 1) if net_sales else None,
            "amazon_fees": round(net_fees, 2), "ad_spend_allocated": round(ad_alloc, 2),
            "storage_allocated": round(storage_alloc, 2),
            "net_profit": round(net_profit, 2),
            "net_margin_pct": round(net_profit / net_rev * 100, 1) if net_rev else None,
            "net_margin_pct_new": round(net_profit / net_sales * 100, 1) if net_sales else None,
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
        # Landed cost shown here is the CURRENT running WAC (today's blended cost of
        # what's actually sitting in stock, per inventory_cost_ledger.json), not the
        # flat formula result - added 2026-09-06 so this "recommended ASP"/margin-check
        # reference reflects a revised batch (e.g. the 04-Sep-2026 UK shipment) going
        # forward. For SKUs with no ledger batch this is identical to the old figure.
        sku_current_wac = current_wac.get(sku) if not is_est else None
        landed_aed = comp["landed_cost_aed"] if (is_est or sku_current_wac is None) else sku_current_wac
        landed_usd = (landed_aed / AED_USD_PEG) if landed_aed is not None else None
        wac_revised = (not is_est and comp["landed_cost_aed"] is not None and landed_aed is not None
                       and abs(landed_aed - comp["landed_cost_aed"]) > 0.01)
        recommended_asp_60 = (landed_aed / 0.40) if landed_aed else None
        cost_table.append({
            "sku": sku, "is_estimated": is_est,
            "product_cost_usd": round(comp["product_cost_usd"], 2) if comp["product_cost_usd"] is not None else None,
            "freight_usd": round(comp["freight_usd"], 2) if comp["freight_usd"] is not None else None,
            "duty_usd": round(comp["duty_usd"], 2) if comp["duty_usd"] is not None else None,
            "vat_usd": round(comp["vat_usd"], 2) if comp["vat_usd"] is not None else None,
            "landed_cost_usd": round(landed_usd, 2) if landed_usd is not None else None,
            "landed_cost": round(landed_aed, 2) if landed_aed is not None else None,
            "recommended_asp_60pct_margin": round(recommended_asp_60, 2) if recommended_asp_60 else None,
            "actual_asp": round(actual_asp, 2) if actual_asp is not None else None,
            "wac_revised": wac_revised,
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
        net_sales_m = gross + refunds_sellable  # "Net sales" P&L view - only sellable returns netted against
        # top-line revenue; non-sellable returns are deferred to the "Damages" line below COGS instead.
        gross_cogs_m = 0.0
        cogs_reversed_m = 0.0
        cogs_m = 0.0
        for sku, mv in agg["sku_month"].items():
            v = mv.get(m)
            if not v:
                continue
            if sku in LANDED_COST:
                m_sellable = sellable_by_sku_month.get((sku, m), {}).get("sellable", 0)
                m_gross, m_rate = month_gross_cogs_and_rate(sku, m, v["units"])
                gross_cogs_m += m_gross
                cogs_reversed_m += m_rate * m_sellable
                cogs_m += m_gross - (m_rate * m_sellable)
            else:
                # No per-unit landed cost basis for estimated SKUs, so no gross/reversed split
                # is possible - contributes to gross_cogs only, same as it always has to cogs_m.
                sku_net_rev_m = v["revenue"] + agg["refund_sku_month"].get(sku, {}).get(m, 0.0)
                est_cogs = COGS_ESTIMATE_PCT_OF_NET_REVENUE * sku_net_rev_m
                gross_cogs_m += est_cogs
                cogs_m += est_cogs
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
            "net_sales": round(net_sales_m, 2), "gross_cogs": round(gross_cogs_m, 2),
            "cogs_reversed_sellable": round(cogs_reversed_m, 2),
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
        "net_sales": round(sum(r["net_sales"] for r in monthly_rows), 2),
        "cogs": round(sum(r["cogs"] for r in monthly_rows), 2),
        "gross_cogs": round(sum(r["gross_cogs"] for r in monthly_rows), 2),
        "cogs_reversed_sellable": round(sum(r["cogs_reversed_sellable"] for r in monthly_rows), 2),
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
    # cogs_reversed pulled from sku_rows (already WAC-aware, summed from months_detail)
    # rather than recomputed flat here - keeps this table's total tied to gross_cogs -
    # cogs on every other table, instead of drifting once a SKU has more than one batch.
    sku_cogs_reversed_ytd = {r["sku"]: r["cogs_reversed_sellable"] for r in sku_rows}
    returns_breakdown = []
    for sku, counts in sellable_by_sku.items():
        sellable = counts["sellable"]
        non_sellable = counts["non_sellable"]
        if sellable == 0 and non_sellable == 0:
            continue
        cogs_reversed = sku_cogs_reversed_ytd.get(sku)
        disp = disposition_by_sku.get(sku, {"customer_damaged": 0, "defective": 0, "amazon_damaged": 0, "other_non_sellable": 0})
        returns_breakdown.append({
            "sku": sku, "sellable": sellable, "non_sellable": non_sellable,
            "customer_damaged": disp["customer_damaged"], "defective": disp["defective"],
            "amazon_damaged": disp["amazon_damaged"], "other_non_sellable": disp["other_non_sellable"],
            "total_returns": sellable + non_sellable,
            "cogs_reversed": round(cogs_reversed, 2) if cogs_reversed is not None else None,
        })
    returns_breakdown.sort(key=lambda r: -r["total_returns"])
    cogs_reversed_ytd = round(sum(r["cogs_reversed"] or 0 for r in returns_breakdown), 2)

    # SKU Analysis tab (added 2026-09-05): units sold per SKU per calendar day, with the
    # implied price point that day (gross revenue / units that day - not returns-
    # adjusted, matching the ASP fix elsewhere: a day's PRICE isn't affected by a refund
    # that might post on a different day). Only includes days with units > 0 - a day
    # with zero sales for a SKU is just absent, not a zero-value row.
    daily_sales = {}
    for sku, days in agg["sku_day"].items():
        rows_for_sku = []
        for day, v in days.items():
            if v["units"] <= 0:
                continue
            rows_for_sku.append({
                "date": day, "units": v["units"], "revenue": round(v["revenue"], 2),
                "price": round(v["revenue"] / v["units"], 2) if v["units"] else None,
            })
        rows_for_sku.sort(key=lambda r: r["date"])
        daily_sales[sku] = rows_for_sku

    # Inventory Movement & Costing tab (added 2026-09-06): the full batch history behind
    # each SKU's running WAC - opening balance (existing LANDED_COST, qty backed out of
    # today's live tracked inventory) plus any dated batches from inventory_cost_ledger.json,
    # with a running WAC recomputed after each. "Ageing" here is time since the SKU's most
    # recent batch, not per-unit lot age (this is a blended-WAC model, not FIFO - see
    # sku_cost_batches()). Every SKU in LANDED_COST is included, even ones with only the
    # opening batch, so this doubles as a full-catalog "last restocked" view.
    cost_ledger_report = []
    for sku in LANDED_COST:
        batches = sku_batches_map.get(sku, [])
        if not batches:
            continue
        running = []
        cum_qty, cum_val = 0, 0.0
        for b in batches:
            cum_qty += b["qty"]
            cum_val += b["qty"] * b["landed_cost_aed"]
            running.append({
                "date": b["date"], "qty": b["qty"],
                "landed_cost_aed": round(b["landed_cost_aed"], 2),
                "running_qty": cum_qty,
                "running_wac_aed": round(cum_val / cum_qty, 2) if cum_qty else None,
                "note": b.get("note", ""),
            })
        last_batch_date = batches[-1]["date"]
        days_since_last_batch = (
            datetime.datetime.utcnow() - datetime.datetime.strptime(last_batch_date, "%Y-%m-%d")
        ).days
        cost_ledger_report.append({
            "sku": sku,
            "current_wac_aed": round(current_wac[sku], 2) if current_wac.get(sku) is not None else None,
            "last_batch_date": last_batch_date,
            "days_since_last_batch": days_since_last_batch,
            "batches": running,
        })
    cost_ledger_report.sort(key=lambda r: r["days_since_last_batch"])

    # FBA Reimbursements (added 2026-09-06, user asked whether Amazon-caused warehouse
    # damage could be identified for reimbursement claims - this report is the actual
    # source for that, NOT the Customer Returns disposition field above, which only
    # covers customer-initiated returns). Kept as its own small list rather than folded
    # into returns_breakdown - a reimbursement isn't a return, and the two reports use
    # different keys (reimbursement has no reliable per-return-month bucketing).
    reimbursements = []
    for row in (reimbursement_rows or []):
        amount = row.get("amount-total") or "0"
        try:
            amount = round(float(amount), 2)
        except ValueError:
            amount = 0.0
        reimbursements.append({
            "date": (row.get("approval-date") or "")[:10],
            "sku": row.get("sku") or "UNKNOWN",
            "reason": row.get("reason") or "",
            "condition": row.get("condition") or "",
            "amount_aed": amount,
            "quantity": int(row.get("quantity-reimbursed-total") or 0),
        })
    reimbursements.sort(key=lambda r: r["date"], reverse=True)
    reimbursements_ytd = round(sum(r["amount_aed"] for r in reimbursements), 2)

    # FBA Removal Orders (added 2026-09-06, user asked where Amazon's ageing-threshold
    # auto-returns to warehouse are captured). order-source distinguishes Amazon's own
    # aged-inventory auto-removal from the seller's unfulfillable auto-removal setting
    # and manual removals - see REMOVAL_ORDER_SOURCE_LABELS/fetch_removal_orders_report
    # docstring. Kept as individual removal orders (not summed per SKU) since a SKU can
    # have multiple orders over the year with different sources/dispositions/statuses.
    removal_orders = []
    for row in (removal_order_rows or []):
        source_raw = row.get("order-source") or ""
        removal_orders.append({
            "date": (row.get("request-date") or "")[:10],
            "sku": row.get("sku") or "UNKNOWN",
            "source": REMOVAL_ORDER_SOURCE_LABELS.get(source_raw, source_raw),
            "is_aged": source_raw == "Amazon-initiated Automated Aged fulfillable Removal System",
            "status": row.get("order-status") or "",
            "disposition": row.get("disposition") or "",
            "requested_qty": int(row.get("requested-quantity") or 0),
            "shipped_qty": int(row.get("shipped-quantity") or 0),
            "in_process_qty": int(row.get("in-process-quantity") or 0),
            "cancelled_qty": int(row.get("cancelled-quantity") or 0),
        })
    removal_orders.sort(key=lambda r: r["date"], reverse=True)

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
        "daily_sales": daily_sales,
        "cost_ledger": cost_ledger_report,
        "reimbursements": reimbursements,
        "reimbursements_ytd": reimbursements_ytd,
        "removal_orders": removal_orders,
        "inventory_ageing": inventory_ageing,
        "inventory_ageing_totals": ageing_totals,
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

    print("Fetching FBA reimbursements report...")
    reimbursement_rows = fetch_reimbursements_report(token, start, now)
    print(f"  {len(reimbursement_rows)} reimbursement records")

    print("Fetching FBA removal orders report...")
    removal_order_rows = fetch_removal_orders_report(token, start, now)
    print(f"  {len(removal_order_rows)} removal order records")

    agg = aggregate(finance_pages)
    data = build_dashboard_data(agg, orders, inventory, months, returns_rows, reimbursement_rows, removal_order_rows)

    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved {OUT_PATH}")
    print(f"YTD gross revenue: {data['ytd']['gross_revenue']}, net profit: {data['ytd']['net_profit']}")


if __name__ == "__main__":
    main()
