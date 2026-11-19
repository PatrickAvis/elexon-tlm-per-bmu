"""Build per-BMU Transmission Loss Multiplier (TLM) reports by BSC season.

Inputs
  * data/reg_bm_units.csv   Registered BM Units (Elexon Portal)
  * data/tlm.csv            Zonal settled TLMs per settlement period (Elexon Portal)
  * Elexon ETLMO table and Insights API settlement stacks (public, fetched and cached)

Method
  BSC Section T: TLM = 1 + TLF(zone, season) + TLMO±(period). The headline "TLM" column is
  1 + TLF(zone, season), the same definition as the "Winter 2022 TLMs.xlsx" reference.
  Settled season averages (including TLMO±) from tlm.csv are shown alongside where available.

  Seasonal zone TLFs are recovered from the Insights API stack TLMs (= 1 + TLF + ETLMO±)
  minus the published ETLMO values. Where tlm.csv covers the season, its exact zone-to-zone
  TLF differences are used and the API only sets the common level.

  BMUs are put in zones 1-14 (= GSP Groups _A.._P) by matching their registered TLF to the
  current zone TLFs, falling back to GSP Group.

Seasons (Elexon BSC seasons; TLFs are set per season)
  Season YYYY belongs to BSC Year YYYY/YY+1 (1 Apr YYYY - 31 Mar YYYY+1):
    Spring YYYY = Apr-May YYYY + Mar YYYY+1   (Elexon "Spring A" + "Spring B")
    Summer YYYY = Jun-Aug YYYY
    Autumn YYYY = Sep-Nov YYYY
    Winter YYYY = Dec YYYY - Feb YYYY+1        (e.g. "Winter 2022 TLMs.xlsx")

Usage
  python tlm_report.py                                   # latest complete BSC winter
  python tlm_report.py --season "Winter 2022" --compare "data/Winter 2022 TLMs.xlsx"
  python tlm_report.py --bsc-year 2022 --bsc-year 2023   # all four seasons of each BSC year
  python tlm_report.py --start 2025-10-01 --end 2026-03-31 --label "Oct25-Mar26"
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import elexon_api as api

ROOT = Path(__file__).resolve().parent
DEFAULT_TLM = ROOT / "data" / "tlm.csv"
DEFAULT_REG = ROOT / "data" / "reg_bm_units.csv"
DEFAULT_OUT_DIR = ROOT / "output"
DEFAULT_CACHE = ROOT / "cache"
DEFAULT_SPS = [2, 10, 18, 26, 34, 42]  # every 4 hours

# GSP Group Id -> (TLM zone number, GSP Group Name)
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

# Elexon BSC seasons. Season YYYY belongs to BSC Year YYYY/YY+1 and may have several parts:
# (year offset, start month, number of months). Spring is split by the BSC Year boundary.
BSC_SEASONS: dict[str, list[tuple[int, int, int]]] = {
    "spring": [(0, 4, 2), (1, 3, 1)],   # Apr-May YYYY + Mar YYYY+1
    "summer": [(0, 6, 3)],              # Jun-Aug YYYY
    "autumn": [(0, 9, 3)],              # Sep-Nov YYYY
    "winter": [(0, 12, 3)],             # Dec YYYY - Feb YYYY+1
}
Segments = list[tuple[date, date]]

TLF_DP = 7
CONF_ORDER = ["None", "Low", "Medium (decided by P/C status)", "Medium", "High"]
VOTE_DP = 6


# --------------------------------------------------------------------------- seasons


def season_segments(name: str) -> Segments:
    """'Winter 2022' -> [(2022-12-01, 2023-02-28)]; 'Spring 2025' -> Apr-May 2025 + Mar 2026."""
    try:
        kind, year = name.split()
        parts = BSC_SEASONS[kind.lower()]
        year = int(year)
    except (ValueError, KeyError):
        raise ValueError(f"Bad season {name!r}; use e.g. 'Winter 2022' (Spring/Summer/Autumn/Winter)")
    segs = []
    for offset, month, months in parts:
        start = date(year + offset, month, 1)
        end = (pd.Timestamp(start) + pd.DateOffset(months=months) - pd.Timedelta(days=1)).date()
        segs.append((start, end))
    return segs


def bsc_year_seasons(year: int) -> list[str]:
    return [f"{k.title()} {year}" for k in BSC_SEASONS]


def describe(segs: Segments) -> str:
    return " + ".join(f"{a} to {b}" for a, b in segs)


def latest_complete_winter(max_date: date) -> str:
    y = max_date.year if max_date.month >= 3 else max_date.year - 1  # winter ends end-Feb of y
    return f"Winter {y - 1}"


# --------------------------------------------------------------------------- loading


def load_registration(path: Path) -> pd.DataFrame:
    """Read the Elexon registered BM Units CSV (2 title lines precede the header)."""
    reg = pd.read_csv(path, skiprows=2, dtype={"BM Unit ID": str})
    reg.columns = [c.strip() for c in reg.columns]
    reg = reg.dropna(subset=["BM Unit ID"])
    reg["BM Unit ID"] = reg["BM Unit ID"].str.strip()
    for col in ("Prod/Cons Status", "Prod/Cons Flag", "GSP Group Id", "BMU Type"):
        reg[col] = reg[col].astype("string").str.strip().replace("", pd.NA)
    reg["TLF"] = pd.to_numeric(reg["TLF"], errors="coerce")
    return reg


TLM_COLUMNS = ["Settlement Date", "Settlement Run Type", "Settlement Period", "Zone",
               "Off Taking", "Delivering"]


def load_tlm(path: Path) -> pd.DataFrame:
    """Zonal settled TLMs. Optional: without the file, zone TLFs come from the API alone
    and the settled-average columns are left blank."""
    if not path.exists():
        print(f"Note: {path} not found - API-only zone TLFs, no settled averages.")
        return pd.DataFrame(columns=TLM_COLUMNS)
    tlm = pd.read_csv(path)
    tlm.columns = [c.strip() for c in tlm.columns]
    tlm["Settlement Date"] = pd.to_datetime(tlm["Settlement Date"], dayfirst=True).dt.date
    return tlm


# --------------------------------------------------------------------------- zones


def zone_tlf_table(reg: pd.DataFrame) -> pd.DataFrame:
    """Current zone TLF = modal TLF of supplier (S) BMUs in each GSP Group."""
    supp = reg[(reg["BMU Type"] == "S") & reg["GSP Group Id"].isin(GSP_ZONE.keys())]
    tlf = supp.groupby("GSP Group Id")["TLF"].agg(lambda s: s.mode().iloc[0])
    table = pd.DataFrame([
        {"Zone": z, "GSP Group Id": g, "GSP Group Name": name, "Current Zone TLF": tlf.get(g)}
        for g, (z, name) in GSP_ZONE.items()
    ])
    if table["Current Zone TLF"].isna().any():
        missing = list(table.loc[table["Current Zone TLF"].isna(), "GSP Group Id"])
        raise ValueError(f"No supplier BMUs to derive zone TLF for {missing}")
    if table["Current Zone TLF"].round(TLF_DP).duplicated().any():
        raise ValueError("Zone TLFs are not unique; TLF cannot identify zone")
    return table


def assign_zones(reg: pd.DataFrame, zones: pd.DataFrame) -> pd.DataFrame:
    """Add Zone, Zone Source and _notes columns to the registration frame."""
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
            n.append(f"TLF matches zone {int(t)} ({ZONE_GSP[int(t)]}) but GSP Group is zone {int(g)}; used TLF")
        if pd.isna(t) and pd.notna(g):
            n.append("TLF does not match a zone TLF; zone from GSP Group")
        if pd.isna(t) and pd.isna(g):
            n.append("No zone: TLF does not match a zone and no GSP Group")
        notes.append(n)
    out["_notes"] = notes
    return out


# --------------------------------------------------------------------------- seasonal TLF


def _candidates(obs: pd.DataFrame, e_plus: float, e_minus: float) -> pd.DataFrame:
    """Each stack TLM gives two candidates for 1 + TLF: minus ETLMO+ (P) or ETLMO- (C)."""
    p = obs.assign(Value=obs["Stack TLM"] - e_plus, Tag="P")
    c = obs.assign(Value=obs["Stack TLM"] - e_minus, Tag="C")
    return pd.concat([p, c], ignore_index=True)


def _pick(cands: pd.DataFrame) -> tuple[float, int, str]:
    """Vote on Key by distinct BMUs; tie-break by BMUs whose P/C status matches the tag.

    Returns (key, support, confidence).
    """
    cands = cands.assign(StatusMatch=cands["Status"] == cands["Tag"])
    votes = cands.groupby("Key").agg(
        Support=("BM Unit ID", "nunique"),
        StatusSupport=("BM Unit ID", lambda s: s[cands.loc[s.index, "StatusMatch"]].nunique()),
    ).sort_values(["Support", "StatusSupport"], ascending=False)
    top = votes.iloc[0]
    second = votes.iloc[1] if len(votes) > 1 else None
    if second is None or top.Support > second.Support:
        conf = "High" if top.Support >= 2 else "Medium"
    elif top.StatusSupport > second.StatusSupport:
        conf = "Medium (decided by P/C status)"
    else:
        conf = "Low"
    return votes.index[0], int(top.Support), conf


def band_zone_tlf(obs: pd.DataFrame, e_plus: float, e_minus: float, bmu_zone: dict,
                  bmu_status: dict, rel: pd.Series | None = None) -> pd.DataFrame:
    """1 + TLF per zone for one ETLMO band.

    obs: distinct (BM Unit ID, Stack TLM). rel: optional exact TLF(z) - TLF(1) from tlm.csv.
    """
    obs = obs[obs["BM Unit ID"].isin(bmu_zone.keys())].copy()
    obs["Zone"] = obs["BM Unit ID"].map(bmu_zone).astype(int)
    obs["Status"] = obs["BM Unit ID"].map(bmu_status)
    cands = _candidates(obs, e_plus, e_minus)
    zones = range(1, 15)

    if rel is not None and not cands.empty:
        # common level: 1 + TLF(zone 1), voted across all zones at once
        cands["Key"] = (cands["Value"] - cands["Zone"].map(rel)).round(VOTE_DP)
        key, support, conf = _pick(cands)
        level = (cands.loc[cands["Key"] == key, "Value"] - cands.loc[cands["Key"] == key, "Zone"].map(rel)).median()
        per_zone = cands[cands["Key"] == key].groupby("Zone")["BM Unit ID"].nunique()
        return pd.DataFrame({
            "Zone": list(zones),
            "1 + TLF": [round(level + rel[z], TLF_DP) for z in zones],
            "Method": "tlm.csv zone differences + API level",
            "API BMUs": [int(per_zone.get(z, 0)) for z in zones],
            "Confidence": conf if support >= 3 else "Low",
        })

    rows = []
    for z in zones:
        zc = cands[cands["Zone"] == z].copy()
        if zc.empty:
            rows.append({"Zone": z, "1 + TLF": float("nan"), "Method": "API", "API BMUs": 0,
                         "Confidence": "None"})
            continue
        zc["Key"] = zc["Value"].round(VOTE_DP)
        key, support, conf = _pick(zc)
        value = zc.loc[zc["Key"] == key, "Value"].median()
        rows.append({"Zone": z, "1 + TLF": round(value, TLF_DP), "Method": "API",
                     "API BMUs": support, "Confidence": conf})
    return pd.DataFrame(rows)


def tlm_relative(tlm: pd.DataFrame, start: date, end: date) -> pd.Series | None:
    """Exact TLF(z) - TLF(1) from tlm.csv Off Taking, if it fully covers [start, end]."""
    sel = tlm[(tlm["Settlement Date"] >= start) & (tlm["Settlement Date"] <= end)]
    if sel["Settlement Date"].nunique() != (end - start).days + 1:
        return None
    piv = sel.pivot_table(index=["Settlement Date", "Settlement Period"], columns="Zone",
                          values="Off Taking")
    rel = piv.sub(piv[1], axis=0)
    if float((rel.max() - rel.min()).max()) > 1e-6:  # TLFs changed inside band
        return None
    return rel.mean().round(TLF_DP)


def season_zone_tlf(segs: Segments, tlm: pd.DataFrame, reg: pd.DataFrame,
                    etlmo: pd.DataFrame, sps: list[int], cache: Path) -> tuple[pd.DataFrame, list[str]]:
    """Season (days-weighted over ETLMO bands) 1 + TLF per zone, plus log lines."""
    bmu_zone = {b: int(z) for b, z in zip(reg["BM Unit ID"], reg["Zone"]) if pd.notna(z)}
    bmu_status = dict(zip(reg["BM Unit ID"], reg["Prod/Cons Status"].fillna(reg["Prod/Cons Flag"])))
    parts, log = [], []
    for band in [b for start, end in segs for b in api.etlmo_bands(etlmo, start, end)]:
        obs = api.stack_tlms(band["From"], band["To"], sps, cache)
        rel = tlm_relative(tlm, band["From"], band["To"])
        res = band_zone_tlf(obs, band["ETLMO+"], band["ETLMO-"], bmu_zone, bmu_status, rel)
        res["Days"] = (band["To"] - band["From"]).days + 1
        parts.append(res)
        weak = res.loc[res["Confidence"].isin(["Low", "None"]), "Zone"].tolist()
        log.append(f"{band['From']}..{band['To']}: ETLMO+ {band['ETLMO+']}, ETLMO- {band['ETLMO-']}; "
                   f"{len(obs)} API (BMU, TLM) pairs; method: {res['Method'].iloc[0]}; "
                   f"low-confidence zones: {weak or 'none'}")
    allp = pd.concat(parts)
    # TLFs should be constant within a BSC season; flag zones where the parts disagree
    # (e.g. Spring 2025: Elexon's mid-April 2025 TLF correction).
    spread = allp.groupby("Zone")["1 + TLF"].agg(lambda s: s.max() - s.min() if s.notna().any() else 0.0)
    changed = spread[spread > 1e-6]
    if not changed.empty:
        log.append("WARNING: zone TLFs change within this season, so the value is a day-weighted blend "
                   f"(max change {changed.max():.5f}; zones {list(changed.index)})")
    # Bands with no usable API data for a zone (e.g. stacks holding only 1.0 placeholders)
    # are left out of that zone's day-weighted average rather than diluting it.
    has = allp["1 + TLF"].notna()
    allp["w"] = allp["1 + TLF"].where(has, 0) * allp["Days"]
    allp["DaysWithData"] = allp["Days"].where(has, 0)
    out = allp.groupby("Zone").agg(w=("w", "sum"), Days=("Days", "sum"),
                                   **{"Days With Data": ("DaysWithData", "sum"),
                                      "API BMUs": ("API BMUs", "sum")},
                                   Method=("Method", "first"),
                                   Confidence=("Confidence",
                                               lambda s: min((c for c in s if c != "None"),
                                                             key=CONF_ORDER.index, default="None")))
    out["1 + TLF"] = (out["w"] / out["Days With Data"].where(out["Days With Data"] > 0)).round(TLF_DP)
    out["Season Zone TLF"] = (out["1 + TLF"] - 1).round(TLF_DP)
    partial = (out["Days With Data"] > 0) & (out["Days With Data"] < out["Days"])
    out.loc[partial, "Confidence"] = out.loc[partial].apply(
        lambda r: f"{r['Confidence']}; data for {int(r['Days With Data'])} of {int(r['Days'])} days", axis=1)
    if partial.any():
        log.append("Zones averaged over part of the range (no API data in some bands): "
                   + ", ".join(f"zone {z} ({int(d)}/{int(t)} days)" for z, d, t in
                               zip(out.index[partial], out.loc[partial, "Days With Data"],
                                   out.loc[partial, "Days"])))
    out.loc[out.index.isin(changed.index), "Confidence"] += "; TLF changed within season (blend)"
    return out.drop(columns=["w"]).reset_index(), log


# --------------------------------------------------------------------------- settled TLMs


def zone_averages(tlm: pd.DataFrame, segs: Segments) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Average settled zonal TLMs over the date segments. Returns (zone stats, run-type coverage)."""
    if tlm.empty:
        raise ValueError("no tlm.csv data")
    lo, hi = tlm["Settlement Date"].min(), tlm["Settlement Date"].max()
    for start, end in segs:
        if start > end:
            raise ValueError("start must be on or before end")
        if start < lo or end > hi:
            raise ValueError(f"Requested {start}..{end} is outside TLM file coverage {lo}..{hi}")

    mask = pd.Series(False, index=tlm.index)
    for start, end in segs:
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

    missing = sum((b - a).days + 1 for a, b in segs) - sel["Settlement Date"].nunique()
    if missing:
        print(f"WARNING: {missing} day(s) in range missing from TLM file")
    return stats, runs


def check_consistency(zone_stats: pd.DataFrame) -> str:
    """Off Taking - Delivering (= TLMO- - TLMO+) should be the same for every zone.

    The published file has periods where a zone's Delivering equals its Off Taking
    (seen in RF runs, Jul 2024 - Jan 2025), which breaks this. Those are reported.
    """
    spread = zone_stats["Avg Off Taking TLM"] - zone_stats["Avg Delivering TLM"]
    rng = spread.max() - spread.min()
    n_bad = int(zone_stats["Periods Delivering = Off Taking"].sum())
    msg = (f"avg(Off Taking - Delivering) = {spread.median():.7f}; range across zones {rng:.2e}; "
           f"{n_bad} zone-periods with Delivering = Off Taking")
    if n_bad:
        affected = zone_stats[zone_stats["Periods Delivering = Off Taking"] > 0]
        msg += " (" + ", ".join(f"zone {int(z)}: {int(n)}" for z, n in
                                zip(affected["Zone"], affected["Periods Delivering = Off Taking"])) + ")"
        print(f"  WARNING: {msg}")
    else:
        print(f"  Settled TLM check OK: {msg}")
    return msg


# --------------------------------------------------------------------------- report


def build_report(reg: pd.DataFrame, season_tlf: pd.DataFrame, zones: pd.DataFrame,
                 zone_stats: pd.DataFrame | None) -> pd.DataFrame:
    df = (reg.merge(season_tlf[["Zone", "1 + TLF", "Season Zone TLF", "Confidence"]]
                    .rename(columns={"Confidence": "Season TLF Confidence"}), on="Zone", how="left")
             .merge(zones[["Zone", "GSP Group Name"]].rename(columns={"GSP Group Name": "Zone Name"}),
                    on="Zone", how="left"))
    if zone_stats is not None:
        df = df.merge(zone_stats[["Zone", "Avg Delivering TLM", "Avg Off Taking TLM"]], on="Zone", how="left")
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

    cols = ["BM Unit ID", "TLM", "BMU Name", "Party Name", "BMU Type", "GSP Group Id",
            "GSP Group Name", "Zone", "Zone Name", "Zone Source", "Prod/Cons Status",
            "Registered TLF (current)", "Season Zone TLF", "Season TLF Confidence", "Avg Settled TLM", "Settled Basis",
            "Avg Delivering TLM", "Avg Off Taking TLM", "Notes"]
    return df[cols].sort_values("BM Unit ID").reset_index(drop=True)


def compare_reference(report: pd.DataFrame, ref_path: Path, tol: float = 5e-7) -> tuple[pd.DataFrame, dict]:
    ref = pd.read_excel(ref_path)[["BM Unit ID", "TLM"]].rename(columns={"TLM": "Reference TLM"})
    new = report[["BM Unit ID", "TLM", "BMU Type", "Zone", "Zone Source"]].rename(columns={"TLM": "New TLM"})
    m = ref.merge(new, on="BM Unit ID", how="outer", indicator=True)
    m["Difference"] = (m["New TLM"] - m["Reference TLM"]).round(TLF_DP)
    status = pd.Series("Different", index=m.index)
    status[m["Difference"].abs() <= tol] = "Match"
    status[(m["_merge"] == "both") & m["New TLM"].isna()] = "No zone in new"
    status[m["_merge"] == "left_only"] = "Only in reference (not currently registered)"
    status[m["_merge"] == "right_only"] = "Only in new (registered since)"
    m["Status"] = status
    m = m.drop(columns="_merge").sort_values(["Status", "BM Unit ID"]).reset_index(drop=True)
    summary = m["Status"].value_counts().to_dict()
    return m, summary


def write_excel(path: Path, sheets: dict[str, pd.DataFrame], info: list[tuple[str, object]],
                runs: pd.DataFrame | None) -> None:
    from openpyxl.utils import get_column_letter

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
                if any(k in str(col) for k in ("TLM", "TLF", "Difference")) or str(col).startswith(("Winter", "Spring", "Summer", "Autumn")):
                    for cell in ws[letter][1:]:
                        cell.number_format = "0.0000000"
        pd.DataFrame(info, columns=["Item", "Value"]).to_excel(xw, sheet_name="Info", index=False)
        ws = xw.sheets["Info"]
        ws.column_dimensions["A"].width = 28
        ws.column_dimensions["B"].width = 110
        if runs is not None:
            runs.to_excel(xw, sheet_name="Info", index=False, startrow=len(info) + 3)
            ws.cell(row=len(info) + 3, column=1, value="Settlement runs in tlm.csv for this range")


def compares(label: str, args) -> bool:
    """Compare only the season named in the reference file (e.g. 'Winter 2022 TLMs.xlsx'),
    or every range if the file name names none of them."""
    name = args.compare.name.lower()
    labels = [r[0].lower() for r in args.ranges]
    return label.lower() in name or not any(l in name for l in labels)


def run_range(label: str, segs: Segments, tlm: pd.DataFrame, reg: pd.DataFrame,
              zones: pd.DataFrame, etlmo: pd.DataFrame, args) -> tuple[pd.DataFrame, pd.DataFrame]:
    print(f"\n{label}: {describe(segs)}")
    if segs[-1][1] >= date.today():
        raise ValueError(f"season not finished until {segs[-1][1]}")
    season_tlf, log = season_zone_tlf(segs, tlm, reg, etlmo, args.api_sps, args.cache)
    for line in log:
        print(f"  {line}")

    zone_stats = runs = None
    settled_msg = ("Not available: no tlm.csv" if tlm.empty else "Not available: tlm.csv covers "
                   f"{tlm['Settlement Date'].min()} to {tlm['Settlement Date'].max()}")
    try:
        zone_stats, runs = zone_averages(tlm, segs)
        settled_msg = check_consistency(zone_stats)
    except ValueError:
        print(f"  Settled averages: {settled_msg}")

    report = build_report(reg, season_tlf, zones, zone_stats)
    zone_summary = zones.merge(season_tlf, on="Zone", how="left")
    if zone_stats is not None:
        zone_summary = zone_summary.merge(zone_stats, on="Zone", how="left")
        # settled average Off Taking should equal 1 + TLF + avg TLMO- for every zone
        implied = (zone_summary["Avg Off Taking TLM"] - zone_summary["1 + TLF"])
        print(f"  Cross-check: avg Off Taking - (1 + TLF) per zone ranges {implied.min():.7f}..{implied.max():.7f}")

    sheets = {"TLMs": report, "Zone Summary": zone_summary}
    unmapped = report.loc[report["Zone"].isna(), "BM Unit ID"].tolist()
    info = [
        ("Report", f"{label} TLMs"),
        ("Settlement dates", f"{describe(segs)} (inclusive)"),
        ("Seasons", "Elexon BSC seasons: Spring = Apr-May + following Mar, Summer = Jun-Aug, "
                    "Autumn = Sep-Nov, Winter = Dec-Feb. TLFs are set per season."),
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("TLM column", "1 + TLF for the BMU's zone in this season (same basis as 'Winter 2022 TLMs.xlsx'). "
                       "Excludes the per-period loss adjustment TLMO±."),
        ("Avg Settled TLM column", "Average settled TLM over the range from tlm.csv: Delivering for "
                                   "production (P) BMUs, Off Taking for consumption (C) BMUs. Includes TLMO±."),
        ("Season zone TLF source", "Elexon Insights API settlement stack TLMs (= 1 + TLF + ETLMO±) minus "
                                   "published ETLMO; where tlm.csv covers the range its exact zone "
                                   "differences are used and the API sets the level."),
        ("Season zone TLF bands", " | ".join(log)),
        ("Settled TLM check", settled_msg),
        ("Zone assignment", "Registered TLF matched to current zone TLFs (zones 1-14 = GSP Groups _A.._P); "
                            "GSP Group used if TLF does not match. Uses current registrations."),
        ("BMUs", len(report)),
        ("BMUs without a zone", ", ".join(unmapped) or "none"),
        ("Sources", f"{args.reg.name} (modified {datetime.fromtimestamp(args.reg.stat().st_mtime):%Y-%m-%d}); "
                    f"{args.tlm.name}; {api.ETLMO_URL}; data.elexon.co.uk ISPSTACK"),
    ]

    if args.compare and compares(label, args):
        cmp_df, summary = compare_reference(report, args.compare)
        sheets["Reference Check"] = cmp_df
        info.append(("Reference check", f"{args.compare.name}: {summary}"))
        print(f"  Reference check vs {args.compare.name}: {summary}")
        diffs = cmp_df[cmp_df["Status"] == "Different"]
        if not diffs.empty:
            print(diffs.groupby("Zone")["Difference"].agg(["count", "min", "max"]).to_string())

    out = args.out if args.out and len(args.ranges) == 1 else DEFAULT_OUT_DIR / f"{label} TLMs.xlsx"
    write_excel(out, sheets, info, runs)
    print(f"  {len(report)} BMUs; unmapped: {unmapped or 'none'}")
    print(f"  Wrote {out}")
    return report, zone_summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--season", action="append",
                   help='BSC season, e.g. "Winter 2022" (Dec 2022-Feb 2023). Repeatable.')
    p.add_argument("--bsc-year", type=int, action="append",
                   help="all four seasons of BSC Year YYYY/YY+1 (Spring, Summer, Autumn, Winter). Repeatable.")
    p.add_argument("--start", type=date.fromisoformat, help="custom range start (YYYY-MM-DD)")
    p.add_argument("--end", type=date.fromisoformat, help="custom range end (YYYY-MM-DD)")
    p.add_argument("--label", help="label for a custom range")
    p.add_argument("--compare", type=Path, help="reference workbook (BM Unit ID, TLM) to check against")
    p.add_argument("--tlm", type=Path, default=DEFAULT_TLM)
    p.add_argument("--reg", type=Path, default=DEFAULT_REG)
    p.add_argument("--out", type=Path, help="output .xlsx (single report only)")
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="download cache folder")
    p.add_argument("--api-sps", type=lambda s: [int(x) for x in s.split(",")], default=DEFAULT_SPS,
                   help="settlement periods sampled per day from the API (default 2,10,18,26,34,42)")
    p.add_argument("--refresh-etlmo", action="store_true", help="re-download the ETLMO table")
    args = p.parse_args(argv)

    tlm = load_tlm(args.tlm)
    reg = load_registration(args.reg)
    try:
        zones = zone_tlf_table(reg)
        etlmo = api.load_etlmo(args.cache, refresh=args.refresh_etlmo)
        if args.start or args.end:
            if not (args.start and args.end) or args.season or args.bsc_year:
                raise ValueError("use --start and --end together, without --season/--bsc-year")
            if args.start > args.end:
                raise ValueError("--start must be on or before --end")
            ranges = [(args.label or f"{args.start} to {args.end}", [(args.start, args.end)])]
        else:
            seasons = [s for y in (args.bsc_year or []) for s in bsc_year_seasons(y)] + (args.season or [])
            seasons = seasons or [latest_complete_winter(
                date.today() if tlm.empty else tlm["Settlement Date"].max())]
            ranges = [(s.title(), season_segments(s)) for s in seasons]
            ranges.sort(key=lambda r: r[1][0][0])
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    reg = assign_zones(reg, zones)
    args.ranges = ranges
    results = {}
    for label, segs in ranges:
        try:
            results[label] = run_range(label, segs, tlm, reg, zones, etlmo, args)
        except ValueError as e:
            print(f"ERROR ({label}): {e} - skipped", file=sys.stderr)
    if not results:
        return 2

    if len(results) > 1:
        wide = None
        zone_rows = []
        for label, (report, zs) in results.items():
            col = report[["BM Unit ID", "TLM"]].rename(columns={"TLM": label})
            wide = col if wide is None else wide.merge(col, on="BM Unit ID", how="outer")
            z = zs[["Zone", "GSP Group Id", "GSP Group Name", "1 + TLF"]].copy()
            z["Season"] = label
            for c in ("Avg Delivering TLM", "Avg Off Taking TLM"):
                z[c] = zs[c] if c in zs else float("nan")
            zone_rows.append(z)
        lead = results[next(iter(results))][0][["BM Unit ID", "BMU Type", "Zone", "Zone Name"]]
        wide = lead.merge(wide, on="BM Unit ID")
        zone_long = pd.concat(zone_rows)
        zone_wide = zone_long.pivot_table(index=["Zone", "GSP Group Id", "GSP Group Name"],
                                          columns="Season", values="1 + TLF", sort=False).reset_index()
        zone_wide.columns.name = None
        out = DEFAULT_OUT_DIR / "TLMs by season.xlsx"
        write_excel(out, {"BMU TLMs by season": wide, "Zone TLMs by season": zone_wide,
                          "Zone detail": zone_long},
                    [("Contents", "TLM = 1 + TLF for each BSC season (same basis as the Winter 2022 file); "
                                  "one column per season. See the per-season workbooks for details."),
                     ("Seasons", "; ".join(f"{l} ({describe(segs)})" for l, segs in ranges
                                           if l in results)),
                     ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M"))], None)
        print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
