# -*- coding: utf-8 -*-
"""
One-time (but safely re-runnable) structural edit: adds the Amazon/Noon/Overall
Channel switcher to dashboard/neuronext_amazon_dashboard.html, per the user's
2026-10-03 request ("one neuronext dashboard... amazon, noon and overall
sections", not three separate dashboards).

Everything this script inserts is wrapped in HTML/JS comment markers
(CHANNEL_*_START/END) so re-running it replaces its own prior output instead
of duplicating it - same idempotency principle as splice_and_validate.py's
anchor-based replacement, just scoped to one block per section instead of
many small ones, since this content is new (not refreshed piecemeal by the
existing Amazon pipeline).

Data refresh on subsequent days: the noonData/overallData JS consts this
script writes are NOT re-derived by splice_and_validate.py - update
splice_and_validate.py separately to replace them from dashboard_data_combined.json
on each pipeline run (see SKILL.md). Running THIS script again only re-applies
the structural markup/JS; pass --data-only to refresh just the embedded
noonData/overallData JSON without touching structure (used by the daily
pipeline instead of re-running the full structural injection).

Run standalone: python inject_unified_dashboard.py [--data-only]
"""
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.environ.get(
    "DASHBOARD_HTML_PATH", os.path.join(os.path.dirname(SCRIPT_DIR), "dashboard", "neuronext_amazon_dashboard.html"))
COMBINED_PATH = os.environ.get("COMBINED_DATA_OUT", os.path.join(SCRIPT_DIR, "dashboard_data_combined.json"))


def strip_between(html, start_marker, end_marker):
    pattern = re.escape(start_marker) + r".*?" + re.escape(end_marker)
    return re.sub(pattern, "", html, flags=re.DOTALL)


def load_data():
    with open(COMBINED_PATH, encoding="utf-8") as f:
        combined = json.load(f)
    noon, overall = combined["noon"], combined["overall"]
    noon_slim = {
        "months": noon["months"], "monthly": noon["monthly"], "ytd": noon["ytd"],
        "sku_rows": noon["sku_rows"], "inventory": noon["inventory"], "cost_table": noon["cost_table"],
    }
    overall_slim = {
        "months": overall["months"], "monthly": overall["monthly"], "ytd": overall["ytd"],
        "sku_rows": overall["sku_rows"], "inventory": overall["inventory"], "cost_table": overall["cost_table"],
    }
    return noon_slim, overall_slim, combined["generated_at"]


CSS_BLOCK = """
  /* CHANNEL_CSS_START */
  /* ---------- CHANNEL SWITCHER (Amazon / Noon / Overall) ---------- */
  .channel-nav { display: flex; gap: 6px; margin-bottom: 4px; }
  .channel-nav-btn {
    font: inherit; font-size: 13.5px; font-weight: 800; color: var(--ink-2);
    background: var(--surface-2); border: 1px solid var(--border-strong); border-radius: 10px;
    padding: 10px 20px; cursor: pointer; transition: background .12s ease, color .12s ease;
    box-shadow: var(--shadow-card);
  }
  .channel-nav-btn:hover { background: var(--border-strong); }
  .channel-nav-btn.active { background: var(--ink); color: #fff; border-color: var(--ink); }
  .channel-panel { display: none; flex-direction: column; gap: 22px; }
  .channel-panel.active { display: flex; }
  .channel-note { font-size: 12px; color: var(--ink-muted); background: var(--surface-2); border-radius: 8px; padding: 10px 12px; }
  /* Noon/Overall Product-wise Profitability: 4 frozen leading columns (SKU/Qty/ASP/
     Cost-per-unit) - same pattern as the Amazon tab's .profit-frozen, just one column
     fewer (no Total Inv Qty), so the width/left offsets differ. */
  table.profit-frozen-4 { table-layout: fixed; }
  table.profit-frozen-4 th:nth-child(1), table.profit-frozen-4 td:nth-child(1) { width: 210px; left: 0; text-align: left; padding-left: 0; color: var(--ink); font-weight: 500; overflow: hidden; text-overflow: ellipsis; box-shadow: none; }
  table.profit-frozen-4 th:nth-child(2), table.profit-frozen-4 td:nth-child(2) { width: 66px; left: 210px; box-shadow: none; }
  table.profit-frozen-4 th:nth-child(3), table.profit-frozen-4 td:nth-child(3) { width: 66px; left: 276px; box-shadow: none; }
  table.profit-frozen-4 th:nth-child(4), table.profit-frozen-4 td:nth-child(4) { width: 76px; left: 342px; box-shadow: 2px 0 4px -2px rgba(15,23,42,0.15); }
  table.profit-frozen-4 th:nth-child(-n+4), table.profit-frozen-4 td:nth-child(-n+4) { position: sticky; z-index: 2; background: var(--surface); }
  table.profit-frozen-4 th { white-space: normal; line-height: 1.25; vertical-align: bottom; padding-bottom: 6px; }
  /* CHANNEL_CSS_END */
"""

NAV_HTML = """
  <!-- CHANNEL_NAV_START -->
  <nav class="channel-nav">
    <button class="channel-nav-btn active" id="channelBtnOverall" type="button">Overall</button>
    <button class="channel-nav-btn" id="channelBtnAmazon" type="button">Amazon</button>
    <button class="channel-nav-btn" id="channelBtnNoon" type="button">Noon</button>
  </nav>
  <!-- CHANNEL_NAV_END -->
"""


def noon_overall_html():
    return """
  <!-- CHANNEL_NOON_OVERALL_START -->
  <div class="channel-panel" id="channelNoon">
    <nav class="tab-nav">
      <button class="tab-nav-btn active" id="noonTabBtnPnl" type="button">PNL</button>
      <button class="tab-nav-btn" id="noonTabBtnInventory" type="button">Inventory</button>
    </nav>
    <div class="tab-panel active" id="noonTabPnl">
      <section class="kpis" id="noonKpis"></section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Monthly revenue, profit before ads &amp; net profit</span>
          <span class="section-note">AED &middot; from Noon's finance/transaction export</span>
        </div>
        <div class="legend">
          <span class="legend-item"><span class="swatch" style="background:var(--series-1)"></span>Gross revenue</span>
          <span class="legend-item"><span class="swatch" style="background:var(--series-2)"></span>Profit before ads</span>
          <span class="legend-item"><span class="swatch" style="background:var(--good)"></span>Net profit (positive)</span>
          <span class="legend-item"><span class="swatch" style="background:var(--critical)"></span>Net profit (loss)</span>
        </div>
        <div class="trend-chart" id="noonTrendChart"></div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Where the revenue goes</span>
          <span class="section-note">Gross revenue &rarr; net proceeds (fees/ads only, excludes COGS), YTD</span>
        </div>
        <div class="waterfall" id="noonWaterfall"></div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Noon Monthwise Profitability</span>
          <span class="section-note">AED, monthly with YTD total &mdash; from Noon's finance/transaction export. "Other fees" is broken down into its components below (indented): referral fee, fulfillment &amp; logistics fee, shipping credits, other order fees, order/non-order subsidies, and other adjustments. COGS reuses the exact same landed-cost figures as the Amazon tab (same physical product, same NEXTWAVE invoices) via the confirmed Noon&harr;Amazon SKU mapping. Advertising Fee is Noon's own ad-spend line, real not allocated.</span>
        </div>
        <div class="overflow-x">
          <table class="fee-table">
            <thead><tr id="noonMonthlyHeaderRow"><th>Line item</th><th class="ytd">YTD Total</th></tr></thead>
            <tbody id="noonMonthlyBody"></tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Revenue by SKU</span>
          <span class="section-note">Units / AED revenue by month, YTD</span>
        </div>
        <div class="mom-key">
          <span><span class="mom-swatch" style="background:var(--good-soft)"></span>Up vs. prior month</span>
          <span><span class="mom-swatch" style="background:var(--critical-soft)"></span>Down vs. prior month</span>
        </div>
        <div class="overflow-x">
          <table class="sku-table channel-sku-matrix">
            <thead><tr id="noonSkuMatrixHeaderRow"><th>SKU</th></tr></thead>
            <tbody id="noonSkuMatrixBody"></tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Noon product-wise profitability</span>
          <span class="section-note">AED &mdash; net of refunds (Noon reports none separately - see below). ASP/Cost-per-unit are per-unit; everything else is aggregate for the selected period. COGS uses the exact same landed cost as the Amazon tab for the same physical SKU. Fees (referral + fulfillment) are real per-order-item data. Ad spend is allocated pro-rata by each SKU's share of that period's gross revenue (Noon's export only reports ad spend in aggregate, not per SKU - same limitation Amazon's own ad-allocation column has). Noon's finance export has no sellable/non-sellable return-disposition data the way Amazon's FBA Customer Returns report does, so there's no separate Returns/Damages column here - not an oversight, a real data gap.</span>
        </div>
        <div class="filter-bar" id="noonProfitFilterBar"></div>
        <div class="overflow-x">
          <table class="fee-table profit-frozen-4" style="min-width:1500px">
            <thead>
              <tr>
                <th>SKU</th><th>Qty sold</th><th>ASP</th><th>Cost/unit</th>
                <th>Gross revenue</th><th>COGS</th><th>Gross margin</th><th class="pct">Gross margin %</th>
                <th>Fees</th><th>Ad spend (alloc.)</th>
                <th class="ytd">Net profit</th><th class="pct">Net margin %</th>
              </tr>
            </thead>
            <tbody id="noonProfitTableBody"></tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Cost per unit &amp; recommended ASP</span>
          <span class="section-note">Product cost, freight, duty and VAT are IDENTICAL to the Amazon tab's copy of this table (same physical product, same NEXTWAVE invoices, same landed-cost formula - cost doesn't change by sales channel). Only Actual ASP and the margin computed from it are Noon-specific - Noon's own average selling price, not Amazon's.</span>
        </div>
        <div class="overflow-x">
          <table class="fee-table" style="min-width:1080px">
            <thead><tr><th>SKU</th><th>Product cost (USD)</th><th>Freight (USD)</th><th>Duty (USD)</th><th>VAT (USD)</th><th>Landed cost/unit (USD)</th><th class="ytd">Landed cost/unit (AED)</th><th class="pct">Recommended ASP (60% margin)</th><th class="pct">Actual ASP (Noon)</th><th class="pct">Margin % at actual ASP</th></tr></thead>
            <tbody id="noonCostTableBody"></tbody>
          </table>
        </div>
      </section>
    </div>
    <div class="tab-panel" id="noonTabInventory">
      <section class="card">
        <div class="section-head">
          <span class="section-title">Noon inventory</span>
          <span class="section-note">Noon Inv is stock physically held at Noon's own fulfillment centers (FBN), live from Noon's inventory-ageing export. WH Inv is the SAME shared "Neuronext SOH &amp; Outbound" warehouse figure shown on the Amazon tab &mdash; one shared pool, not a separate Noon warehouse. Only SKUs actually listed on Noon are shown here (most Amazon SKUs aren't listed on Noon at all). Inventory Ageing and Inventory Costing are channel-agnostic &mdash; see the Amazon tab for those (same figures, not duplicated here).</span>
        </div>
        <div class="overflow-x">
          <table class="inv-table">
            <thead><tr><th>SKU</th><th>Noon Inv</th><th>WH Inv</th><th class="ytd">Total Inv</th><th>Units sold YTD</th><th>Avg daily sales</th><th>Reorder point (90d)</th><th>Days of cover</th><th>Suggested reorder qty</th><th>Status</th></tr></thead>
            <tbody id="noonInvBody"></tbody>
          </table>
        </div>
      </section>
    </div>
  </div>

  <div class="channel-panel active" id="channelOverall">
    <nav class="tab-nav">
      <button class="tab-nav-btn active" id="overallTabBtnPnl" type="button">PNL</button>
      <button class="tab-nav-btn" id="overallTabBtnInventory" type="button">Inventory</button>
    </nav>
    <div class="tab-panel active" id="overallTabPnl">
      <section class="kpis" id="overallKpis"></section>
      <div class="channel-note" id="overallInvNote">Inventory Ageing Matrix and Inventory Costing/WAC are channel-agnostic (same landed cost and same shared-warehouse FIFO ageing regardless of which channel sells the unit) &mdash; see the Amazon tab for those figures rather than a duplicate here.</div>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Monthly revenue, profit before ads &amp; net profit (Amazon + Noon)</span>
          <span class="section-note">AED &middot; combined from both channels' settlement/finance data</span>
        </div>
        <div class="legend">
          <span class="legend-item"><span class="swatch" style="background:var(--series-1)"></span>Gross revenue</span>
          <span class="legend-item"><span class="swatch" style="background:var(--series-2)"></span>Profit before ads</span>
          <span class="legend-item"><span class="swatch" style="background:var(--good)"></span>Net profit (positive)</span>
          <span class="legend-item"><span class="swatch" style="background:var(--critical)"></span>Net profit (loss)</span>
        </div>
        <div class="trend-chart" id="overallTrendChart"></div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Where the revenue goes</span>
          <span class="section-note">Gross revenue &rarr; net proceeds (Amazon fees/refunds + Noon fees + ads, excludes COGS), YTD</span>
        </div>
        <div class="waterfall" id="overallWaterfall"></div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Overall Monthwise Profitability (Amazon + Noon)</span>
          <span class="section-note">AED, monthly with YTD total. Gross revenue/COGS/ad spend/net profit are simple sums of the Amazon and Noon tabs. "Other costs" is shown per-channel rather than force-merged into one schema &mdash; Amazon's and Noon's fee/refund data aren't structured the same way (e.g. Noon's finance export has no sellable/non-sellable refund-disposition split the way Amazon's FBA Customer Returns report does), so combining them into one breakdown would fabricate detail neither channel actually reports.</span>
        </div>
        <div class="overflow-x">
          <table class="fee-table">
            <thead><tr id="overallMonthlyHeaderRow"><th>Line item</th><th class="ytd">YTD Total</th></tr></thead>
            <tbody id="overallMonthlyBody"></tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Revenue by SKU (Amazon + Noon)</span>
          <span class="section-note">Units / AED revenue by month, YTD &mdash; combined by physical SKU via the confirmed Noon&harr;Amazon SKU mapping</span>
        </div>
        <div class="mom-key">
          <span><span class="mom-swatch" style="background:var(--good-soft)"></span>Up vs. prior month</span>
          <span><span class="mom-swatch" style="background:var(--critical-soft)"></span>Down vs. prior month</span>
        </div>
        <div class="overflow-x">
          <table class="sku-table channel-sku-matrix">
            <thead><tr id="overallSkuMatrixHeaderRow"><th>SKU</th></tr></thead>
            <tbody id="overallSkuMatrixBody"></tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Overall product-wise profitability (Amazon + Noon)</span>
          <span class="section-note">AED, combined by physical SKU. Two SKUs here (RH1008BSZ, RH228DBPZ) have real 2026 Noon sales but zero Amazon sales YTD &mdash; they appear only because of Noon. COGS is the same landed cost regardless of channel. Fees = Amazon's (commission+fulfillment+storage) + Noon's (referral+fulfillment) combined. Ad spend is each channel's own allocation, summed.</span>
        </div>
        <div class="filter-bar" id="overallProfitFilterBar"></div>
        <div class="overflow-x">
          <table class="fee-table profit-frozen-4" style="min-width:1500px">
            <thead>
              <tr>
                <th>SKU</th><th>Qty sold</th><th>ASP</th><th>Cost/unit</th>
                <th>Gross revenue</th><th>COGS</th><th>Gross margin</th><th class="pct">Gross margin %</th>
                <th>Fees</th><th>Ad spend (alloc.)</th>
                <th class="ytd">Net profit</th><th class="pct">Net margin %</th>
              </tr>
            </thead>
            <tbody id="overallProfitTableBody"></tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <div class="section-head">
          <span class="section-title">Cost per unit &amp; recommended ASP</span>
          <span class="section-note">Product cost, freight, duty and VAT are IDENTICAL to the Amazon tab's copy of this table (cost doesn't change by sales channel). Actual ASP here is blended across BOTH channels (combined revenue &divide; combined units), unlike the Amazon and Noon tabs' own copies which each show that one channel's ASP.</span>
        </div>
        <div class="overflow-x">
          <table class="fee-table" style="min-width:1080px">
            <thead><tr><th>SKU</th><th>Product cost (USD)</th><th>Freight (USD)</th><th>Duty (USD)</th><th>VAT (USD)</th><th>Landed cost/unit (USD)</th><th class="ytd">Landed cost/unit (AED)</th><th class="pct">Recommended ASP (60% margin)</th><th class="pct">Actual ASP (blended)</th><th class="pct">Margin % at actual ASP</th></tr></thead>
            <tbody id="overallCostTableBody"></tbody>
          </table>
        </div>
      </section>
    </div>
    <div class="tab-panel" id="overallTabInventory">
      <section class="card">
        <div class="section-head">
          <span class="section-title">Overall inventory (AZ + Noon + Warehouse)</span>
          <span class="section-note">All three physical stock pools added together: Amazon FBA (AZ Inv), Noon FBN (Noon Inv), and NeuroNext's own warehouse (WH Inv, shared between both channels).</span>
        </div>
        <div class="overflow-x">
          <table class="inv-table">
            <thead><tr><th>SKU</th><th>AZ Inv</th><th>Noon Inv</th><th>WH Inv</th><th class="ytd">Total Inv</th><th>Units sold YTD</th><th>Avg daily sales</th><th>Reorder point (90d)</th><th>Days of cover</th><th>Suggested reorder qty</th><th>Status</th></tr></thead>
            <tbody id="overallInvBody"></tbody>
          </table>
        </div>
      </section>
    </div>
  </div>
  <!-- CHANNEL_NOON_OVERALL_END -->
"""


def js_block(noon_slim, overall_slim):
    noon_json = json.dumps(noon_slim)
    overall_json = json.dumps(overall_slim)
    return """
  /* CHANNEL_JS_START */
  const noonData = """ + noon_json + """;
  const overallData = """ + overall_json + """;

  // ---------- CHANNEL SWITCHING ----------
  const channels = [
    { btn: "channelBtnAmazon", panel: "channelAmazon" },
    { btn: "channelBtnNoon", panel: "channelNoon" },
    { btn: "channelBtnOverall", panel: "channelOverall" },
  ];
  channels.forEach(c => {
    document.getElementById(c.btn).addEventListener("click", () => {
      channels.forEach(o => {
        document.getElementById(o.btn).classList.toggle("active", o === c);
        document.getElementById(o.panel).classList.toggle("active", o === c);
      });
    });
  });
  const subTabs = [
    [["noonTabBtnPnl","noonTabPnl"],["noonTabBtnInventory","noonTabInventory"]],
    [["overallTabBtnPnl","overallTabPnl"],["overallTabBtnInventory","overallTabInventory"]],
  ];
  subTabs.forEach(group => {
    group.forEach(([btnId, panelId]) => {
      document.getElementById(btnId).addEventListener("click", () => {
        group.forEach(([b, p]) => {
          document.getElementById(b).classList.toggle("active", b === btnId);
          document.getElementById(p).classList.toggle("active", b === btnId);
        });
      });
    });
  });

  const monthShort = m => { const d = m.split("-"); return ["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][+d[1]]; };

  function renderChannelKpis(elId, ytd) {
    const el = document.getElementById(elId);
    const netCls = ytd.net_profit < 0 ? " neg" : "";
    el.innerHTML = `
      <div class="kpi accent-critical"><span class="label">Gross revenue (YTD)</span><span class="value">${fmt(ytd.gross_revenue)}</span></div>
      <div class="kpi${ytd.net_profit < 0 ? " accent-critical" : " accent-good"}"><span class="label">Net profit (YTD)</span><span class="value${netCls}">${ytd.net_profit < 0 ? "-" : ""}${fmt(Math.abs(ytd.net_profit))}</span></div>
      <div class="kpi accent-amber"><span class="label">Ad spend (YTD)</span><span class="value">${fmt(Math.abs(ytd.ad_spend || 0))}</span></div>
      <div class="kpi"><span class="label">Units (YTD)</span><span class="value">${ytd.units.toLocaleString()}</span></div>
      <div class="kpi"><span class="label">Revenue / day</span><span class="value">${fmt(ytd.revenue_per_day)}</span></div>
    `;
  }
  renderChannelKpis("noonKpis", noonData.ytd);
  renderChannelKpis("overallKpis", overallData.ytd);

  // ---------- Generic trend chart + waterfall (Noon / Overall) ----------
  // Same visual logic as the Amazon tab's own trend chart (makeTrendCol / waterfall
  // steps above), generalized to take any {month, revenue, profitBefore, profit}[]
  // array instead of Amazon's hardcoded `monthly`/`ytdBar` consts - the Amazon tab's
  // own chart code is untouched, this is a separate, parallel implementation so there
  // is zero risk of regressing it.
  function renderGenericTrend(containerId, rows, ytdRow) {
    const trendEl = document.getElementById(containerId);
    if (!trendEl) return;
    const aboveMax = Math.max(1, ...rows.map(d => Math.max(d.revenue, d.profitBefore, d.profit)));
    const belowMax = Math.max(0, ...rows.map(d => Math.max(0, -d.profit)));
    const totalSpan = aboveMax + belowMax;
    const baselineFrac = belowMax / totalSpan;

    const gridEl = document.createElement("div");
    gridEl.className = "trend-grid";
    trendEl.appendChild(gridEl);
    const baselineEl = document.createElement("div");
    baselineEl.className = "trend-baseline";
    baselineEl.style.bottom = (baselineFrac * 100) + "%";
    trendEl.appendChild(baselineEl);
    const colsEl = document.createElement("div");
    colsEl.className = "trend-cols";
    trendEl.appendChild(colsEl);

    function makeCol(d, span, base, isYtd) {
      const revH = (d.revenue / span) * 100;
      const beforeH = (d.profitBefore / span) * 100;
      const isNeg = d.profit < 0;
      const netH = (Math.abs(d.profit) / span) * 100;
      const wrap = document.createElement("div");
      wrap.className = "trend-month" + (isYtd ? " is-ytd" : "");
      const revBar = document.createElement("div");
      revBar.className = "bar revenue";
      revBar.style.bottom = (base * 100) + "%";
      revBar.style.height = revH + "%";
      const beforeBar = document.createElement("div");
      beforeBar.className = "bar profit-before";
      beforeBar.style.bottom = (base * 100) + "%";
      beforeBar.style.height = beforeH + "%";
      const netBar = document.createElement("div");
      netBar.className = "bar net-profit" + (isNeg ? " is-neg" : "");
      netBar.style.bottom = (isNeg ? ((base * 100) - netH) : (base * 100)) + "%";
      netBar.style.height = netH + "%";
      const tip = document.createElement("div");
      tip.className = "tooltip";
      tip.innerHTML = `
        <div class="t-row"><span><span class="t-swatch" style="background:var(--series-1)"></span>Revenue</span><b>&nbsp;${fmt(d.revenue)}</b></div>
        <div class="t-row"><span><span class="t-swatch" style="background:var(--series-2)"></span>Profit before ads</span><b>&nbsp;${fmt(d.profitBefore)}</b></div>
        <div class="t-row"><span><span class="t-swatch" style="background:${isNeg ? 'var(--critical)' : 'var(--good)'}"></span>Net profit</span><b>&nbsp;${fmt(d.profit)}</b></div>
      `;
      wrap.appendChild(tip);
      wrap.appendChild(revBar);
      wrap.appendChild(beforeBar);
      wrap.appendChild(netBar);
      return wrap;
    }
    rows.forEach(d => colsEl.appendChild(makeCol(d, totalSpan, baselineFrac, false)));
    const ytdAbove = Math.max(1, ytdRow.revenue, ytdRow.profitBefore, ytdRow.profit);
    const ytdBelow = Math.max(0, -ytdRow.profit);
    const ytdSpan = ytdAbove + ytdBelow;
    const ytdBase = ytdBelow / ytdSpan;
    colsEl.appendChild(makeCol(ytdRow, ytdSpan, ytdBase, true));

    const labelsEl = document.createElement("div");
    labelsEl.className = "trend-labels";
    rows.concat([ytdRow]).forEach(d => {
      const s = document.createElement("span");
      s.textContent = d.m;
      if (d === ytdRow) s.style.fontWeight = "800";
      labelsEl.appendChild(s);
    });
    trendEl.parentNode.insertBefore(labelsEl, trendEl.nextSibling);
  }

  function renderGenericWaterfall(containerId, steps) {
    const wfEl = document.getElementById(containerId);
    if (!wfEl) return;
    const wfMax = Math.max(1, ...steps.map(s => Math.abs(s.value)));
    steps.forEach(s => {
      const h = Math.max(6, (Math.abs(s.value) / wfMax) * 100);
      const isTotal = s.type.includes("total");
      const pct = (s.value / wfMax * 100);
      const pctStr = (pct >= 0 ? "" : "-") + Math.abs(pct).toFixed(1) + "%";
      const col = document.createElement("div");
      col.className = "wf-step";
      col.innerHTML = `
        <span class="wf-value ${s.value < 0 ? 'neg' : ''}">${s.value < 0 ? "-" : ""}${fmt(Math.abs(s.value))}</span>
        <span class="wf-pct ${s.value < 0 ? 'neg' : ''}">${pctStr}</span>
        <div class="wf-bar-col">
          <div class="wf-bar ${isTotal ? (s.type.includes('final') ? 'total final' : 'total') : 'drop'}" style="height:${h}%"></div>
        </div>
        <span class="wf-label">${s.label}</span>
      `;
      wfEl.appendChild(col);
    });
  }

  function noonTrendRows() {
    return noonData.monthly.map(m => ({
      m: monthShort(m.month), revenue: m.gross_revenue, profitBefore: m.net_profit - m.ad_spend, profit: m.net_profit,
    }));
  }
  renderGenericTrend("noonTrendChart", noonTrendRows(), {
    m: "YTD", revenue: noonData.ytd.gross_revenue, profitBefore: noonData.ytd.net_profit - noonData.ytd.ad_spend, profit: noonData.ytd.net_profit,
  });
  renderGenericWaterfall("noonWaterfall", [
    { label: "Gross revenue", value: noonData.ytd.gross_revenue, type: "total" },
    { label: "Fees & adjustments", value: noonData.ytd.other_fees, type: "drop" },
    { label: "Ad spend", value: noonData.ytd.ad_spend, type: "drop" },
    { label: "Net proceeds", value: noonData.ytd.net_proceeds, type: "total final" },
  ]);

  function overallTrendRows() {
    return overallData.monthly.map(m => ({
      m: monthShort(m.month), revenue: m.gross_revenue, profitBefore: m.net_profit - m.ad_spend, profit: m.net_profit,
    }));
  }
  renderGenericTrend("overallTrendChart", overallTrendRows(), {
    m: "YTD", revenue: overallData.ytd.gross_revenue, profitBefore: overallData.ytd.net_profit - overallData.ytd.ad_spend, profit: overallData.ytd.net_profit,
  });
  renderGenericWaterfall("overallWaterfall", [
    { label: "Gross revenue", value: overallData.ytd.gross_revenue, type: "total" },
    { label: "Amazon fees, refunds, storage", value: overallData.ytd.other_costs_amazon_incl_refunds, type: "drop" },
    { label: "Noon fees & adjustments", value: overallData.ytd.other_costs_noon, type: "drop" },
    { label: "Ad spend", value: overallData.ytd.ad_spend, type: "drop" },
    { label: "Net proceeds", value: overallData.ytd.net_proceeds, type: "total final" },
  ]);

  // Appends <th> elements to the EXISTING header <tr> (which already has the fixed
  // Line-item/YTD-Total columns) - fixed 2026-10-04: the previous version replaced a
  // tbody-tag placeholder that had been nested inside the <tr>, which is invalid HTML
  // (a tbody element can't be a child of a tr element); browsers silently relocate it
  // during parsing, which is what produced the garbled/misaligned header row the user
  // screenshotted. Appending directly to a real <tr> has no such hazard. (Note: do NOT
  // write out that removed tag name in angle brackets anywhere in this file's comments -
  // splice_and_validate.py's structural check does a naive text count of that tag's
  // open/close markers across the WHOLE file, comments included, and a bracketed prose
  // mention trips it exactly like the already-documented paren/quote false positives -
  // spell it as plain "tbody" with no brackets if you need to reference it again.)
  function monthlyHeaderRow(elId, months) {
    const tr = document.getElementById(elId);
    months.forEach(m => { const th = document.createElement("th"); th.textContent = monthShort(m); tr.appendChild(th); });
  }
  monthlyHeaderRow("noonMonthlyHeaderRow", noonData.months);
  monthlyHeaderRow("overallMonthlyHeaderRow", overallData.months);

  // Negative-value coloring (the "neg" class -> red, same as every Amazon-tab money
  // cell) is computed PER CELL from that cell's own sign - never hardcoded - same
  // convention as cellMoney's "ytd" + (d.net_profit < 0 ? " neg" : "") pattern above.
  const negCls = (v, base) => (base ? base + " " : "") + (v < 0 ? "neg" : "");
  function moneyRow(label, months, byMonth, ytdVal, cls) {
    const cells = months.map(m => cellN0(byMonth(m), negCls(byMonth(m) || 0, ""))).join("");
    return `<tr${cls ? ` class="${cls}"` : ""}><td>${label}</td>${cellN0(ytdVal, negCls(ytdVal, "ytd"))}${cells}</tr>`;
  }
  function subRow(label, months, byMonth) {
    const cells = months.map(m => cellN0(byMonth(m), negCls(byMonth(m) || 0, ""))).join("");
    const ytdVal = months.reduce((s, m) => s + (byMonth(m) || 0), 0);
    return `<tr class="sub-row"><td>${label}</td>${cellN0(ytdVal, negCls(ytdVal, "ytd"))}${cells}</tr>`;
  }

  function renderNoonMonthly() {
    const byMonth = key => m => { const row = noonData.monthly.find(r => r.month === m); return row ? row[key] : null; };
    const byCb = key => m => { const row = noonData.monthly.find(r => r.month === m); return row && row.cost_breakdown ? row.cost_breakdown[key] : null; };
    const rows = [];
    rows.push(moneyRow("Gross revenue", noonData.months, byMonth("gross_revenue"), noonData.ytd.gross_revenue, "total"));
    rows.push(moneyRow("COGS", noonData.months, m => -Math.abs(byMonth("cogs")(m) || 0), -Math.abs(noonData.ytd.cogs), ""));
    const gmByMonth = m => (byMonth("gross_revenue")(m) || 0) - (byMonth("cogs")(m) || 0);
    rows.push(moneyRow("Gross margin", noonData.months, gmByMonth, noonData.ytd.gross_revenue - noonData.ytd.cogs, "total"));
    rows.push(moneyRow("Other fees (referral, fulfillment, etc.)", noonData.months, m => (byMonth("net_before_fees")(m) || 0) - (byMonth("gross_revenue")(m) || 0),
      noonData.monthly.reduce((s, r) => s + (r.net_before_fees - r.gross_revenue), 0), ""));
    rows.push(subRow("Referral fee", noonData.months, byCb("referral")));
    rows.push(subRow("Fulfillment &amp; logistics fee", noonData.months, byCb("fulfillment")));
    rows.push(subRow("Shipping credits", noonData.months, byCb("shipping_credits")));
    rows.push(subRow("Other order fees", noonData.months, byCb("other_order_fees")));
    rows.push(subRow("Order subsidies", noonData.months, byCb("order_subsidies")));
    rows.push(subRow("Non-order subsidies", noonData.months, byCb("non_order_subsidies")));
    rows.push(subRow("Others", noonData.months, byCb("others")));
    rows.push(moneyRow("Advertising spend", noonData.months, byMonth("ad_spend"), noonData.ytd.ad_spend, ""));
    rows.push(moneyRow("Net profit", noonData.months, byMonth("net_profit"), noonData.ytd.net_profit, "total final"));
    document.getElementById("noonMonthlyBody").innerHTML = rows.join("\\n");
  }
  renderNoonMonthly();

  // SKU display name lookup, shared by Noon's and Overall's tables - built from
  // overallData.cost_table since that covers every Amazon SKU with a SKU_SHORT_NAME
  // (same lookup merge_dashboard_data.py's build_cost_table() already applied
  // server-side); falls back to the raw sku/partner_sku string if unmapped.
  const nameByAmzSku = Object.fromEntries(overallData.cost_table.map(c => [c.sku, c.name]));
  const displayName = r => nameByAmzSku[r.amazon_sku || r.sku] || r.amazon_sku || r.sku || r.partner_sku;

  // ---------- Generic Revenue-by-SKU matrix (Noon / Overall) ----------
  // Same visual pattern as the Amazon tab's own SKU matrix (MoM up/down coloring,
  // sku-name-cell/sku-code split) - generalized to take any months[]/rows[] instead of
  // Amazon's baked gen_sku_matrix.html fragment, since Noon/Overall numbers refresh via
  // this script's --data-only path, not a separate HTML fragment splice.
  function renderGenericSkuMatrix(headerRowId, bodyId, months, rows, getId) {
    // Header order MUST match the body row's cell order below (SKU, then each month,
    // then YTD Total LAST) - a static "YTD Total" <th> placed right after SKU (like the
    // Monthwise table's header) would misalign every column once the months are
    // appended, since the body always puts its YTD cell last, matching the Amazon tab's
    // own SKU matrix convention. Real bug, fixed 2026-10-04 (caught via user screenshot
    // showing YTD Total empty/misaligned here).
    const headerTr = document.getElementById(headerRowId);
    months.forEach(m => { const th = document.createElement("th"); th.textContent = monthShort(m); headerTr.appendChild(th); });
    const ytdTh = document.createElement("th");
    ytdTh.className = "ytd";
    ytdTh.textContent = "YTD Total";
    headerTr.appendChild(ytdTh);
    const tbody = document.getElementById(bodyId);
    const htmlRows = rows.map(r => {
      const cells = [`<td><div class="sku-name-cell"><span class="sku-name">${displayName(r)}</span><span class="sku-code">${getId(r)}</span></div></td>`];
      let prevRev = null;
      months.forEach(m => {
        const mv = r.months[m];
        const rev = mv ? mv.gross_revenue : null;
        if (rev === null || rev === undefined) {
          cells.push(`<td class="${prevRev === null ? "" : "mom-down"} cell-dash">–</td>`);
        } else {
          const cls = prevRev === null ? "" : (rev > prevRev ? "mom-up" : (rev < prevRev ? "mom-down" : ""));
          cells.push(`<td class="${cls}"><span class="cell-rev">${Math.round(rev).toLocaleString()}</span><span class="cell-units">${mv.units}u</span></td>`);
          prevRev = rev;
        }
      });
      cells.push(`<td class="ytd"><span class="cell-rev">${Math.round(r.gross_revenue).toLocaleString()}</span><span class="cell-units">${r.units}u</span></td>`);
      return `<tr>${cells.join("")}</tr>`;
    });
    tbody.innerHTML = htmlRows.join("\\n");
  }
  renderGenericSkuMatrix("noonSkuMatrixHeaderRow", "noonSkuMatrixBody", noonData.months, noonData.sku_rows, r => r.partner_sku);
  renderGenericSkuMatrix("overallSkuMatrixHeaderRow", "overallSkuMatrixBody", overallData.months, overallData.sku_rows, r => r.sku);

  // ---------- Generic Product-wise Profitability (Noon / Overall), period-filterable ----------
  // Columns are a SUBSET of the Amazon tab's own Product-wise Profitability table -
  // Sellable returns/Damages/Net sales are dropped because neither Noon's finance
  // export nor the Overall merge has return-disposition data the way Amazon's FBA
  // Customer Returns report does (see this table's own section-note). Everything that
  // IS real (Qty/ASP/Cost-per-unit/Gross revenue/COGS/Gross margin/Fees/Ad spend
  // alloc/Net profit) uses the exact same cellN0/cellN1/cellPct formatting helpers and
  // "neg" conventions as the Amazon tab, and the table itself uses the same
  // frozen-column + period-filter-bar pattern (profit-frozen-4, one fewer frozen
  // column than Amazon's since there's no Total Inv Qty column here).
  function renderGenericProfitTable(tbodyId, filterBarId, rows, months, getId) {
    function periodData(r, period) {
      if (period === "YTD") return r;
      if (period === "MTD") return r.months[months[months.length - 1]];
      return r.months[period];
    }
    function render(period) {
      const tbody = document.getElementById(tbodyId);
      const items = rows.map(r => ({ r, d: periodData(r, period) })).filter(x => x.d && x.d.units);
      const tot = { units: 0, gross_revenue: 0, cogs: 0, gross_margin: 0, fees: 0, ad_spend_allocated: 0, net_profit: 0 };
      const htmlRows = items.map(({ r, d }) => {
        tot.units += d.units || 0; tot.gross_revenue += d.gross_revenue || 0; tot.cogs += d.cogs || 0;
        tot.gross_margin += d.gross_margin || 0; tot.fees += d.fees || 0; tot.ad_spend_allocated += d.ad_spend_allocated || 0;
        tot.net_profit += d.net_profit || 0;
        return `<tr><td title="${displayName(r)}">${displayName(r)}</td><td>${d.units || 0}</td>` +
          cellN1(d.asp) + cellN1(r.cost_per_unit) +
          cellN0(d.gross_revenue, "") + cellN0(-Math.abs(d.cogs), "neg") + cellN0(d.gross_margin, "") + cellPct(d.margin_pct) +
          cellN0(d.fees, "") + cellN0(d.ad_spend_allocated, "") +
          cellN0(d.net_profit, "ytd" + (d.net_profit < 0 ? " neg" : "")) + cellPct(d.net_margin_pct) + `</tr>`;
      });
      const totGM = tot.gross_revenue ? (tot.gross_margin / tot.gross_revenue * 100) : null;
      const totNM = tot.gross_revenue ? (tot.net_profit / tot.gross_revenue * 100) : null;
      const totAsp = tot.units ? (tot.gross_revenue / tot.units) : null;
      const totCpu = tot.units ? (tot.cogs / tot.units) : null;
      const totRow = `<tr class="total"><td>Total (${items.length} SKU${items.length === 1 ? "" : "s"})</td><td>${tot.units}</td>` +
        cellN1(totAsp) + cellN1(totCpu) +
        cellN0(tot.gross_revenue, "") + cellN0(-Math.abs(tot.cogs), "neg") + cellN0(tot.gross_margin, "") + cellPct(totGM) +
        cellN0(tot.fees, "") + cellN0(tot.ad_spend_allocated, "") +
        cellN0(tot.net_profit, "ytd" + (tot.net_profit < 0 ? " neg" : "")) + cellPct(totNM) + `</tr>`;
      tbody.innerHTML = htmlRows.join("\\n") + totRow;
    }
    const filterBarEl = document.getElementById(filterBarId);
    const periods = months.concat(["MTD", "YTD"]);
    periods.forEach(p => {
      const btn = document.createElement("button");
      btn.className = "filter-btn" + (p === "YTD" ? " active" : "");
      btn.textContent = p === "MTD" ? "MTD" : p === "YTD" ? "YTD" : monthShort(p);
      btn.addEventListener("click", () => {
        filterBarEl.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        render(p);
      });
      filterBarEl.appendChild(btn);
    });
    render("YTD");
  }
  renderGenericProfitTable("noonProfitTableBody", "noonProfitFilterBar", noonData.sku_rows, noonData.months, r => r.partner_sku);
  renderGenericProfitTable("overallProfitTableBody", "overallProfitFilterBar", overallData.sku_rows, overallData.months, r => r.sku);

  // ---------- Generic Cost-per-unit & recommended ASP table (Noon / Overall) ----------
  // Same table Amazon's own "Cost per unit & recommended ASP" section uses, reading
  // noonData.cost_table/overallData.cost_table (built server-side by
  // merge_dashboard_data.py's build_cost_table() - reuses Amazon's own landed-cost
  // figures verbatim, only actual_asp/margin_at_actual_pct are channel-specific).
  function renderGenericCostTable(tbodyId, rows) {
    const tbody = document.getElementById(tbodyId);
    const dash = '<td class="cell-dash">&mdash;</td>';
    const htmlRows = rows.map(r => {
      const hasAsp = r.actual_asp !== null && r.actual_asp !== undefined && r.actual_asp > 0;
      const meetsTarget = hasAsp && r.recommended_asp_60pct_margin != null && r.actual_asp >= r.recommended_asp_60pct_margin;
      const aspCls = hasAsp && r.recommended_asp_60pct_margin != null ? (meetsTarget ? "pos" : "neg") : "";
      const marginCls = r.margin_at_actual_pct != null && r.margin_at_actual_pct < 0 ? "neg" : "";
      return `<tr><td>${r.name}</td>` +
        (r.product_cost_usd == null ? dash : `<td>$${r.product_cost_usd.toFixed(1)}</td>`) +
        (r.freight_usd == null ? dash : `<td>$${r.freight_usd.toFixed(1)}</td>`) +
        (r.duty_usd == null ? dash : `<td>$${r.duty_usd.toFixed(1)}</td>`) +
        (r.vat_usd == null ? dash : `<td>$${r.vat_usd.toFixed(1)}</td>`) +
        `<td>${r.landed_cost_usd == null ? "&mdash;" : "$" + r.landed_cost_usd.toFixed(1)}</td>` +
        `<td class="ytd">${r.landed_cost == null ? "&mdash;" : r.landed_cost.toFixed(1)}</td>` +
        `<td class="pct">${r.recommended_asp_60pct_margin == null ? "&mdash;" : r.recommended_asp_60pct_margin.toFixed(1)}</td>` +
        `<td class="pct ${aspCls}">${hasAsp ? r.actual_asp.toFixed(1) : "&mdash;"}</td>` +
        `<td class="pct ${marginCls}">${r.margin_at_actual_pct == null ? "&mdash;" : r.margin_at_actual_pct.toFixed(1) + "%"}</td></tr>`;
    });
    tbody.innerHTML = htmlRows.join("\\n");
  }
  renderGenericCostTable("noonCostTableBody", noonData.cost_table);
  renderGenericCostTable("overallCostTableBody", overallData.cost_table);

  function statusAndDoc(r) {
    const doc = r.days_of_cover === null || r.days_of_cover === undefined ? "&mdash;" : r.days_of_cover.toLocaleString(undefined,{maximumFractionDigits:0});
    const statusCell = r.needs_reorder ? `<td class="reorder-flag">Reorder now</td>` : `<td class="ok-flag">OK</td>`;
    return { doc, statusCell };
  }

  function renderNoonInventory() {
    const tbody = document.getElementById("noonInvBody");
    const rows = noonData.inventory.map(r => {
      const { doc, statusCell } = statusAndDoc(r);
      const whCell = r.wh_inv === null || r.wh_inv === undefined ? '<td class="cell-dash">&mdash;</td>' : `<td>${r.wh_inv.toLocaleString()}</td>`;
      return `<tr${r.needs_reorder ? ' class="reorder"' : ""}><td>${r.name}<br><span class="sku-code">${r.sku}</span></td>` +
        `<td>${r.noon_inv.toLocaleString()}</td>` + whCell + `<td class="ytd">${r.total_inv.toLocaleString()}</td>` +
        `<td>${r.units_sold_ytd}</td><td>${r.avg_daily_sales.toFixed(3)}</td><td>${r.reorder_point_90d.toFixed(1)}</td>` +
        `<td>${doc}</td><td>${r.suggested_reorder_qty}</td>` + statusCell + `</tr>`;
    });
    tbody.innerHTML = rows.join("\\n");
  }
  renderNoonInventory();

  function renderOverallMonthly() {
    const byMonth = key => m => { const row = overallData.monthly.find(r => r.month === m); return row ? row[key] : null; };
    const rows = [];
    rows.push(moneyRow("Gross revenue (Amazon + Noon)", overallData.months, byMonth("gross_revenue"), overallData.ytd.gross_revenue, "total"));
    rows.push(subRow("&nbsp;&nbsp;of which Amazon", overallData.months, byMonth("gross_revenue_amazon")));
    rows.push(subRow("&nbsp;&nbsp;of which Noon", overallData.months, byMonth("gross_revenue_noon")));
    rows.push(moneyRow("COGS", overallData.months, m => -Math.abs(byMonth("cogs")(m) || 0), -Math.abs(overallData.ytd.cogs), ""));
    rows.push(moneyRow("Gross margin", overallData.months, byMonth("gross_margin"), overallData.ytd.gross_margin, "total"));
    rows.push(subRow("Other costs (Amazon: fees/storage/adj.)", overallData.months, byMonth("other_costs_amazon")));
    rows.push(subRow("Other costs (Noon: referral/fulfillment/etc.)", overallData.months, byMonth("other_costs_noon")));
    rows.push(moneyRow("Advertising spend", overallData.months, byMonth("ad_spend"), overallData.ytd.ad_spend, ""));
    rows.push(moneyRow("Net profit (Amazon + Noon)", overallData.months, byMonth("net_profit"), overallData.ytd.net_profit, "total final"));
    rows.push(subRow("&nbsp;&nbsp;of which Amazon", overallData.months, byMonth("net_profit_amazon")));
    rows.push(subRow("&nbsp;&nbsp;of which Noon", overallData.months, byMonth("net_profit_noon")));
    document.getElementById("overallMonthlyBody").innerHTML = rows.join("\\n");
  }
  renderOverallMonthly();

  function renderOverallInventory() {
    const tbody = document.getElementById("overallInvBody");
    const rows = overallData.inventory.map(r => {
      const { doc, statusCell } = statusAndDoc(r);
      const whCell = r.wh_inv === null || r.wh_inv === undefined ? '<td class="cell-dash">&mdash;</td>' : `<td>${r.wh_inv.toLocaleString()}</td>`;
      return `<tr${r.needs_reorder ? ' class="reorder"' : ""}><td>${r.name}<br><span class="sku-code">${r.sku}</span></td>` +
        `<td>${r.az_inv.toLocaleString()}</td><td>${r.noon_inv.toLocaleString()}</td>` + whCell + `<td class="ytd">${r.total_inv.toLocaleString()}</td>` +
        `<td>${r.units_sold_ytd}</td><td>${r.avg_daily_sales.toFixed(3)}</td><td>${r.reorder_point_90d.toFixed(1)}</td>` +
        `<td>${doc}</td><td>${r.suggested_reorder_qty}</td>` + statusCell + `</tr>`;
    });
    tbody.innerHTML = rows.join("\\n");
  }
  renderOverallInventory();
  /* CHANNEL_JS_END */
"""


def main():
    data_only = "--data-only" in sys.argv
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    noon_slim, overall_slim, generated_at = load_data()

    html = strip_between(html, "/* CHANNEL_JS_START */", "/* CHANNEL_JS_END */")
    html = html.replace("</script>", js_block(noon_slim, overall_slim) + "\n</script>")

    if not data_only:
        # One-time structural wrap (idempotent - only applied if not already wrapped):
        # the Amazon tab's pre-existing <nav class="tab-nav"> + 5 tab-panels get wrapped
        # in #channelAmazon, and the new channel-nav buttons are inserted just before it.
        if 'id="channelAmazon"' not in html:
            html = html.replace(
                '<nav class="tab-nav">',
                '  <div id="channelAmazon" class="channel-panel">\n  <nav class="tab-nav">',
                1)
            html = html.replace(
                '  <footer>',
                '  </div> <!-- /channelAmazon -->\n  <!-- CHANNEL_NOON_OVERALL_ANCHOR -->\n  <footer>',
                1)

        # Everything below IS refreshed on every non-data-only run (strip, then
        # reinsert fresh) - CSS, the channel-nav buttons, and the Noon/Overall markup -
        # this used to only happen on the FIRST run (bug: a second full run stripped
        # the old Noon/Overall HTML but never put back a new copy, since reinsertion
        # was wrongly nested inside the one-time wrap's `if` above). Fixed 2026-10-03.
        html = strip_between(html, "/* CHANNEL_CSS_START */", "/* CHANNEL_CSS_END */\n")
        html = html.replace("</style>", CSS_BLOCK + "</style>")
        html = strip_between(html, "<!-- CHANNEL_NAV_START -->", "<!-- CHANNEL_NAV_END -->")
        html = html.replace(
            '  <div id="channelAmazon" class="channel-panel">',
            NAV_HTML + '  <div id="channelAmazon" class="channel-panel">',
            1)
        html = strip_between(html, "<!-- CHANNEL_NOON_OVERALL_START -->", "<!-- CHANNEL_NOON_OVERALL_END -->")
        html = html.replace(
            '<!-- CHANNEL_NOON_OVERALL_ANCHOR -->',
            noon_overall_html().strip() + '\n  <!-- CHANNEL_NOON_OVERALL_ANCHOR -->',
            1)

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Updated {HTML_PATH} (data_only={data_only})")


if __name__ == "__main__":
    main()
