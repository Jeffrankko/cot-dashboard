#!/usr/bin/env python3
"""
Weekly COT Dashboard Generator
Scrapes tradingster.com for COT position data and renders index.html from template.html.

Run after CFTC Friday 3:30 pm ET release (workflow fires at 08:00 UTC Saturday).

Usage:
    python generate.py
"""

import csv
import io
import re
import json
import sys
import time
import zipfile
from pathlib import Path
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ── Request config ────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tradingster.com/",
})

BASE = "https://www.tradingster.com"

# ── CFTC historical data URLs ─────────────────────────────────────────────────
CFTC_LEGACY_HIST    = "https://www.cftc.gov/files/dea/history/deahistfo.zip"
CFTC_LEGACY_CURRENT = "https://www.cftc.gov/dea/newcot/deafut.txt"
CFTC_FIN_HIST       = "https://www.cftc.gov/files/dea/history/HistFinFut.zip"
CFTC_FIN_CURRENT    = "https://www.cftc.gov/dea/newcot/FinFut.txt"
HISTORY_WEEKS       = 104  # 2 years
HISTORY_PATH        = Path(__file__).parent / "cot_history.json"

# ── Asset config ──────────────────────────────────────────────────────────────
ASSETS_CFG = [
    dict(
        id="gold", label="Gold", ticker="AU",
        accent="#d4a017", bg="rgba(212,160,23,0.18)",
        subtitle="COMEX · 100 Troy Oz/contract · COT Legacy Futures",
        legacy_url=f"{BASE}/cot/legacy-futures/088691",
        fin_url=None,
        cftc_code="088691",
        hist_type="legacy",
    ),
    dict(
        id="es", label="E-Mini S&P 500", ticker="ES",
        accent="#58a6ff", bg="rgba(88,166,255,0.15)",
        subtitle="CME · COT Legacy &amp; Financial Futures · Code 13874A",
        legacy_url=f"{BASE}/cot/legacy-futures/13874A",
        fin_url=f"{BASE}/cot/futures/fin/13874A",
        cftc_code="13874A",
        hist_type="legacy",
    ),
    dict(
        id="nq", label="NASDAQ-100 Mini", ticker="NQ",
        accent="#bc8cff", bg="rgba(188,140,255,0.15)",
        subtitle="CME · Disaggregated Financial COT · Code 209742",
        legacy_url=None,
        fin_url=f"{BASE}/cot/futures/fin/209742",
        cftc_code="209742",
        hist_type="financial",
    ),
    dict(
        id="cl", label="Crude Oil (WTI)", ticker="CL",
        accent="#e8724a", bg="rgba(232,114,74,0.15)",
        subtitle="NYMEX · 1,000 Barrels/contract · COT Legacy Futures · Code 067651",
        legacy_url=f"{BASE}/cot/legacy-futures/067651",
        fin_url=None,
        cftc_code="067651",
        hist_type="legacy",
    ),
]


# ── Parsing helpers ───────────────────────────────────────────────────────────

def to_int(text: str) -> int | None:
    """Convert '219,793' or '(48,171)' or '+7,979' to int."""
    if not text:
        return None
    text = text.strip().replace(",", "").replace(" ", "")
    text = text.replace("(", "-").replace(")", "")
    m = re.search(r"-?\d+", text)
    return int(m.group()) if m else None


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


def fetch(url: str) -> BeautifulSoup:
    time.sleep(1)
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def extract_date(soup: BeautifulSoup) -> str:
    text = soup.get_text(" ")
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    return datetime.now().strftime("%Y-%m-%d")


def table_rows(table) -> list[list[str]]:
    result = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["th", "td"])]
        if any(c for c in cells):
            result.append(cells)
    return result


# ── Legacy page parser ────────────────────────────────────────────────────────

def parse_legacy(soup: BeautifulSoup) -> dict:
    for table in soup.find_all("table"):
        raw = table_rows(table)
        if not raw:
            continue
        flat = " ".join(" ".join(r) for r in raw).lower()
        if "non-commercial" not in flat:
            continue

        header = [c.lower() for c in raw[0]]
        if any("non-commercial" in h for h in header):
            nc_col   = next((i for i, h in enumerate(header) if "non-commercial" in h), None)
            comm_col = next((i for i, h in enumerate(header) if "commercial" in h and "non" not in h), None)
            nr_col   = next((i for i, h in enumerate(header) if "non-reportable" in h or "nonreportable" in h), None)

            if nc_col is None:
                continue

            data: dict = {}
            for row in raw[1:]:
                label = row[0].lower() if row else ""
                nums  = [to_int(row[i]) if i < len(row) else None for i in range(len(header))]

                if re.search(r"\blong\b", label) and "nc_long" not in data:
                    data["nc_long"]   = nums[nc_col]
                    data["comm_long"] = nums[comm_col] if comm_col else None
                    data["nr_long"]   = nums[nr_col]   if nr_col   else None

                elif re.search(r"\bshort\b", label) and "nc_short" not in data:
                    data["nc_short"]   = nums[nc_col]
                    data["comm_short"] = nums[comm_col] if comm_col else None
                    data["nr_short"]   = nums[nr_col]   if nr_col   else None

                elif "open interest" in label and "oi" not in data:
                    for n in nums:
                        if n and n > 1000:
                            data["oi"] = n
                            break

                elif "change" in label:
                    if "chg_nc_long" not in data:
                        data["chg_nc_long"]  = nums[nc_col]
                    elif "chg_nc_short" not in data:
                        data["chg_nc_short"] = nums[nc_col]

            if "nc_long" in data and "nc_short" in data:
                return data

        header = [c.lower() for c in raw[0]]
        if any(h in ("long", "longs") for h in header):
            long_idx  = next((i for i, h in enumerate(header) if h in ("long", "longs")), None)
            short_idx = next((i for i, h in enumerate(header) if h in ("short", "shorts")), None)

            if long_idx is None or short_idx is None:
                continue

            data = {}
            for row in raw[1:]:
                label = row[0].lower() if row else ""
                if "non-commercial" in label:
                    data["nc_long"]  = to_int(row[long_idx])  if long_idx  < len(row) else None
                    data["nc_short"] = to_int(row[short_idx]) if short_idx < len(row) else None
                elif "commercial" in label and "non" not in label:
                    data["comm_long"]  = to_int(row[long_idx])  if long_idx  < len(row) else None
                    data["comm_short"] = to_int(row[short_idx]) if short_idx < len(row) else None
                elif "non-reportable" in label or "nonreportable" in label:
                    data["nr_long"]  = to_int(row[long_idx])  if long_idx  < len(row) else None
                    data["nr_short"] = to_int(row[short_idx]) if short_idx < len(row) else None
                elif "open interest" in label:
                    data["oi"] = to_int(row[1]) if len(row) > 1 else None

            if "nc_long" in data and "nc_short" in data:
                return data

    raise ValueError("Could not locate legacy COT table on page")


# ── Financial page parser ─────────────────────────────────────────────────────

def parse_financial(soup: BeautifulSoup) -> dict:
    CATEGORY_PATTERNS = {
        "dealer":   re.compile(r"dealer", re.I),
        "am":       re.compile(r"asset.?manager|institutional", re.I),
        "lev":      re.compile(r"leveraged", re.I),
        "other":    re.compile(r"other.?reportable", re.I),
    }

    for table in soup.find_all("table"):
        raw = table_rows(table)
        if not raw:
            continue
        flat = " ".join(" ".join(r) for r in raw).lower()
        if "dealer" not in flat or "leveraged" not in flat:
            continue

        header = [c.lower() for c in raw[0]]

        if any(h in ("long", "longs") for h in header):
            long_idx  = next((i for i, h in enumerate(header) if h in ("long", "longs")), None)
            short_idx = next((i for i, h in enumerate(header) if h in ("short", "shorts")), None)
            if long_idx is None or short_idx is None:
                continue

            data: dict = {}
            for row in raw[1:]:
                label = row[0] if row else ""
                for key, pat in CATEGORY_PATTERNS.items():
                    if pat.search(label):
                        data[f"{key}_long"]  = to_int(row[long_idx])  if long_idx  < len(row) else None
                        data[f"{key}_short"] = to_int(row[short_idx]) if short_idx < len(row) else None
                if "open interest" in label.lower():
                    data["oi"] = to_int(row[1]) if len(row) > 1 else None

            if "dealer_long" in data and "am_long" in data and "lev_long" in data:
                return data

        dealer_col = am_col = lev_col = other_col = None
        for i, h in enumerate(header):
            if CATEGORY_PATTERNS["dealer"].search(h): dealer_col = i
            elif CATEGORY_PATTERNS["am"].search(h):   am_col     = i
            elif CATEGORY_PATTERNS["lev"].search(h):  lev_col    = i
            elif CATEGORY_PATTERNS["other"].search(h): other_col = i

        if dealer_col and am_col and lev_col:
            data = {}
            for row in raw[1:]:
                label = row[0].lower() if row else ""
                nums  = [to_int(row[i]) if i < len(row) else None for i in range(len(header))]

                if re.search(r"\blong\b", label) and "dealer_long" not in data:
                    data["dealer_long"] = nums[dealer_col]
                    data["am_long"]     = nums[am_col]
                    data["lev_long"]    = nums[lev_col]
                    data["other_long"]  = nums[other_col] if other_col else None

                elif re.search(r"\bshort\b", label) and "dealer_short" not in data:
                    data["dealer_short"] = nums[dealer_col]
                    data["am_short"]     = nums[am_col]
                    data["lev_short"]    = nums[lev_col]
                    data["other_short"]  = nums[other_col] if other_col else None

                elif "open interest" in label and "oi" not in data:
                    for n in nums:
                        if n and n > 100:
                            data["oi"] = n
                            break

            if "dealer_long" in data and "am_long" in data:
                return data

    raise ValueError("Could not locate financial COT table on page")


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
                  f"trimmed <strong>{fmtabs(chg_nc_l)} longs</strong>")
    short_move = (f"added {fmtabs(chg_nc_s)} new shorts"
                  if chg_nc_s >= 0 else
                  f"cut {fmtabs(chg_nc_s)} shorts")

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
        lev_chg_txt = (f" Leveraged funds covered <strong>{fmtabs(chg_ls)} short contracts</strong> "
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
                dict(type="Commercial",     sub="Institutional / Hedgers",
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
                 longPct=long_pct(dl, ds),  shortPct=long_pct(ds, dl)),
            dict(type="Asset Manager",          sub="Institutional long-only",
                 long=f"{aml:,}", short=f"{ams:,}", net=fmtnum(am_net),
                 netCls="pos" if am_net >= 0 else "neg",
                 longPct=am_lp, shortPct=100 - am_lp),
            dict(type="Leveraged Funds",         sub="Hedge funds / CTAs",
                 long=f"{ll:,}",  short=f"{ls:,}",  net=fmtnum(lev_net),
                 netCls="pos" if lev_net >= 0 else "neg",
                 longPct=lev_lp, shortPct=100 - lev_lp),
            dict(type="Other Reportables",       sub="Other institutional",
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


# ── Build individual asset ────────────────────────────────────────────────────

def build_asset(cfg: dict) -> tuple[dict, dict | None]:
    """Returns (asset_dict, current_nets).
    current_nets = {date, line1, line2} for updating cot_history.
    """
    d_leg = d_fin = None
    report_date = datetime.now().strftime("%Y-%m-%d")

    if cfg["legacy_url"]:
        print(f"  Fetching legacy: {cfg['legacy_url']}")
        soup = fetch(cfg["legacy_url"])
        report_date = extract_date(soup)
        d_leg = parse_legacy(soup)
        print(f"    nc_long={d_leg.get('nc_long')} nc_short={d_leg.get('nc_short')} oi={d_leg.get('oi')}")

    if cfg["fin_url"]:
        print(f"  Fetching financial: {cfg['fin_url']}")
        soup = fetch(cfg["fin_url"])
        if not d_leg:
            report_date = extract_date(soup)
        d_fin = parse_financial(soup)
        print(f"    am_long={d_fin.get('am_long')} lev_short={d_fin.get('lev_short')} oi={d_fin.get('oi')}")

    if d_fin:
        computed = financial_analysis(d_fin, d_leg)
        oi_val = d_fin.get("oi") or (d_leg.get("oi") if d_leg else 0) or 0
    else:
        computed = legacy_analysis(d_leg)
        oi_val = d_leg.get("oi") or 0

    # Extract current-week net positions for history update
    current_nets = None
    if cfg["hist_type"] == "legacy" and d_leg:
        nc_l = d_leg.get("nc_long")    or 0
        nc_s = d_leg.get("nc_short")   or 0
        co_l = d_leg.get("comm_long")  or 0
        co_s = d_leg.get("comm_short") or 0
        current_nets = {"date": report_date, "line1": nc_l - nc_s, "line2": co_l - co_s}
    elif cfg["hist_type"] == "financial" and d_fin:
        am_l = d_fin.get("am_long")   or 0
        am_s = d_fin.get("am_short")  or 0
        lv_l = d_fin.get("lev_long")  or 0
        lv_s = d_fin.get("lev_short") or 0
        current_nets = {"date": report_date, "line1": am_l - am_s, "line2": lv_l - lv_s}

    asset = dict(
        id=cfg["id"],
        label=cfg["label"],
        ticker=cfg["ticker"],
        accent=cfg["accent"],
        bg=cfg["bg"],
        subtitle=cfg["subtitle"],
        oi=f"{oi_val:,}",
        reportDate=report_date,
        **computed,
    )
    return asset, current_nets


def chart_img_url(page_url: str) -> str:
    m = re.search(r"/x/([A-Za-z0-9]+)/?$", page_url)
    if m:
        code = m.group(1)
        return f"https://s3.tradingview.com/snapshots/{code[0].lower()}/{code}.png"
    return ""


# ── CFTC history functions ────────────────────────────────────────────────────

def _cftc_rows_from_zip(zip_url: str) -> list:
    """Download CFTC zip and return all rows from every CSV inside (headers skipped)."""
    print(f"    Downloading {zip_url} ...")
    resp = SESSION.get(zip_url, timeout=300)
    resp.raise_for_status()
    all_rows: list = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith((".txt", ".csv")):
                continue
            with zf.open(name) as f:
                text = f.read().decode("latin-1", errors="replace")
            rows = list(csv.reader(io.StringIO(text)))
            all_rows.extend(rows[1:])  # skip each file's header row
    return all_rows


def _cftc_rows_from_url(url: str) -> list:
    """Download a CFTC current-year text/CSV file and return rows (header skipped)."""
    print(f"    Downloading {url} ...")
    resp = SESSION.get(url, timeout=60)
    resp.raise_for_status()
    rows = list(csv.reader(io.StringIO(resp.text)))
    return rows[1:]


def _parse_cftc_rows(
    all_rows: list, cftc_code: str, is_financial: bool, cutoff_date: str
) -> dict:
    """Filter CFTC rows by code and date, return {date_str: (line1_net, line2_net)}.

    Legacy columns:   nc_long=8, nc_short=9, comm_long=11, comm_short=12
    Financial columns: am_long=11, am_short=12, lev_long=14, lev_short=15
    Date column: 2 (MM/DD/YYYY), Code column: 3
    """
    code_col = 3
    date_col = 2
    if is_financial:
        c1l, c1s, c2l, c2s = 11, 12, 14, 15
    else:
        c1l, c1s, c2l, c2s = 8, 9, 11, 12

    min_len = max(code_col, date_col, c1l, c1s, c2l, c2s) + 1
    records: dict = {}

    for row in all_rows:
        if len(row) < min_len:
            continue
        if row[code_col].strip() != cftc_code:
            continue
        try:
            date_str = datetime.strptime(row[date_col].strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        if date_str < cutoff_date:
            continue
        try:
            def _i(s: str) -> int:
                return int(s.replace(",", "").strip() or "0")
            records[date_str] = (_i(row[c1l]) - _i(row[c1s]), _i(row[c2l]) - _i(row[c2s]))
        except (ValueError, IndexError):
            continue

    return records


def _series_from_records(records: dict, is_financial: bool) -> dict:
    """Convert {date: (line1, line2)} to sorted series dict for the chart."""
    dates = sorted(records.keys())
    return {
        "dates":  dates,
        "line1":  [records[d][0] for d in dates],
        "line2":  [records[d][1] for d in dates],
        "label1": "Asset Manager Net" if is_financial else "Non-Commercial Net",
        "label2": "Leveraged Funds Net" if is_financial else "Commercial Net",
    }


def _records_from_series(series: dict) -> dict:
    """Reconstruct {date: (line1, line2)} from a stored series dict."""
    dates = series.get("dates", [])
    l1    = series.get("line1", [])
    l2    = series.get("line2", [])
    return {d: (l1[i] if i < len(l1) else 0, l2[i] if i < len(l2) else 0)
            for i, d in enumerate(dates)}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load config.json
    config_path = Path(__file__).parent / "config.json"
    chart_map: dict = {}
    tv_symbol_map: dict = {}
    if config_path.exists():
        cfg_data = json.loads(config_path.read_text(encoding="utf-8"))
        chart_map     = cfg_data.get("charts", {})
        tv_symbol_map = cfg_data.get("tvSymbols", {})
    else:
        print("WARNING: config.json not found", file=sys.stderr)

    # Load existing COT history
    raw_history: dict = {}
    if HISTORY_PATH.exists():
        try:
            raw_history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"WARNING: could not read {HISTORY_PATH}: {exc}", file=sys.stderr)

    cutoff_date = (datetime.now() - timedelta(weeks=HISTORY_WEEKS)).strftime("%Y-%m-%d")

    # Determine which CFTC zip files we need to download (only on bootstrap)
    needs_legacy_zip = any(
        cfg["hist_type"] == "legacy" and
        len(raw_history.get(cfg["id"], {}).get("dates", [])) < 50
        for cfg in ASSETS_CFG
    )
    needs_fin_zip = any(
        cfg["hist_type"] == "financial" and
        len(raw_history.get(cfg["id"], {}).get("dates", [])) < 50
        for cfg in ASSETS_CFG
    )

    legacy_zip_rows: list = []
    fin_zip_rows:    list = []

    if needs_legacy_zip:
        print("\nBootstrapping legacy CFTC history (one-time download)...")
        try:
            legacy_zip_rows = (
                _cftc_rows_from_zip(CFTC_LEGACY_HIST) +
                _cftc_rows_from_url(CFTC_LEGACY_CURRENT)
            )
        except Exception as exc:
            print(f"WARNING: CFTC legacy download failed: {exc}", file=sys.stderr)

    if needs_fin_zip:
        print("\nBootstrapping financial CFTC history (one-time download)...")
        try:
            fin_zip_rows = (
                _cftc_rows_from_zip(CFTC_FIN_HIST) +
                _cftc_rows_from_url(CFTC_FIN_CURRENT)
            )
        except Exception as exc:
            print(f"WARNING: CFTC financial download failed: {exc}", file=sys.stderr)

    # Build each asset
    assets = []
    for cfg in ASSETS_CFG:
        print(f"\nProcessing {cfg['label']}...")
        try:
            asset, current_nets = build_asset(cfg)

            is_financial = cfg["hist_type"] == "financial"
            stored = raw_history.get(cfg["id"], {})
            stored_dates = stored.get("dates", [])

            # Rebuild records from whatever source we have
            if len(stored_dates) < 50:
                # Bootstrap from CFTC zip data
                zip_rows = fin_zip_rows if is_financial else legacy_zip_rows
                if zip_rows:
                    records = _parse_cftc_rows(zip_rows, cfg["cftc_code"], is_financial, cutoff_date)
                    print(f"  Bootstrapped {len(records)} weeks of CFTC history")
                else:
                    records = {}
                    print(f"  WARNING: no CFTC data available for history bootstrap")
            else:
                records = _records_from_series(stored)

            # Add / refresh current week's data point from the live scrape
            if current_nets:
                records[current_nets["date"]] = (current_nets["line1"], current_nets["line2"])

            # Trim to rolling HISTORY_WEEKS window
            all_dates = sorted(records.keys())
            if len(all_dates) > HISTORY_WEEKS:
                keep = set(all_dates[-HISTORY_WEEKS:])
                records = {d: v for d, v in records.items() if d in keep}

            series = _series_from_records(records, is_financial)
            raw_history[cfg["id"]] = series

            # Attach chart metadata
            page_url = chart_map.get(cfg["id"], "")
            asset["chartPageUrl"] = page_url
            asset["chartImgUrl"]  = chart_img_url(page_url) if page_url else ""
            asset["tvSymbol"]     = tv_symbol_map.get(cfg["id"], "")
            asset["cotHistory"]   = series

            assets.append(asset)

        except Exception as e:
            import traceback
            print(f"  ERROR: {e}", file=sys.stderr)
            traceback.print_exc()
            sys.exit(1)

    # Persist updated history
    HISTORY_PATH.write_text(json.dumps(raw_history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved: {HISTORY_PATH}")

    # Format report date for display
    report_date = assets[0].get("reportDate", datetime.now().strftime("%Y-%m-%d"))
    try:
        report_date = datetime.strptime(report_date, "%Y-%m-%d").strftime("%B %-d, %Y")
    except (ValueError, AttributeError):
        try:
            report_date = datetime.strptime(report_date, "%Y-%m-%d").strftime("%B %d, %Y")
        except Exception:
            pass

    # Render template
    template_path = Path(__file__).parent / "template.html"
    if not template_path.exists():
        print("ERROR: template.html not found", file=sys.stderr)
        sys.exit(1)

    template   = template_path.read_text(encoding="utf-8")
    assets_json = json.dumps(assets, ensure_ascii=False, indent=2)
    html = template.replace("%%ASSETS_JSON%%", assets_json).replace("%%REPORT_DATE%%", report_date)

    out_path = Path(__file__).parent / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Written: {out_path}  (report date: {report_date})")


if __name__ == "__main__":
    main()
