"""Build per-BMU Transmission Loss Multiplier (TLM) reports for each Elexon BSC season.

Background
    BSC Section T: TLM = 1 + TLF(zone, season) + TLMO±(settlement period).

    The headline ``TLM`` column produced here is ``1 + TLF(zone, season)``. That is the same
    definition as the original "Winter 2022 TLMs.xlsx" report this tool replaces. Where the
    optional ``data/tlm.csv`` covers a season, settled season averages (which include TLMO±)
    are shown alongside.

Method (details and evidence in docs/METHOD.md)
    1. Each BMU is put in a TLM zone (1-14, matching GSP Groups _A.._P). Its registered TLF is
       matched to the current zone TLFs, falling back to its GSP Group.
    2. Each zone's seasonal TLF is recovered from Elexon Insights API settlement-stack TLMs
       (which equal 1 + TLF + ETLMO±) minus Elexon's published ETLMO values, by voting across
       BMUs.
    3. Where tlm.csv covers the period, its exact zone-to-zone TLF differences are used, and
       the API only sets the common level.

Seasons (Elexon BSC seasons; TLFs are set per season)
    Season YYYY belongs to BSC Year YYYY/YY+1 (1 Apr YYYY - 31 Mar YYYY+1):
        Spring YYYY = Apr-May YYYY + Mar YYYY+1   (Elexon "Spring A" + "Spring B")
        Summer YYYY = Jun-Aug YYYY
        Autumn YYYY = Sep-Nov YYYY
        Winter YYYY = Dec YYYY - Feb YYYY+1

Usage
    python tlm_report.py                                   # latest complete BSC winter
    python tlm_report.py --season "Winter 2022" --compare "data/Winter 2022 TLMs.xlsx"
    python tlm_report.py --bsc-year 2022 --bsc-year 2023   # all four seasons of each BSC Year
    python tlm_report.py --start 2025-10-01 --end 2026-03-31 --label "Oct25-Mar26"
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import elexon_api as api

# --------------------------------------------------------------------------- configuration

ROOT = Path(__file__).resolve().parent
DEFAULT_TLM = ROOT / "data" / "tlm.csv"
DEFAULT_REG = ROOT / "data" / "reg_bm_units.csv"
DEFAULT_OUT_DIR = ROOT / "output"
DEFAULT_CACHE = ROOT / "cache"

# Settlement periods sampled per day from the API (every 4 hours). More periods give more
# evidence per zone at the cost of more API calls (2 calls per period: bid and offer stacks).
DEFAULT_SPS = [2, 10, 18, 26, 34, 42]

# GSP Group Id -> (TLM zone number, GSP Group name). TLM zones 1-14 follow GSP Group order.
GSP_ZONE: dict[str, tuple[int, str]] = {
    "_A": (1, "Eastern"),
    "_B": (2, "East Midlands"),
    "_C": (3, "London"),
    "_D": (4, "Merseyside and North Wales"),
    "_E": (5, "Midlands"),
    "_F": (6, "Northern"),
    "_G": (7, "North Western"),
    "_H": (8, "Southern"),
    "_J": (9, "South Eastern"),
    "_K": (10, "South Wales"),
    "_L": (11, "South Western"),
    "_M": (12, "Yorkshire"),
    "_N": (13, "South Scotland"),
    "_P": (14, "North Scotland"),
}
ZONE_GSP = {zone: gsp for gsp, (zone, _) in GSP_ZONE.items()}
ZONES = range(1, 15)

# Elexon BSC seasons. Season YYYY belongs to BSC Year YYYY/YY+1 and may have several parts,
# each given as (year offset, start month, number of months). Spring is split by the BSC Year
# boundary into Apr-May and the following March, which share the same TLFs.
BSC_SEASONS: dict[str, list[tuple[int, int, int]]] = {
    "spring": [(0, 4, 2), (1, 3, 1)],   # Apr-May YYYY + Mar YYYY+1
    "summer": [(0, 6, 3)],              # Jun-Aug YYYY
    "autumn": [(0, 9, 3)],              # Sep-Nov YYYY
    "winter": [(0, 12, 3)],             # Dec YYYY - Feb YYYY+1
}

Segments = list[tuple[date, date]]
"""Inclusive (start, end) date ranges that together make up one report period."""

TLF_DP = 7      # TLFs/TLMs are published to 7 decimal places
VOTE_DP = 6     # candidate values are grouped at 6 d.p. when voting (absorbs float noise)
TLF_CHANGE_TOL = 1e-6   # zone value differences below this are treated as "no change"

# Confidence labels, weakest first. A season's confidence is the weakest of its bands.
CONF_ORDER = ["None", "Low", "Medium (decided by P/C status)", "Medium", "High"]

TLM_COLUMNS = ["Settlement Date", "Settlement Run Type", "Settlement Period", "Zone",
               "Off Taking", "Delivering"]


# --------------------------------------------------------------------------- seasons


def season_segments(name: str) -> Segments:
    """Return the date segments of a BSC season.

    Examples:
        "Winter 2022" -> [(2022-12-01, 2023-02-28)]
        "Spring 2025" -> [(2025-04-01, 2025-05-31), (2026-03-01, 2026-03-31)]

    Raises:
        ValueError: if ``name`` isn't "<Spring|Summer|Autumn|Winter> <year>".
    """
    try:
        kind, year_text = name.split()
        parts = BSC_SEASONS[kind.lower()]
        year = int(year_text)
    except (ValueError, KeyError):
        raise ValueError(f"Bad season {name!r}; use e.g. 'Winter 2022' "
                         "(Spring/Summer/Autumn/Winter)") from None
    segments = []
    for offset, month, months in parts:
        start = date(year + offset, month, 1)
        end = (pd.Timestamp(start) + pd.DateOffset(months=months) - pd.Timedelta(days=1)).date()
        segments.append((start, end))
    return segments


def bsc_year_seasons(year: int) -> list[str]:
    """Names of the four seasons of BSC Year ``year``/``year+1``, e.g. ["Spring 2025", ...]."""
    return [f"{kind.title()} {year}" for kind in BSC_SEASONS]


def describe(segments: Segments) -> str:
    """Human-readable form of date segments, e.g. "2025-04-01 to 2025-05-31 + ..."."""
    return " + ".join(f"{start} to {end}" for start, end in segments)


def latest_complete_winter(as_of: date) -> str:
    """Name of the most recent BSC Winter (Dec-Feb) that has ended by ``as_of``."""
    # Winter Y runs Dec Y - Feb Y+1, so from March of year Y+1 onwards Winter Y is complete.
    end_year = as_of.year if as_of.month >= 3 else as_of.year - 1
    return f"Winter {end_year - 1}"


# --------------------------------------------------------------------------- loading


def load_registration(path: Path) -> pd.DataFrame:
    """Read the Elexon Portal "Registered BM Units" CSV.

    The file starts with a title line and a blank line before the header row. Text columns
    used later are stripped, with empty strings turned into missing values, and ``TLF`` is
    made numeric.
    """
    reg = pd.read_csv(path, skiprows=2, dtype={"BM Unit ID": str})
    reg.columns = [c.strip() for c in reg.columns]
    reg = reg.dropna(subset=["BM Unit ID"])
    reg["BM Unit ID"] = reg["BM Unit ID"].str.strip()
    for col in ("Prod/Cons Status", "Prod/Cons Flag", "GSP Group Id", "BMU Type"):
        reg[col] = reg[col].astype("string").str.strip().replace("", pd.NA)
    reg["TLF"] = pd.to_numeric(reg["TLF"], errors="coerce")
    return reg


def load_tlm(path: Path) -> pd.DataFrame:
    """Read the optional Elexon zonal settled TLM CSV (one row per date, period and zone).

    If the file doesn't exist, an empty frame with the expected columns is returned. Zone
    TLFs then come from the API alone, and the settled-average columns are left blank.
    """
    if not path.exists():
        print(f"Note: {path} not found - API-only zone TLFs, no settled averages.")
        return pd.DataFrame(columns=TLM_COLUMNS)
    tlm = pd.read_csv(path)
    tlm.columns = [c.strip() for c in tlm.columns]
    tlm["Settlement Date"] = pd.to_datetime(tlm["Settlement Date"], dayfirst=True).dt.date
    return tlm


# --------------------------------------------------------------------------- zones


def zone_tlf_table(reg: pd.DataFrame) -> pd.DataFrame:
    """Current TLF of each zone: the most common TLF among supplier (S) BMUs in its GSP Group.

    Returns:
        One row per zone: Zone, GSP Group Id, GSP Group Name, Current Zone TLF.

    Raises:
        ValueError: if a GSP Group has no supplier BMUs, or two zones share a TLF. Zones are
            identified by TLF, so the values must be distinct.
    """
    suppliers = reg[(reg["BMU Type"] == "S") & reg["GSP Group Id"].isin(GSP_ZONE.keys())]
    tlf = suppliers.groupby("GSP Group Id")["TLF"].agg(lambda s: s.mode().iloc[0])
    table = pd.DataFrame([
        {"Zone": zone, "GSP Group Id": gsp, "GSP Group Name": name, "Current Zone TLF": tlf.get(gsp)}
        for gsp, (zone, name) in GSP_ZONE.items()
    ])
    if table["Current Zone TLF"].isna().any():
        missing = list(table.loc[table["Current Zone TLF"].isna(), "GSP Group Id"])
        raise ValueError(f"No supplier BMUs to derive zone TLF for {missing}")
    if table["Current Zone TLF"].round(TLF_DP).duplicated().any():
        raise ValueError("Zone TLFs are not unique; TLF cannot identify zone")
    return table


def assign_zones(reg: pd.DataFrame, zones: pd.DataFrame) -> pd.DataFrame:
    """Add each BMU's TLM zone to the registration frame.

    The BMU's registered TLF is matched to the current zone TLFs. This covers transmission
    (T_) and interconnector (I_) units, which have no GSP Group. Where the TLF doesn't match,
    the GSP Group is used.

    Adds the columns ``Zone`` (nullable int), ``Zone Source`` ("TLF", "GSP Group" or
    "Unmapped") and ``_notes`` (a list of strings per BMU, used for the report's Notes column).
    """
    tlf_to_zone = dict(zip(zones["Current Zone TLF"].round(TLF_DP), zones["Zone"]))
    out = reg.copy()
    by_tlf = out["TLF"].round(TLF_DP).map(tlf_to_zone)
    by_gsp = out["GSP Group Id"].map(lambda g: GSP_ZONE[g][0] if g in GSP_ZONE else pd.NA)

    out["Zone"] = by_tlf.fillna(by_gsp).astype("Int64")
    out["Zone Source"] = "Unmapped"
    out.loc[by_gsp.notna(), "Zone Source"] = "GSP Group"
    out.loc[by_tlf.notna(), "Zone Source"] = "TLF"

    notes: list[list[str]] = []
    for t, g in zip(by_tlf, by_gsp):
        n = []
        if pd.notna(t) and pd.notna(g) and t != g:
            n.append(f"TLF matches zone {int(t)} ({ZONE_GSP[int(t)]}) but GSP Group is zone "
                     f"{int(g)}; used TLF")
        if pd.isna(t) and pd.notna(g):
            n.append("TLF does not match a zone TLF; zone from GSP Group")
        if pd.isna(t) and pd.isna(g):
            n.append("No zone: TLF does not match a zone and no GSP Group")
        notes.append(n)
    out["_notes"] = notes
    return out


# --------------------------------------------------------------------------- seasonal TLF


def _candidates(obs: pd.DataFrame, e_plus: float, e_minus: float) -> pd.DataFrame:
    """Turn each stack TLM into its two candidate values for 1 + TLF.

    It's not known whether the BMU was delivering or offtaking, so both are kept:
    ``TLM - ETLMO+`` (tag "P", delivering) and ``TLM - ETLMO-`` (tag "C", offtaking).
    """
    delivering = obs.assign(Value=obs["Stack TLM"] - e_plus, Tag="P")
    offtaking = obs.assign(Value=obs["Stack TLM"] - e_minus, Tag="C")
    return pd.concat([delivering, offtaking], ignore_index=True)


def _pick(cands: pd.DataFrame) -> tuple[float, int, str]:
    """Choose the winning candidate ``Key`` by voting.

    Votes are counted as distinct BMUs supporting each key. Ties go to the key backed by more
    BMUs whose registered P/C status matches the candidate's tag. Any remaining tie is broken
    by the smallest key, so results are deterministic, and is labelled "Low".

    Args:
        cands: candidate rows with columns ``Key``, ``BM Unit ID``, ``Tag`` and ``Status``.

    Returns:
        (winning key, number of distinct BMUs supporting it, confidence label)
    """
    cands = cands.assign(StatusMatch=cands["Status"] == cands["Tag"])
    votes = (cands.groupby("Key")
             .agg(Support=("BM Unit ID", "nunique"),
                  StatusSupport=("BM Unit ID",
                                 lambda s: s[cands.loc[s.index, "StatusMatch"]].nunique()))
             .reset_index()
             .sort_values(["Support", "StatusSupport", "Key"], ascending=[False, False, True]))
    top = votes.iloc[0]
    second = votes.iloc[1] if len(votes) > 1 else None
    if second is None or top.Support > second.Support:
        confidence = "High" if top.Support >= 2 else "Medium"
    elif top.StatusSupport > second.StatusSupport:
        confidence = "Medium (decided by P/C status)"
    else:
        confidence = "Low"
    return top.Key, int(top.Support), confidence


def band_zone_tlf(obs: pd.DataFrame, e_plus: float, e_minus: float, bmu_zone: dict[str, int],
                  bmu_status: dict[str, str], rel: pd.Series | None = None) -> pd.DataFrame:
    """Recover 1 + TLF for every zone within one ETLMO band (a period of constant ETLMO).

    Args:
        obs: distinct (``BM Unit ID``, ``Stack TLM``) pairs from the API for the band.
        e_plus, e_minus: the band's ETLMO+ and ETLMO-.
        bmu_zone: BMU ID -> zone. BMUs without a zone are ignored.
        bmu_status: BMU ID -> registered P/C status, used to break ties.
        rel: optional exact TLF(z) - TLF(1) per zone from tlm.csv. If given, a single vote
            across all zones sets the common level. Otherwise each zone is voted separately.

    Returns:
        One row per zone: Zone, 1 + TLF (NaN if no data), Method, API BMUs, Confidence.
    """
    obs = obs[obs["BM Unit ID"].isin(bmu_zone.keys())].copy()
    obs["Zone"] = obs["BM Unit ID"].map(bmu_zone).astype(int)
    obs["Status"] = obs["BM Unit ID"].map(bmu_status)
    cands = _candidates(obs, e_plus, e_minus)

    if rel is not None and not cands.empty:
        # Anchored: every candidate implies a value for 1 + TLF(zone 1); vote on that level.
        cands["Level"] = cands["Value"] - cands["Zone"].map(rel)
        cands["Key"] = cands["Level"].round(VOTE_DP)
        key, support, confidence = _pick(cands)
        winners = cands[cands["Key"] == key]
        level = winners["Level"].median()
        per_zone = winners.groupby("Zone")["BM Unit ID"].nunique()
        return pd.DataFrame({
            "Zone": list(ZONES),
            "1 + TLF": [round(level + rel[z], TLF_DP) for z in ZONES],
            "Method": "tlm.csv zone differences + API level",
            "API BMUs": [int(per_zone.get(z, 0)) for z in ZONES],
            "Confidence": confidence if support >= 3 else "Low",
        })

    # API only: vote separately in each zone.
    rows = []
    for z in ZONES:
        zc = cands[cands["Zone"] == z].copy()
        if zc.empty:
            rows.append({"Zone": z, "1 + TLF": float("nan"), "Method": "API", "API BMUs": 0,
                         "Confidence": "None"})
            continue
        zc["Key"] = zc["Value"].round(VOTE_DP)
        key, support, confidence = _pick(zc)
        value = zc.loc[zc["Key"] == key, "Value"].median()
        rows.append({"Zone": z, "1 + TLF": round(value, TLF_DP), "Method": "API",
                     "API BMUs": support, "Confidence": confidence})
    return pd.DataFrame(rows)


def tlm_relative(tlm: pd.DataFrame, start: date, end: date) -> pd.Series | None:
    """Exact zone TLF differences, TLF(z) - TLF(1), from tlm.csv for [start, end].

    Within any settlement period, every zone gets the same TLMO, so OffTaking(z) -
    OffTaking(1) = TLF(z) - TLF(1).

    Returns:
        A Series indexed by zone, or None if tlm.csv doesn't cover every day of the range or
        the differences change within it.
    """
    sel = tlm[(tlm["Settlement Date"] >= start) & (tlm["Settlement Date"] <= end)]
    if sel["Settlement Date"].nunique() != (end - start).days + 1:
        return None
    piv = sel.pivot_table(index=["Settlement Date", "Settlement Period"], columns="Zone",
                          values="Off Taking")
    rel = piv.sub(piv[1], axis=0)
    if float((rel.max() - rel.min()).max()) > TLF_CHANGE_TOL:
        return None
    return rel.mean().round(TLF_DP)


def season_zone_tlf(segments: Segments, tlm: pd.DataFrame, reg: pd.DataFrame,
                    etlmo: pd.DataFrame, sps: list[int], cache: Path,
                    ) -> tuple[pd.DataFrame, list[str]]:
    """Recover each zone's 1 + TLF for a report period, which may span several ETLMO bands.

    Each band is solved with :func:`band_zone_tlf`. The bands are then combined as a
    day-weighted average per zone. Bands with no data for a zone (e.g. stacks holding only
    1.0 placeholders) are left out of that zone's average and flagged, rather than diluting it.

    Args:
        segments: date ranges making up the period.
        tlm: zonal settled TLMs (may be empty).
        reg: registration frame after :func:`assign_zones`.
        etlmo: ETLMO table from :func:`elexon_api.load_etlmo`.
        sps: settlement periods to sample per day.
        cache: download cache directory.

    Returns:
        (one row per zone: Zone, Days, Days With Data, API BMUs, Method, Confidence, 1 + TLF,
        Season Zone TLF; log lines describing each band and any warnings)
    """
    bmu_zone = {b: int(z) for b, z in zip(reg["BM Unit ID"], reg["Zone"]) if pd.notna(z)}
    bmu_status = dict(zip(reg["BM Unit ID"],
                          reg["Prod/Cons Status"].fillna(reg["Prod/Cons Flag"])))
    parts, log = [], []
    bands = [b for start, end in segments for b in api.etlmo_bands(etlmo, start, end)]
    for band in bands:
        obs = api.stack_tlms(band["From"], band["To"], sps, cache)
        rel = tlm_relative(tlm, band["From"], band["To"])
        res = band_zone_tlf(obs, band["ETLMO+"], band["ETLMO-"], bmu_zone, bmu_status, rel)
        res["Days"] = (band["To"] - band["From"]).days + 1
        parts.append(res)
        weak = res.loc[res["Confidence"].isin(["Low", "None"]), "Zone"].tolist()
        log.append(f"{band['From']}..{band['To']}: ETLMO+ {band['ETLMO+']}, "
                   f"ETLMO- {band['ETLMO-']}; {len(obs)} API (BMU, TLM) pairs; "
                   f"method: {res['Method'].iloc[0]}; low-confidence zones: {weak or 'none'}")
    allp = pd.concat(parts)

    # TLFs should be constant within a BSC season; flag zones where the bands disagree
    # (e.g. Spring 2025, after Elexon's mid-April 2025 TLF correction).
    spread = allp.groupby("Zone")["1 + TLF"].agg(
        lambda s: s.max() - s.min() if s.notna().any() else 0.0)
    changed = spread[spread > TLF_CHANGE_TOL]
    if not changed.empty:
        log.append("WARNING: zone TLFs change within this season, so the value is a day-weighted "
                   f"blend (max change {changed.max():.5f}; zones {list(changed.index)})")

    has_data = allp["1 + TLF"].notna()
    allp["Weighted"] = allp["1 + TLF"].where(has_data, 0) * allp["Days"]
    allp["DaysWithData"] = allp["Days"].where(has_data, 0)
    out = allp.groupby("Zone").agg(
        Weighted=("Weighted", "sum"),
        Days=("Days", "sum"),
        **{"Days With Data": ("DaysWithData", "sum"), "API BMUs": ("API BMUs", "sum")},
        Method=("Method", "first"),
        # weakest label among bands that had data
        Confidence=("Confidence", lambda s: min((c for c in s if c != "None"),
                                                key=CONF_ORDER.index, default="None")),
    )
    out["1 + TLF"] = (out["Weighted"] / out["Days With Data"].where(out["Days With Data"] > 0)
                      ).round(TLF_DP)
    out["Season Zone TLF"] = (out["1 + TLF"] - 1).round(TLF_DP)

    partial = (out["Days With Data"] > 0) & (out["Days With Data"] < out["Days"])
    out.loc[partial, "Confidence"] = out.loc[partial].apply(
        lambda r: f"{r['Confidence']}; data for {int(r['Days With Data'])} of "
                  f"{int(r['Days'])} days", axis=1)
    if partial.any():
        log.append("Zones averaged over part of the range (no API data in some bands): "
                   + ", ".join(f"zone {z} ({int(d)}/{int(t)} days)" for z, d, t in
                               zip(out.index[partial], out.loc[partial, "Days With Data"],
                                   out.loc[partial, "Days"])))
    out.loc[out.index.isin(changed.index), "Confidence"] += "; TLF changed within season (blend)"
    return out.drop(columns=["Weighted"]).reset_index(), log


# --------------------------------------------------------------------------- settled TLMs


def zone_averages(tlm: pd.DataFrame, segments: Segments) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Average the settled zonal TLMs from tlm.csv over the date segments.

    Returns:
        (one row per zone with average/min/max Delivering and Off Taking TLMs, the number of
        settlement periods, and the number of periods where Delivering == Off Taking;
        the settlement run types covering the range)

    Raises:
        ValueError: if tlm.csv is empty or doesn't cover the whole range.
    """
    if tlm.empty:
        raise ValueError("no tlm.csv data")
    lo, hi = tlm["Settlement Date"].min(), tlm["Settlement Date"].max()
    for start, end in segments:
        if start > end:
            raise ValueError("start must be on or before end")
        if start < lo or end > hi:
            raise ValueError(f"Requested {start}..{end} is outside TLM file coverage {lo}..{hi}")

    mask = pd.Series(False, index=tlm.index)
    for start, end in segments:
        mask |= (tlm["Settlement Date"] >= start) & (tlm["Settlement Date"] <= end)
    sel = tlm[mask].copy()
    sel["Anomalous"] = (sel["Off Taking"] - sel["Delivering"]).abs() < 1e-9
    stats = sel.groupby("Zone").agg(**{
        "Avg Delivering TLM": ("Delivering", "mean"),
        "Min Delivering TLM": ("Delivering", "min"),
        "Max Delivering TLM": ("Delivering", "max"),
        "Avg Off Taking TLM": ("Off Taking", "mean"),
        "Min Off Taking TLM": ("Off Taking", "min"),
        "Max Off Taking TLM": ("Off Taking", "max"),
        "Settlement Periods": ("Delivering", "size"),
        "Periods Delivering = Off Taking": ("Anomalous", "sum"),
    }).reset_index()

    runs = (sel.drop_duplicates(["Settlement Date", "Settlement Run Type"])
            .groupby("Settlement Run Type")["Settlement Date"].agg(["min", "max", "nunique"])
            .rename(columns={"min": "From", "max": "To", "nunique": "Days"})
            .sort_values("From").reset_index())

    missing = sum((end - start).days + 1 for start, end in segments) - sel["Settlement Date"].nunique()
    if missing:
        print(f"  WARNING: {missing} day(s) in range missing from TLM file")
    return stats, runs


def check_consistency(zone_stats: pd.DataFrame) -> str:
    """Check that Off Taking - Delivering (= TLMO- - TLMO+) is the same in every zone.

    Elexon's TLM file has periods where a zone's Delivering equals its Off Taking (RF runs,
    Jul 2024 - Jan 2025), which breaks this. Those periods are counted and reported.

    Returns:
        A one-line summary, which is also printed.
    """
    spread = zone_stats["Avg Off Taking TLM"] - zone_stats["Avg Delivering TLM"]
    n_bad = int(zone_stats["Periods Delivering = Off Taking"].sum())
    msg = (f"avg(Off Taking - Delivering) = {spread.median():.7f}; range across zones "
           f"{spread.max() - spread.min():.2e}; {n_bad} zone-periods with Delivering = Off Taking")
    if n_bad:
        affected = zone_stats[zone_stats["Periods Delivering = Off Taking"] > 0]
        msg += " (" + ", ".join(f"zone {int(z)}: {int(n)}" for z, n in
                                zip(affected["Zone"],
                                    affected["Periods Delivering = Off Taking"])) + ")"
        print(f"  WARNING: {msg}")
    else:
        print(f"  Settled TLM check OK: {msg}")
    return msg


# --------------------------------------------------------------------------- report


def build_report(reg: pd.DataFrame, season_tlf: pd.DataFrame, zones: pd.DataFrame,
                 zone_stats: pd.DataFrame | None) -> pd.DataFrame:
    """Build the per-BMU "TLMs" sheet.

    ``TLM`` is the BMU's zone 1 + TLF for the season. ``Avg Settled TLM`` uses the zone's
    settled Delivering average for production (P) BMUs and Off Taking for consumption (C) BMUs.
    A blank P/C status falls back to the P/C flag, then to C.
    """
    df = (reg.merge(season_tlf[["Zone", "1 + TLF", "Season Zone TLF", "Confidence"]]
                    .rename(columns={"Confidence": "Season TLF Confidence"}),
                    on="Zone", how="left")
             .merge(zones[["Zone", "GSP Group Name"]].rename(columns={"GSP Group Name": "Zone Name"}),
                    on="Zone", how="left"))
    if zone_stats is not None:
        df = df.merge(zone_stats[["Zone", "Avg Delivering TLM", "Avg Off Taking TLM"]],
                      on="Zone", how="left")
    else:
        df["Avg Delivering TLM"] = float("nan")
        df["Avg Off Taking TLM"] = float("nan")

    status = df["Prod/Cons Status"].fillna(df["Prod/Cons Flag"])
    notes = [list(n) for n in df["_notes"]]
    for n, s, raw in zip(notes, status, df["Prod/Cons Status"]):
        if pd.isna(raw) and pd.notna(s):
            n.append(f"Prod/Cons Status blank; used Flag {s}")
        elif pd.isna(s):
            n.append("Prod/Cons Status and Flag blank; assumed C (Off Taking)")
    status = status.fillna("C")

    has_settled = df["Avg Delivering TLM"].notna()
    df["Settled Basis"] = status.map({"P": "Delivering", "C": "Off Taking"}).where(has_settled)
    df["Avg Settled TLM"] = df["Avg Delivering TLM"].where(status == "P", df["Avg Off Taking TLM"])
    df["TLM"] = df["1 + TLF"]
    df["Notes"] = ["; ".join(n) for n in notes]
    df = df.rename(columns={"TLF": "Registered TLF (current)"})

    columns = ["BM Unit ID", "TLM", "BMU Name", "Party Name", "BMU Type", "GSP Group Id",
               "GSP Group Name", "Zone", "Zone Name", "Zone Source", "Prod/Cons Status",
               "Registered TLF (current)", "Season Zone TLF", "Season TLF Confidence",
               "Avg Settled TLM", "Settled Basis", "Avg Delivering TLM", "Avg Off Taking TLM",
               "Notes"]
    return df[columns].sort_values("BM Unit ID").reset_index(drop=True)


def compare_reference(report: pd.DataFrame, ref_path: Path,
                      tol: float = 5e-7) -> tuple[pd.DataFrame, dict]:
    """Compare the report's TLMs BMU by BMU with a reference workbook (``BM Unit ID``, ``TLM``).

    Args:
        tol: absolute difference still counted as a match. The default allows for API values
            given to 6 d.p.

    Returns:
        (one row per BMU in either file with a Status column; counts per Status)
    """
    ref = pd.read_excel(ref_path)[["BM Unit ID", "TLM"]].rename(columns={"TLM": "Reference TLM"})
    new = (report[["BM Unit ID", "TLM", "BMU Type", "Zone", "Zone Source"]]
           .rename(columns={"TLM": "New TLM"}))
    m = ref.merge(new, on="BM Unit ID", how="outer", indicator=True)
    m["Difference"] = (m["New TLM"] - m["Reference TLM"]).round(TLF_DP)
    status = pd.Series("Different", index=m.index)
    status[m["Difference"].abs() <= tol] = "Match"
    status[(m["_merge"] == "both") & m["New TLM"].isna()] = "No zone in new"
    status[m["_merge"] == "left_only"] = "Only in reference (not currently registered)"
    status[m["_merge"] == "right_only"] = "Only in new (registered since)"
    m["Status"] = status
    m = m.drop(columns="_merge").sort_values(["Status", "BM Unit ID"]).reset_index(drop=True)
    return m, m["Status"].value_counts().to_dict()


def write_excel(path: Path, sheets: dict[str, pd.DataFrame], info: list[tuple[str, object]],
                runs: pd.DataFrame | None = None) -> None:
    """Write a workbook: the given data sheets, then an "Info" sheet.

    Data sheets get a frozen header, an autofilter and sized columns. TLM/TLF columns are
    shown to 7 d.p. If ``runs`` is given, it's appended below the Info items.
    """
    from openpyxl.utils import get_column_letter

    season_prefixes = tuple(k.title() for k in BSC_SEASONS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "B2"
            ws.auto_filter.ref = ws.dimensions
            for idx, col in enumerate(df.columns, start=1):
                letter = get_column_letter(idx)
                width = max(len(str(col)), *(len(str(v)) for v in df[col].head(500))) + 2
                ws.column_dimensions[letter].width = min(width, 60)
                label = str(col)
                if any(k in label for k in ("TLM", "TLF", "Difference")) or label.startswith(season_prefixes):
                    for cell in ws[letter][1:]:
                        cell.number_format = "0.0000000"
        pd.DataFrame(info, columns=["Item", "Value"]).to_excel(xw, sheet_name="Info", index=False)
        ws = xw.sheets["Info"]
        ws.column_dimensions["A"].width = 28
        ws.column_dimensions["B"].width = 110
        if runs is not None:
            runs.to_excel(xw, sheet_name="Info", index=False, startrow=len(info) + 3)
            ws.cell(row=len(info) + 3, column=1, value="Settlement runs in tlm.csv for this range")


# --------------------------------------------------------------------------- orchestration


@dataclass
class Inputs:
    """Everything a report run needs, loaded once and shared by all report periods."""
    reg: pd.DataFrame        # registration frame after assign_zones()
    zones: pd.DataFrame      # zone_tlf_table()
    tlm: pd.DataFrame        # zonal settled TLMs (may be empty)
    etlmo: pd.DataFrame      # ETLMO table
    reg_path: Path
    tlm_path: Path
    sps: list[int]
    cache: Path


def run_period(label: str, segments: Segments, inputs: Inputs, out_path: Path,
               compare_with: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build and write the workbook for one report period (a season or a custom range).

    Returns:
        (the per-BMU report, the zone summary), for use in the combined workbook.

    Raises:
        ValueError: if the period hasn't finished yet, or its ETLMO values are missing.
    """
    print(f"\n{label}: {describe(segments)}")
    if segments[-1][1] >= date.today():
        raise ValueError(f"period not finished until {segments[-1][1]}")
    season_tlf, log = season_zone_tlf(segments, inputs.tlm, inputs.reg, inputs.etlmo,
                                      inputs.sps, inputs.cache)
    for line in log:
        print(f"  {line}")

    # Optional settled averages from tlm.csv.
    tlm = inputs.tlm
    zone_stats = runs = None
    settled_msg = ("Not available: no tlm.csv" if tlm.empty else "Not available: tlm.csv covers "
                   f"{tlm['Settlement Date'].min()} to {tlm['Settlement Date'].max()}")
    try:
        zone_stats, runs = zone_averages(tlm, segments)
        settled_msg = check_consistency(zone_stats)
    except ValueError:
        print(f"  Settled averages: {settled_msg}")

    report = build_report(inputs.reg, season_tlf, inputs.zones, zone_stats)
    zone_summary = inputs.zones.merge(season_tlf, on="Zone", how="left")
    if zone_stats is not None:
        zone_summary = zone_summary.merge(zone_stats, on="Zone", how="left")
        # Settled Off Taking = 1 + TLF + TLMO-, so this difference should be equal across zones.
        implied = zone_summary["Avg Off Taking TLM"] - zone_summary["1 + TLF"]
        print(f"  Cross-check: avg Off Taking - (1 + TLF) per zone ranges "
              f"{implied.min():.7f}..{implied.max():.7f}")

    sheets = {"TLMs": report, "Zone Summary": zone_summary}
    unmapped = report.loc[report["Zone"].isna(), "BM Unit ID"].tolist()
    reg_date = datetime.fromtimestamp(inputs.reg_path.stat().st_mtime)
    tlm_source = f"{inputs.tlm_path.name}; " if not tlm.empty else ""
    info: list[tuple[str, object]] = [
        ("Report", f"{label} TLMs"),
        ("Settlement dates", f"{describe(segments)} (inclusive)"),
        ("Seasons", "Elexon BSC seasons: Spring = Apr-May + following Mar, Summer = Jun-Aug, "
                    "Autumn = Sep-Nov, Winter = Dec-Feb. TLFs are set per season."),
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("TLM column", "1 + TLF for the BMU's zone in this season (same basis as the original "
                       "'Winter 2022 TLMs.xlsx'). Excludes the per-period loss adjustment TLMO±."),
        ("Avg Settled TLM column", "Average settled TLM over the range from tlm.csv: Delivering "
                                   "for production (P) BMUs, Off Taking for consumption (C) BMUs. "
                                   "Includes TLMO±."),
        ("Season zone TLF source", "Elexon Insights API settlement stack TLMs (= 1 + TLF + ETLMO±) "
                                   "minus published ETLMO; where tlm.csv covers the range its "
                                   "exact zone differences are used and the API sets the level."),
        ("Season zone TLF bands", " | ".join(log)),
        ("Settled TLM check", settled_msg),
        ("Zone assignment", "Registered TLF matched to current zone TLFs (zones 1-14 = GSP Groups "
                            "_A.._P); GSP Group used if TLF does not match. Uses current "
                            "registrations."),
        ("BMUs", len(report)),
        ("BMUs without a zone", ", ".join(unmapped) or "none"),
        ("Sources", f"{inputs.reg_path.name} (modified {reg_date:%Y-%m-%d}); {tlm_source}"
                    f"{api.ETLMO_URL}; data.elexon.co.uk ISPSTACK"),
    ]

    if compare_with is not None:
        cmp_df, summary = compare_reference(report, compare_with)
        sheets["Reference Check"] = cmp_df
        info.append(("Reference check", f"{compare_with.name}: {summary}"))
        print(f"  Reference check vs {compare_with.name}: {summary}")
        diffs = cmp_df[cmp_df["Status"] == "Different"]
        if not diffs.empty:
            print(diffs.groupby("Zone")["Difference"].agg(["count", "min", "max"]).to_string())

    write_excel(out_path, sheets, info, runs)
    print(f"  {len(report)} BMUs; unmapped: {unmapped or 'none'}")
    print(f"  Wrote {out_path}")
    return report, zone_summary


def write_combined_workbook(results: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
                            periods: list[tuple[str, Segments]], out_path: Path) -> None:
    """Write one workbook with every period side by side (BMUs and zones by season)."""
    wide = None
    zone_rows = []
    for label, (report, zone_summary) in results.items():
        col = report[["BM Unit ID", "TLM"]].rename(columns={"TLM": label})
        wide = col if wide is None else wide.merge(col, on="BM Unit ID", how="outer")
        z = zone_summary[["Zone", "GSP Group Id", "GSP Group Name", "1 + TLF"]].copy()
        z["Season"] = label
        for c in ("Avg Delivering TLM", "Avg Off Taking TLM"):
            z[c] = zone_summary[c] if c in zone_summary else float("nan")
        zone_rows.append(z)

    first_report = next(iter(results.values()))[0]
    wide = first_report[["BM Unit ID", "BMU Type", "Zone", "Zone Name"]].merge(wide, on="BM Unit ID")
    zone_long = pd.concat(zone_rows)
    zone_wide = zone_long.pivot_table(index=["Zone", "GSP Group Id", "GSP Group Name"],
                                      columns="Season", values="1 + TLF", sort=False).reset_index()
    zone_wide.columns.name = None

    info = [
        ("Contents", "TLM = 1 + TLF for each BSC season (same basis as the original Winter 2022 "
                     "report); one column per season. See the per-season workbooks for details."),
        ("Seasons", "; ".join(f"{label} ({describe(segs)})" for label, segs in periods
                              if label in results)),
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    write_excel(out_path, {"BMU TLMs by season": wide, "Zone TLMs by season": zone_wide,
                           "Zone detail": zone_long}, info)
    print(f"\nWrote {out_path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments (see the module docstring for examples)."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--season", action="append",
                   help='BSC season, e.g. "Winter 2022" (Dec 2022-Feb 2023). Repeatable.')
    p.add_argument("--bsc-year", type=int, action="append",
                   help="all four seasons of BSC Year YYYY/YY+1. Repeatable.")
    p.add_argument("--start", type=date.fromisoformat, help="custom range start (YYYY-MM-DD)")
    p.add_argument("--end", type=date.fromisoformat, help="custom range end (YYYY-MM-DD)")
    p.add_argument("--label", help="name for a custom range (used in the output file name)")
    p.add_argument("--compare", type=Path,
                   help="reference workbook (BM Unit ID, TLM) to check against; applied to the "
                        "season named in its file name, or to every period if none is named")
    p.add_argument("--reg", type=Path, default=DEFAULT_REG,
                   help="Registered BM Units CSV (default: data/reg_bm_units.csv)")
    p.add_argument("--tlm", type=Path, default=DEFAULT_TLM,
                   help="zonal settled TLM CSV, optional (default: data/tlm.csv)")
    p.add_argument("--out", type=Path, help="output .xlsx path (only when building one period)")
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                   help="download cache folder (default: cache/)")
    p.add_argument("--api-sps", type=lambda s: [int(x) for x in s.split(",")], default=DEFAULT_SPS,
                   help="settlement periods sampled per day from the API (default: "
                        + ",".join(map(str, DEFAULT_SPS)) + ")")
    p.add_argument("--refresh-etlmo", action="store_true",
                   help="re-download the ETLMO table (otherwise refreshed after 30 days)")
    return p.parse_args(argv)


def resolve_periods(args: argparse.Namespace, tlm: pd.DataFrame) -> list[tuple[str, Segments]]:
    """Turn the period options into a date-ordered list of (label, segments).

    Raises:
        ValueError: for an invalid combination of options or a bad season name.
    """
    if args.start or args.end:
        if not (args.start and args.end) or args.season or args.bsc_year:
            raise ValueError("use --start and --end together, without --season/--bsc-year")
        if args.start > args.end:
            raise ValueError("--start must be on or before --end")
        return [(args.label or f"{args.start} to {args.end}", [(args.start, args.end)])]

    seasons = [s for y in (args.bsc_year or []) for s in bsc_year_seasons(y)] + (args.season or [])
    if not seasons:
        as_of = date.today() if tlm.empty else tlm["Settlement Date"].max()
        seasons = [latest_complete_winter(as_of)]
    periods = [(s.title(), season_segments(s)) for s in seasons]
    return sorted(periods, key=lambda p: p[1][0][0])


def should_compare(label: str, compare: Path, labels: list[str]) -> bool:
    """Apply --compare to the period named in the reference file name (e.g. "Winter 2022
    TLMs.xlsx"), or to every period if the file name doesn't name any of them."""
    name = compare.name.lower()
    return label.lower() in name or not any(other.lower() in name for other in labels)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Returns 0 on success and 2 if nothing could be built."""
    args = parse_args(argv)
    tlm = load_tlm(args.tlm)
    reg = load_registration(args.reg)
    try:
        zones = zone_tlf_table(reg)
        etlmo = api.load_etlmo(args.cache, refresh=args.refresh_etlmo)
        periods = resolve_periods(args, tlm)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    inputs = Inputs(reg=assign_zones(reg, zones), zones=zones, tlm=tlm, etlmo=etlmo,
                    reg_path=args.reg, tlm_path=args.tlm, sps=args.api_sps, cache=args.cache)
    labels = [label for label, _ in periods]
    results: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for label, segments in periods:
        out_path = args.out if args.out and len(periods) == 1 else DEFAULT_OUT_DIR / f"{label} TLMs.xlsx"
        compare_with = (args.compare if args.compare and should_compare(label, args.compare, labels)
                        else None)
        try:
            results[label] = run_period(label, segments, inputs, out_path, compare_with)
        except ValueError as e:
            print(f"ERROR ({label}): {e} - skipped", file=sys.stderr)
    if not results:
        return 2

    if len(results) > 1:
        write_combined_workbook(results, periods, DEFAULT_OUT_DIR / "TLMs by season.xlsx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
