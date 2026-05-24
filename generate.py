#!/usr/bin/env python3
"""
Weekly COT Dashboard Generator
Fetches COT data directly from CFTC annual zip files — no web scraping needed.

CFTC publishes every Friday 3:30 pm ET.
Run: python generate.py
"""

import csv
import io
import re
import json
import sys
import zipfile
from pathlib import Path
from datetime import datetime, timedelta

import requests

# ── CFTC data ─────────────────────────────────────────────────────────────────
CFTC_BASE    = "https://www.cftc.gov/files/dea/history"
HISTORY_WEEKS = 104  # 2 years

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})

# Module-level cache so each zip is only downloaded once per run
_cftc_cache: dict = {}

# ── Asset config ──────────────────────────────────────────────────────────────
ASSETS_CFG = [
    dict(
        id="gold", label="Gold", ticker="AU",
        accent="#d4a017", bg="rgba(212,160,23,0.18)",
        subtitle="COMEX · 100 Troy Oz/contract · COT Legacy Futures",
        cftc_code="088691", legacy=True, financial=False, hist_type="legacy",
        yahoo_symbol="GC=F",
    ),
    dict(
        id="es", label="E-Mini S&P 500", ticker="ES",
        accent="#58a6ff", bg="rgba(88,166,255,0.15)",
        subtitle="CME · COT Legacy &amp; Financial Futures · Code 13874A",
        cftc_code="13874A", legacy=True, financial=True, hist_type="legacy",
        yahoo_symbol="ES=F",
    ),
    dict(
        id="nq", label="NASDAQ-100 Mini", ticker="NQ",
        accent="#bc8cff", bg="rgba(188,140,255,0.15)",
        subtitle="CME · COT Legacy &amp; Financial Futures · Code 209742",
        cftc_code="209742", legacy=True, financial=True, hist_type="legacy",
        yahoo_symbol="NQ=F",
    ),
    dict(
        id="cl", label="Crude Oil (WTI)", ticker="CL",
        accent="#e8724a", bg="rgba(232,114,74,0.15)",
        subtitle="NYMEX · 1,000 Barrels/contract · COT Legacy Futures · Code 067651",
        cftc_code="067651", legacy=True, financial=False, hist_type="legacy",
        yahoo_symbol="CL=F",
    ),
]


# ── CFTC download helpers ─────────────────────────────────────────────────────

def _rows_from_zip(url: str) -> list:
    """Download a CFTC zip and return all CSV rows (each file's header skipped)."""
    print(f"    Downloading {url} ...")
    resp = SESSION.get(url, timeout=180)
    resp.raise_for_status()
    all_rows: list = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith((".txt", ".csv")):
                continue
            with zf.open(name) as f:
                text = f.read().decode("latin-1", errors="replace")
            rows = list(csv.reader(io.StringIO(text)))
            all_rows.extend(rows[1:])
    return all_rows


def _get_cftc_rows(is_financial: bool) -> list:
    """Return cached rows for the last 2 calendar years (legacy or financial)."""
    key = "financial" if is_financial else "legacy"
    if key in _cftc_cache:
        return _cftc_cache[key]

    current_year = datetime.now().year
    all_rows: list = []
    for year in [current_year - 1, current_year]:
        if is_financial:
            url = f"{CFTC_BASE}/fut_fin_txt_{year}.zip"
        else:
            url = f"{CFTC_BASE}/deacot{year}.zip"
        try:
            rows = _rows_from_zip(url)
            all_rows.extend(rows)
            print(f"      {year}: {len(rows):,} rows")
        except Exception as exc:
            print(f"      WARNING: {year} download failed: {exc}", file=sys.stderr)

    _cftc_cache[key] = all_rows
    return all_rows


def _parse_date(s: str) -> str | None:
    """Parse CFTC date formats (YYYY-MM-DD or MM/DD/YYYY) → YYYY-MM-DD."""
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _extract_rows(all_rows: list, cftc_code: str, is_financial: bool) -> dict:
    """
    Filter rows by CFTC commodity code and extract all relevant fields.
    Returns {date_str: field_dict}, sorted from earliest to latest.

    Legacy column indices (deacot*.zip):
      oi=7, nc_long=8, nc_short=9, comm_long=11, comm_short=12,
      nr_long=15, nr_short=16, chg_nc_long=38, chg_nc_short=39

    Financial column indices (fut_fin_txt_*.zip):
      oi=7, dealer_long=8, dealer_short=9, am_long=11, am_short=12,
      lev_long=14, lev_short=15, other_long=17, other_short=18,
      chg_lev_long=31, chg_lev_short=32
    """
    code_col, date_col = 3, 2
    result: dict = {}

    for row in all_rows:
        if len(row) < 45:
            continue
        if row[code_col].strip() != cftc_code:
            continue
        date_str = _parse_date(row[date_col])
        if not date_str:
            continue

        def _i(idx: int) -> int:
            try:
                return int(row[idx].replace(",", "").strip() or "0")
            except (ValueError, IndexError):
                return 0

        if is_financial:
            result[date_str] = {
                "dealer_long":  _i(8),  "dealer_short": _i(9),
                "am_long":      _i(11), "am_short":     _i(12),
                "lev_long":     _i(14), "lev_short":    _i(15),
                "other_long":   _i(17), "other_short":  _i(18),
                "oi":           _i(7),
                "chg_lev_long": _i(31), "chg_lev_short": _i(32),
            }
        else:
            result[date_str] = {
                "nc_long":      _i(8),  "nc_short":      _i(9),
                "comm_long":    _i(11), "comm_short":    _i(12),
                "nr_long":      _i(15), "nr_short":      _i(16),
                "oi":           _i(7),
                "chg_nc_long":  _i(38), "chg_nc_short":  _i(39),
            }

    return result


# ── Yahoo Finance price helpers ───────────────────────────────────────────────

_price_cache: dict = {}


def _fetch_weekly_prices(yahoo_symbol: str) -> dict:
    """Fetch ~3 years of weekly closes from Yahoo Finance. Returns {YYYY-MM-DD: close}."""
    if yahoo_symbol in _price_cache:
        return _price_cache[yahoo_symbol]
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
        f"?interval=1wk&range=3y"
    )
    try:
        resp = SESSION.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes     = result["indicators"]["quote"][0]["close"]
        prices: dict = {}
        for ts, close in zip(timestamps, closes):
            if close is not None:
                d = datetime.fromtimestamp(ts, tz=__import__('datetime').timezone.utc).strftime("%Y-%m-%d")
                prices[d] = round(float(close), 4)
        _price_cache[yahoo_symbol] = prices
        print(f"    Prices: {len(prices)} weekly closes for {yahoo_symbol}")
        return prices
    except Exception as exc:
        print(f"    WARNING: price fetch failed for {yahoo_symbol}: {exc}", file=sys.stderr)
        return {}


def _price_on_or_before(prices: dict, target: str) -> float | None:
    """Most-recent weekly close on or before target date."""
    tdt = datetime.strptime(target, "%Y-%m-%d")
    best = None
    for d, p in prices.items():
        ddt = datetime.strptime(d, "%Y-%m-%d")
        if ddt <= tdt:
            if best is None or ddt > datetime.strptime(best[0], "%Y-%m-%d"):
                best = (d, p)
    return best[1] if best else None


def _price_one_week_after(prices: dict, cot_date: str) -> float | None:
    """Weekly close whose candle opens 5–14 days after cot_date (i.e. next week's close)."""
    cdt = datetime.strptime(cot_date, "%Y-%m-%d")
    for d in sorted(prices.keys()):
        delta = (datetime.strptime(d, "%Y-%m-%d") - cdt).days
        if 5 <= delta <= 14:
            return prices[d]
    return None


def _build_call_history(leg_rows: dict, fin_rows: dict, hist_type: str,
                        prices: dict) -> tuple[list, dict]:
    """
    Generate a scored verdict for every COT week in the historical data.
    Returns (call_history_list, accuracy_stats).
    call_history_list is sorted newest-first, capped at HISTORY_WEEKS entries.
    Only legacy nc_net is used for the verdict regardless of hist_type so the
    displayed accuracy always reflects commercial/non-commercial positioning.
    """
    source_rows = leg_rows if (hist_type == "legacy" and leg_rows) else fin_rows
    if not source_rows:
        return [], {"correct": 0, "wrong": 0, "total": 0, "pct": None}

    entries = []
    for date_str in sorted(source_rows.keys()):
        if hist_type == "legacy":
            d       = leg_rows[date_str]
            nc_long  = d.get("nc_long",  0) or 0
            nc_short = d.get("nc_short", 0) or 0
            oi       = d.get("oi",       0) or 0
            nc_net   = nc_long - nc_short
            v_id, v_label = verdict(nc_net, oi)
        else:  # financial
            d       = fin_rows[date_str]
            am_long  = d.get("am_long",  0) or 0
            am_short = d.get("am_short", 0) or 0
            lev_long  = d.get("lev_long",  0) or 0
            lev_short = d.get("lev_short", 0) or 0
            am_net  = am_long  - am_short
            lev_net = lev_long - lev_short
            if am_net > 0 and lev_net < 0:
                v_id, v_label = "mixed",   "Mixed"
            elif am_net > 0:
                v_id, v_label = "bullish", "Bullish"
            elif am_net < 0 and lev_net > 0:
                v_id, v_label = "mixed",   "Mixed"
            else:
                v_id, v_label = "bearish", "Bearish"

        p_curr = _price_on_or_before(prices, date_str)
        p_next = _price_one_week_after(prices, date_str)

        if p_curr and p_next:
            chg = (p_next - p_curr) / p_curr * 100
            chg = round(chg, 2)
            if v_id == "mixed":
                outcome = "neutral"
            elif v_id == "bullish":
                outcome = "correct" if chg > 0 else "wrong"
            else:  # bearish
                outcome = "correct" if chg < 0 else "wrong"
        else:
            chg     = None
            outcome = "pending"

        entries.append({
            "cotDate":       date_str,
            "verdict":       v_id,
            "verdictLabel":  v_label,
            "priceAtDate":   round(p_curr, 2) if p_curr else None,
            "priceNextWeek": round(p_next, 2) if p_next else None,
            "priceChangePct": chg,
            "outcome":       outcome,
        })

    # Sort newest first, keep up to HISTORY_WEEKS
    entries.sort(key=lambda e: e["cotDate"], reverse=True)
    entries = entries[:HISTORY_WEEKS]

    # Accuracy stats (exclude mixed/pending from denominator)
    resolved = [e for e in entries if e["outcome"] in ("correct", "wrong")]
    correct  = sum(1 for e in resolved if e["outcome"] == "correct")
    wrong    = len(resolved) - correct
    total    = len(resolved)
    pct      = round(correct / total * 100) if total else None

    accuracy = {"correct": correct, "wrong": wrong, "total": total, "pct": pct}
    return entries, accuracy


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmtnum(n: int) -> str:
    sign = "+" if n >= 0 else "−"
    return f"{sign}{abs(n):,}"


def fmtabs(n: int) -> str:
    return f"{abs(n):,}"


def long_pct(long: int, short: int) -> int:
    total = long + short
    return round(long / total * 100) if total else 50


def verdict(nc_net: int, oi: int) -> tuple[str, str]:
    ratio = nc_net / oi if oi else 0
    if ratio > 0.08:
        return "bullish", "Bullish"
    if ratio < -0.08:
        return "bearish", "Bearish"
    return "mixed", "Mixed"


# ── Analysis text generators ──────────────────────────────────────────────────

def legacy_analysis(d: dict) -> dict:
    nc_long    = d.get("nc_long")    or 0
    nc_short   = d.get("nc_short")   or 0
    comm_long  = d.get("comm_long")  or 0
    comm_short = d.get("comm_short") or 0
    nr_long    = d.get("nr_long")    or 0
    nr_short   = d.get("nr_short")   or 0
    oi         = d.get("oi")         or 0
    chg_nc_l   = d.get("chg_nc_long")  or 0
    chg_nc_s   = d.get("chg_nc_short") or 0

    nc_net   = nc_long - nc_short
    comm_net = comm_long - comm_short
    nr_net   = nr_long - nr_short
    lp       = long_pct(nc_long, nc_short)
    v_id, v_label = verdict(nc_net, oi)

    bias     = "long"    if nc_net >= 0 else "short"
    bias_dir = "bullish" if nc_net >= 0 else "bearish"

    long_move  = (f"added <strong>{fmtabs(chg_nc_l)} new longs</strong>"
                  if chg_nc_l >= 0 else
                  f"trimmed <strong>{fmtabs(abs(chg_nc_l))} longs</strong>")
    short_move = (f"added {fmtabs(chg_nc_s)} new shorts"
                  if chg_nc_s >= 0 else
                  f"cut {fmtabs(abs(chg_nc_s))} shorts")

    crowding = (" Positioning is becoming stretched — extreme net readings historically precede short-term reversals."
                if abs(nc_net / oi) > 0.35 and oi else "")

    analysis = (
        f"<strong>Non-commercial traders (speculators &amp; hedge funds)</strong> are "
        f"<strong>net {bias}</strong> with <strong>{fmtnum(nc_net)} contracts</strong>, "
        f"holding {lp}% long vs {100 - lp}% short — a clear {bias_dir} lean from the speculative community. "
        f"Week-over-week, specs {long_move} while {short_move}."
        f"<br><br>"
        f"<strong>Commercial traders</strong> (producers and hedgers) hold a net position of "
        f"<strong>{fmtnum(comm_net)}</strong>. This is standard hedging behaviour, not a directional bet. "
        f"Small traders (non-reportable) are net <strong>{fmtnum(nr_net)}</strong>."
        f"<br><br>"
        f"<strong>Overall bias: {v_label}.</strong>"
        + (" Speculative longs are dominant." + crowding if v_id == "bullish" else
           " Speculative shorts are dominant — the market is positioned defensively." + crowding if v_id == "bearish" else
           " Positioning is balanced with no strong directional conviction from either side.")
    )

    comm_lp = long_pct(comm_long, comm_short)

    return dict(
        verdict=v_id, verdictLabel=v_label,
        analysis=analysis,
        sentimentLabel="Non-Commercial Long vs Short",
        sentimentLongPct=lp,
        chips=[
            dict(label="Spec Net",   value=fmtnum(nc_net),   cls="pos" if nc_net >= 0 else "neg"),
            dict(label="WoW Longs",  value=fmtnum(chg_nc_l), cls="pos" if chg_nc_l >= 0 else "neg"),
            dict(label="WoW Shorts", value=fmtnum(chg_nc_s), cls="neg" if chg_nc_s >= 0 else "pos"),
        ],
        sections=[dict(
            heading="COT Legacy Positions",
            rows=[
                dict(type="Non-Commercial", sub="Speculators / Hedge Funds",
                     long=f"{nc_long:,}",   short=f"{nc_short:,}",   net=fmtnum(nc_net),
                     netCls="pos" if nc_net >= 0 else "neg", longPct=lp, shortPct=100 - lp),
                dict(type="Commercial",     sub="Producers / Hedgers",
                     long=f"{comm_long:,}", short=f"{comm_short:,}", net=fmtnum(comm_net),
                     netCls="pos" if comm_net >= 0 else "neg",
                     longPct=comm_lp, shortPct=100 - comm_lp),
                dict(type="Non-Reportable", sub="Small Traders / Retail",
                     long=f"{nr_long:,}",   short=f"{nr_short:,}",   net=fmtnum(nr_net),
                     netCls="pos" if nr_net >= 0 else "neg",
                     longPct=long_pct(nr_long, nr_short), shortPct=long_pct(nr_short, nr_long)),
            ],
        )],
    )


def financial_analysis(d_fin: dict, d_leg: dict | None = None) -> dict:
    dl    = d_fin.get("dealer_long")  or 0
    ds    = d_fin.get("dealer_short") or 0
    aml   = d_fin.get("am_long")      or 0
    ams   = d_fin.get("am_short")     or 0
    ll    = d_fin.get("lev_long")     or 0
    ls    = d_fin.get("lev_short")    or 0
    ol    = d_fin.get("other_long")   or 0
    os_   = d_fin.get("other_short")  or 0
    oi    = d_fin.get("oi")           or 0
    chg_ll = d_fin.get("chg_lev_long")  or 0
    chg_ls = d_fin.get("chg_lev_short") or 0

    am_net     = aml - ams
    lev_net    = ll  - ls
    dealer_net = dl  - ds
    other_net  = ol  - os_
    am_lp      = long_pct(aml, ams)
    lev_lp     = long_pct(ll, ls)

    if am_net > 0 and lev_net < 0:
        v_id, v_label = "mixed", "Mixed — Institutions Bullish / Leveraged Funds Bearish"
    elif am_net > 0:
        v_id, v_label = "bullish", "Bullish"
    elif am_net < 0 and lev_net > 0:
        v_id, v_label = "mixed", "Mixed — Institutions Bearish / Leveraged Funds Bullish"
    else:
        v_id, v_label = "bearish", "Bearish"

    lev_chg_txt = ""
    if chg_ls > 0:
        lev_chg_txt = (f" Notably, leveraged funds added <strong>{fmtabs(chg_ls)} new short contracts</strong> "
                       f"this week, deepening their bearish positioning.")
    elif chg_ls < 0:
        lev_chg_txt = (f" Leveraged funds covered <strong>{fmtabs(abs(chg_ls))} short contracts</strong> "
                       f"this week, reducing their bearish exposure.")

    verdict_note = {
        "bullish": "Institutional and speculative money are broadly aligned to the upside.",
        "bearish": "Both major groups are positioned for downside.",
        "mixed":   ("The divergence between institutional longs and leveraged shorts is notable — "
                    "a large spec short base can accelerate a rally if a positive catalyst arrives (short squeeze risk)."),
    }[v_id]

    analysis = (
        f"<strong>Asset Managers</strong> — the largest institutional group — are "
        f"<strong>net {'long' if am_net >= 0 else 'short'} {fmtnum(am_net)}</strong>, "
        f"holding {am_lp}% long vs {100 - am_lp}% short. This reflects sustained "
        f"{'bullish' if am_net >= 0 else 'bearish'} institutional conviction.<br><br>"
        f"<strong>Leveraged Funds</strong> (hedge funds, CTAs) are "
        f"<strong>net {'long' if lev_net >= 0 else 'short'} {fmtnum(lev_net)}</strong> "
        f"({lev_lp}% long), representing the speculative directional bet.{lev_chg_txt} "
        f"<strong>Dealers</strong> are net <strong>{fmtnum(dealer_net)}</strong>, "
        f"primarily reflecting client-flow hedging.<br><br>"
        f"<strong>Overall bias: {v_label.split('—')[0].strip()}.</strong> {verdict_note}"
    )

    sections = []
    if d_leg:
        nc_l   = d_leg.get("nc_long")    or 0
        nc_s   = d_leg.get("nc_short")   or 0
        co_l   = d_leg.get("comm_long")  or 0
        co_s   = d_leg.get("comm_short") or 0
        nc_net_leg = nc_l - nc_s
        co_net     = co_l - co_s
        sections.append(dict(
            heading="COT Legacy Positions",
            rows=[
                dict(type="Non-Commercial", sub="Speculators / Hedge Funds",
                     long=f"{nc_l:,}", short=f"{nc_s:,}", net=fmtnum(nc_net_leg),
                     netCls="pos" if nc_net_leg >= 0 else "neg",
                     longPct=long_pct(nc_l, nc_s), shortPct=long_pct(nc_s, nc_l)),
                dict(type="Commercial", sub="Institutional / Hedgers",
                     long=f"{co_l:,}", short=f"{co_s:,}", net=fmtnum(co_net),
                     netCls="pos" if co_net >= 0 else "neg",
                     longPct=long_pct(co_l, co_s), shortPct=long_pct(co_s, co_l)),
            ],
        ))

    sections.append(dict(
        heading="Disaggregated Financial COT",
        rows=[
            dict(type="Dealer / Intermediary", sub="Client-flow hedging",
                 long=f"{dl:,}",  short=f"{ds:,}",  net=fmtnum(dealer_net),
                 netCls="pos" if dealer_net >= 0 else "neg",
                 longPct=long_pct(dl, ds), shortPct=long_pct(ds, dl)),
            dict(type="Asset Manager", sub="Institutional long-only",
                 long=f"{aml:,}", short=f"{ams:,}", net=fmtnum(am_net),
                 netCls="pos" if am_net >= 0 else "neg",
                 longPct=am_lp, shortPct=100 - am_lp),
            dict(type="Leveraged Funds", sub="Hedge funds / CTAs",
                 long=f"{ll:,}",  short=f"{ls:,}",  net=fmtnum(lev_net),
                 netCls="pos" if lev_net >= 0 else "neg",
                 longPct=lev_lp, shortPct=100 - lev_lp),
            dict(type="Other Reportables", sub="Other institutional",
                 long=f"{ol:,}",  short=f"{os_:,}", net=fmtnum(other_net),
                 netCls="pos" if other_net >= 0 else "neg",
                 longPct=long_pct(ol, os_), shortPct=long_pct(os_, ol)),
        ],
    ))

    return dict(
        verdict=v_id, verdictLabel=v_label,
        analysis=analysis,
        sentimentLabel="Asset Manager Long vs Short",
        sentimentLongPct=am_lp,
        chips=[
            dict(label="Asset Mgr Net", value=fmtnum(am_net),     cls="pos" if am_net >= 0 else "neg"),
            dict(label="Lev Funds Net", value=fmtnum(lev_net),    cls="pos" if lev_net >= 0 else "neg"),
            dict(label="Dealer Net",    value=fmtnum(dealer_net), cls="pos" if dealer_net >= 0 else "neg"),
            dict(label="Lev WoW Short", value=fmtnum(chg_ls),     cls="neg" if chg_ls >= 0 else "pos"),
        ],
        sections=sections,
    )


# ── Chart URL helper ──────────────────────────────────────────────────────────

def chart_img_url(page_url: str) -> str:
    m = re.search(r"/x/([A-Za-z0-9]+)/?$", page_url)
    if m:
        code = m.group(1)
        return f"https://s3.tradingview.com/snapshots/{code[0].lower()}/{code}.png"
    return ""


# ── Build one asset ───────────────────────────────────────────────────────────

def build_asset(cfg: dict) -> dict:
    d_leg: dict | None = None
    d_fin: dict | None = None
    leg_rows: dict = {}
    fin_rows: dict = {}
    report_date = datetime.now().strftime("%Y-%m-%d")

    if cfg["legacy"]:
        print(f"  Loading legacy CFTC data...")
        all_rows = _get_cftc_rows(False)
        leg_rows = _extract_rows(all_rows, cfg["cftc_code"], False)
        if not leg_rows:
            raise ValueError(f"No legacy CFTC rows found for code {cfg['cftc_code']}")
        latest = max(leg_rows.keys())
        d_leg = leg_rows[latest]
        report_date = latest
        print(f"    Latest: {latest}  nc_long={d_leg.get('nc_long'):,}  oi={d_leg.get('oi'):,}")

    if cfg["financial"]:
        print(f"  Loading financial CFTC data...")
        all_rows = _get_cftc_rows(True)
        fin_rows = _extract_rows(all_rows, cfg["cftc_code"], True)
        if not fin_rows:
            raise ValueError(f"No financial CFTC rows found for code {cfg['cftc_code']}")
        latest = max(fin_rows.keys())
        d_fin = fin_rows[latest]
        if not d_leg:
            report_date = latest
        print(f"    Latest: {latest}  am_long={d_fin.get('am_long'):,}  lev_short={d_fin.get('lev_short'):,}")

    # Generate analysis text + chips + sections
    if d_fin:
        computed = financial_analysis(d_fin, d_leg)
        oi_val   = d_fin.get("oi") or (d_leg.get("oi") if d_leg else 0) or 0
    else:
        computed = legacy_analysis(d_leg)  # type: ignore[arg-type]
        oi_val   = (d_leg or {}).get("oi") or 0

    # Build 2-year COT history for the chart
    if cfg["hist_type"] == "financial" and fin_rows:
        dates = sorted(fin_rows.keys())[-HISTORY_WEEKS:]
        cot_history = {
            "dates":  dates,
            "line1":  [fin_rows[d]["am_long"]  - fin_rows[d]["am_short"]  for d in dates],
            "line2":  [fin_rows[d]["lev_long"] - fin_rows[d]["lev_short"] for d in dates],
            "label1": "Asset Manager Net",
            "label2": "Leveraged Funds Net",
        }
    elif leg_rows:
        dates = sorted(leg_rows.keys())[-HISTORY_WEEKS:]
        cot_history = {
            "dates":  dates,
            "line1":  [leg_rows[d]["nc_long"]   - leg_rows[d]["nc_short"]   for d in dates],
            "line2":  [leg_rows[d]["comm_long"]  - leg_rows[d]["comm_short"] for d in dates],
            "label1": "Non-Commercial Net",
            "label2": "Commercial Net",
        }
    else:
        cot_history = {"dates": [], "line1": [], "line2": [], "label1": "", "label2": ""}

    # Fetch Yahoo prices and build call accuracy history
    print(f"  Fetching Yahoo Finance prices for {cfg['yahoo_symbol']}...")
    prices = _fetch_weekly_prices(cfg["yahoo_symbol"])
    call_history, call_accuracy = _build_call_history(
        leg_rows, fin_rows, cfg["hist_type"], prices
    )

    return dict(
        id=cfg["id"], label=cfg["label"], ticker=cfg["ticker"],
        accent=cfg["accent"], bg=cfg["bg"], subtitle=cfg["subtitle"],
        oi=f"{oi_val:,}", reportDate=report_date,
        cotHistory=cot_history,
        callHistory=call_history,
        callAccuracy=call_accuracy,
        **computed,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config_path = Path(__file__).parent / "config.json"
    chart_map:     dict = {}
    tv_symbol_map: dict = {}
    if config_path.exists():
        cfg_data      = json.loads(config_path.read_text(encoding="utf-8"))
        chart_map     = cfg_data.get("charts", {})
        tv_symbol_map = cfg_data.get("tvSymbols", {})

    assets = []
    for cfg in ASSETS_CFG:
        print(f"\nProcessing {cfg['label']}...")
        try:
            asset = build_asset(cfg)
            page_url = chart_map.get(cfg["id"], "")
            asset["chartPageUrl"] = page_url
            asset["chartImgUrl"]  = chart_img_url(page_url) if page_url else ""
            asset["tvSymbol"]     = tv_symbol_map.get(cfg["id"], "")
            assets.append(asset)
        except Exception as exc:
            import traceback
            print(f"  ERROR: {exc}", file=sys.stderr)
            traceback.print_exc()
            sys.exit(1)

    # Format report date
    report_date = assets[0].get("reportDate", datetime.now().strftime("%Y-%m-%d"))
    try:
        report_date = datetime.strptime(report_date, "%Y-%m-%d").strftime("%B %-d, %Y")
    except (ValueError, AttributeError):
        try:
            report_date = datetime.strptime(report_date, "%Y-%m-%d").strftime("%B %d, %Y")
        except Exception:
            pass

    template_path = Path(__file__).parent / "template.html"
    if not template_path.exists():
        print("ERROR: template.html not found", file=sys.stderr)
        sys.exit(1)

    template    = template_path.read_text(encoding="utf-8")
    assets_json = json.dumps(assets, ensure_ascii=False, indent=2)
    html = template.replace("%%ASSETS_JSON%%", assets_json).replace("%%REPORT_DATE%%", report_date)

    out_path = Path(__file__).parent / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"\nWritten: {out_path}  ({report_date})")


if __name__ == "__main__":
    main()
