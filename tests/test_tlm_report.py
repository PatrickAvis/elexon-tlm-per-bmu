"""Offline unit tests for tlm_report.py and elexon_api.py.

They use small synthetic data only: no network access and no input files are needed.
Run with ``python -m pytest``.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import elexon_api as api  # noqa: E402
import tlm_report as tr  # noqa: E402

E_PLUS, E_MINUS = -0.014, 0.006   # synthetic ETLMO+ / ETLMO-


# --------------------------------------------------------------------------- fixtures


def _reg(rows: list[list]) -> pd.DataFrame:
    """Build a registration frame shaped like load_registration() output."""
    cols = ["BM Unit ID", "BMU Name", "Party Name", "BMU Type", "GSP Group Id",
            "Prod/Cons Flag", "Prod/Cons Status", "TLF"]
    df = pd.DataFrame(rows, columns=cols)
    df["GSP Group Name"] = df["GSP Group Id"].map(lambda g: tr.GSP_ZONE.get(g, (0, None))[1])
    for c in ("Prod/Cons Status", "Prod/Cons Flag", "GSP Group Id", "BMU Type"):
        df[c] = df[c].astype("string")
    return df


@pytest.fixture
def reg() -> pd.DataFrame:
    """One supplier BMU per GSP Group (current zone TLF = zone / 1000), plus edge cases."""
    suppliers = [[f"2__{g[1]}SUPP", "", "", "S", g, None, "C", z / 1000]
                 for g, (z, _) in tr.GSP_ZONE.items()]
    edge_cases = [
        ["T_GEN-1", "", "", "T", None, None, "P", 0.014],    # zone 14 via TLF, no GSP Group
        ["T_ODD-1", "", "", "T", None, None, "P", 0.0],      # unmappable
        ["V__X001", "", "", "V", "_H", None, "C", 0.013],    # TLF says zone 13, GSP says zone 8
        ["E_ODD", "", "", "E", "_B", None, "C", 0.5],        # TLF matches nothing: GSP fallback
        ["T_NEW-1", "", "", "T", None, None, None, 0.001],   # blank P/C status and flag
        ["T_FLAG-1", "", "", "T", None, "P", None, 0.001],   # blank status, flag P
    ]
    return _reg(suppliers + edge_cases)


@pytest.fixture
def tlm() -> pd.DataFrame:
    """Two days x two periods of zonal TLMs with TLF(z) = z/1000 and TLMO = ±0.001 x period."""
    rows = []
    for d in (date(2025, 1, 1), date(2025, 1, 2)):
        for sp in (1, 2):
            for z in tr.ZONES:
                tlmo = 0.001 * sp
                rows.append([d, "RF", sp, z, 1 + z / 1000 + tlmo, 1 + z / 1000 - tlmo])
    return pd.DataFrame(rows, columns=tr.TLM_COLUMNS)


def _build(reg: pd.DataFrame, tlm: pd.DataFrame) -> pd.DataFrame:
    """Run build_report() with season values 1 + zone/100, indexed by BMU ID."""
    zones = tr.zone_tlf_table(reg)
    stats, _ = tr.zone_averages(tlm, [(date(2025, 1, 1), date(2025, 1, 2))])
    season = pd.DataFrame({"Zone": list(tr.ZONES), "1 + TLF": [1 + z / 100 for z in tr.ZONES],
                           "Confidence": "High"})
    season["Season Zone TLF"] = season["1 + TLF"] - 1
    return tr.build_report(tr.assign_zones(reg, zones), season, zones, stats).set_index("BM Unit ID")


def _obs(pairs: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(pairs, columns=["BM Unit ID", "Stack TLM"])


# --------------------------------------------------------------------------- zones and report


def test_zone_mapping(reg, tlm):
    r = _build(reg, tlm)
    assert r.loc["T_GEN-1", "Zone"] == 14 and r.loc["T_GEN-1", "Zone Source"] == "TLF"
    assert pd.isna(r.loc["T_ODD-1", "Zone"]) and r.loc["T_ODD-1", "Zone Source"] == "Unmapped"
    assert r.loc["V__X001", "Zone"] == 13 and "GSP Group is zone 8" in r.loc["V__X001", "Notes"]
    assert r.loc["E_ODD", "Zone"] == 2 and r.loc["E_ODD", "Zone Source"] == "GSP Group"


def test_report_tlm_columns(reg, tlm):
    r = _build(reg, tlm)
    assert r.loc["T_GEN-1", "TLM"] == pytest.approx(1.14)                  # 1 + season TLF
    assert r.loc["T_GEN-1", "Avg Settled TLM"] == pytest.approx(1.0125)    # P -> Delivering
    assert r.loc["2__PSUPP", "Avg Settled TLM"] == pytest.approx(1.0155)   # C -> Off Taking
    assert r.loc["T_NEW-1", "Settled Basis"] == "Off Taking"
    assert "assumed C" in r.loc["T_NEW-1", "Notes"]
    assert r.loc["T_FLAG-1", "Settled Basis"] == "Delivering"


def test_zone_tlf_table_requires_distinct_tlfs(reg):
    dup = reg.copy()
    dup.loc[dup["GSP Group Id"] == "_B", "TLF"] = 0.001   # same as zone 1
    with pytest.raises(ValueError, match="not unique"):
        tr.zone_tlf_table(dup)


# --------------------------------------------------------------------------- seasons and CLI


def test_season_segments():
    assert tr.season_segments("Winter 2022") == [(date(2022, 12, 1), date(2023, 2, 28))]
    assert tr.season_segments("winter 2023") == [(date(2023, 12, 1), date(2024, 2, 29))]
    assert tr.season_segments("Summer 2025") == [(date(2025, 6, 1), date(2025, 8, 31))]
    assert tr.season_segments("Autumn 2025") == [(date(2025, 9, 1), date(2025, 11, 30))]
    assert tr.season_segments("Spring 2025") == [(date(2025, 4, 1), date(2025, 5, 31)),
                                                 (date(2026, 3, 1), date(2026, 3, 31))]
    assert tr.bsc_year_seasons(2024) == ["Spring 2024", "Summer 2024", "Autumn 2024", "Winter 2024"]
    assert tr.latest_complete_winter(date(2026, 11, 10)) == "Winter 2025"
    assert tr.latest_complete_winter(date(2026, 2, 10)) == "Winter 2024"
    with pytest.raises(ValueError):
        tr.season_segments("Wintr 2022")


def test_resolve_periods_orders_by_date_and_validates():
    args = argparse.Namespace(start=None, end=None, label=None, season=["Winter 2022"],
                              bsc_year=[2022])
    labels = [label for label, _ in tr.resolve_periods(args, pd.DataFrame(columns=tr.TLM_COLUMNS))]
    assert labels == ["Spring 2022", "Summer 2022", "Autumn 2022", "Winter 2022", "Winter 2022"]
    bad = argparse.Namespace(start=date(2025, 1, 1), end=None, label=None, season=None, bsc_year=None)
    with pytest.raises(ValueError):
        tr.resolve_periods(bad, pd.DataFrame(columns=tr.TLM_COLUMNS))


def test_should_compare():
    ref = Path("Winter 2022 TLMs.xlsx")
    labels = ["Summer 2022", "Winter 2022"]
    assert tr.should_compare("Winter 2022", ref, labels)
    assert not tr.should_compare("Summer 2022", ref, labels)
    assert tr.should_compare("Summer 2022", Path("reference.xlsx"), labels)   # names no period


# --------------------------------------------------------------------------- settled TLMs


def test_zone_averages_out_of_range(tlm):
    with pytest.raises(ValueError):
        tr.zone_averages(tlm, [(date(2024, 12, 31), date(2025, 1, 2))])


def test_zone_averages_over_segments(tlm):
    segments = [(date(2025, 1, 1), date(2025, 1, 1)), (date(2025, 1, 2), date(2025, 1, 2))]
    stats, runs = tr.zone_averages(tlm, segments)
    assert stats["Settlement Periods"].iloc[0] == 4
    assert runs["Settlement Run Type"].tolist() == ["RF"]


def test_check_consistency_flags_delivering_equal_off_taking(tlm):
    bad = tlm.copy()
    bad.loc[bad["Zone"] == 5, "Delivering"] = bad.loc[bad["Zone"] == 5, "Off Taking"]
    stats, _ = tr.zone_averages(bad, [(date(2025, 1, 1), date(2025, 1, 2))])
    assert "zone 5: 4" in tr.check_consistency(stats)


def test_tlm_relative(tlm):
    rel = tr.tlm_relative(tlm, date(2025, 1, 1), date(2025, 1, 2))
    assert rel[1] == 0 and rel[14] == pytest.approx(0.013)
    assert tr.tlm_relative(tlm, date(2025, 1, 1), date(2025, 1, 3)) is None   # not fully covered


# --------------------------------------------------------------------------- TLF recovery


def test_band_zone_tlf_api_only():
    true = {1: 1.01, 2: 1.02}
    bmu_zone = {"A1": 1, "A2": 1, "B1": 2}
    status = {"A1": "P", "A2": "C", "B1": "P"}
    obs = _obs([("A1", true[1] + E_PLUS), ("A2", true[1] + E_MINUS), ("B1", true[2] + E_PLUS)])
    res = tr.band_zone_tlf(obs, E_PLUS, E_MINUS, bmu_zone, status).set_index("Zone")
    assert res.loc[1, "1 + TLF"] == pytest.approx(1.01) and res.loc[1, "Confidence"] == "High"
    assert res.loc[2, "1 + TLF"] == pytest.approx(1.02)
    assert res.loc[2, "Confidence"] == "Medium (decided by P/C status)"
    assert res.loc[3, "Confidence"] == "None"


def test_band_zone_tlf_anchored():
    rel = pd.Series({z: z / 1000 for z in tr.ZONES}) - 0.001   # TLF(z) - TLF(1)
    level = 1.005
    bmu_zone = {"A": 1, "B": 5, "C": 14}
    status = {"A": "P", "B": "C", "C": "P"}
    obs = _obs([("A", level + E_PLUS), ("B", level + rel[5] + E_MINUS),
                ("C", level + rel[14] + E_PLUS)])
    res = tr.band_zone_tlf(obs, E_PLUS, E_MINUS, bmu_zone, status, rel).set_index("Zone")
    assert res.loc[1, "1 + TLF"] == pytest.approx(level)
    assert res.loc[9, "1 + TLF"] == pytest.approx(level + rel[9])
    assert res.loc[9, "Confidence"] == "High"


def test_full_tie_is_deterministic_and_low():
    # One BMU with no P/C status: both candidates tie, so the smaller value wins, marked Low.
    obs = _obs([("X", 1.0)])
    res = tr.band_zone_tlf(obs, E_PLUS, E_MINUS, {"X": 1}, {"X": None}).set_index("Zone")
    assert res.loc[1, "1 + TLF"] == pytest.approx(1.0 - E_MINUS)
    assert res.loc[1, "Confidence"] == "Low"


# --------------------------------------------------------------------------- elexon_api helpers


def test_etlmo_bands_split_and_missing_coverage():
    etlmo = pd.DataFrame({"From": [date(2025, 1, 1), date(2025, 1, 11)],
                          "To": [date(2025, 1, 10), date(2025, 1, 20)],
                          "ETLMO+": [-0.01, -0.02], "ETLMO-": [0.01, 0.02]})
    bands = api.etlmo_bands(etlmo, date(2025, 1, 5), date(2025, 1, 15))
    assert [(b["From"], b["To"]) for b in bands] == [(date(2025, 1, 5), date(2025, 1, 10)),
                                                     (date(2025, 1, 11), date(2025, 1, 15))]
    with pytest.raises(ValueError, match="SUPPLEMENTARY_ETLMO"):
        api.etlmo_bands(etlmo, date(2025, 1, 15), date(2025, 1, 25))


def test_compact_keeps_only_id_and_tlm():
    raw = {"data": [{"id": "T_A-1", "transmissionLossMultiplier": 0.99, "volume": 5}]}
    assert api.compact(raw) == {"pairs": [["T_A-1", 0.99]]}
