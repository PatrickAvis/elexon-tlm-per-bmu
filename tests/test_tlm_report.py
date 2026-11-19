import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tlm_report as tr  # noqa: E402


def _reg(rows):
    cols = ["BM Unit ID", "BMU Name", "Party Name", "BMU Type", "GSP Group Id",
            "Prod/Cons Flag", "Prod/Cons Status", "TLF"]
    df = pd.DataFrame(rows, columns=cols)
    df["GSP Group Name"] = df["GSP Group Id"].map(lambda g: tr.GSP_ZONE.get(g, (0, None))[1])
    for c in ("Prod/Cons Status", "Prod/Cons Flag", "GSP Group Id", "BMU Type"):
        df[c] = df[c].astype("string")
    return df


@pytest.fixture
def reg():
    # one supplier BMU per GSP group defines current zone TLF = zone/1000
    supp = [[f"2__{g[1]}SUPP", "", "", "S", g, None, "C", z / 1000]
            for g, (z, _) in tr.GSP_ZONE.items()]
    extra = [
        ["T_GEN-1", "", "", "T", None, None, "P", 0.014],          # zone 14 via TLF
        ["T_ODD-1", "", "", "T", None, None, "P", 0.0],            # unmapped
        ["V__X001", "", "", "V", "_H", None, "C", 0.013],          # TLF zone 13, GSP zone 8
        ["E_ODD", "", "", "E", "_B", None, "C", 0.5],              # GSP fallback, zone 2
        ["T_NEW-1", "", "", "T", None, None, None, 0.001],         # blank status
        ["T_FLAG-1", "", "", "T", None, "P", None, 0.001],         # blank status, flag P
    ]
    return _reg(supp + extra)


@pytest.fixture
def tlm():
    rows = []
    for d in (date(2025, 1, 1), date(2025, 1, 2)):
        for sp in (1, 2):
            for z in range(1, 15):
                tlmo = 0.001 * sp
                rows.append([d, "RF", sp, z, 1 + z / 1000 + tlmo, 1 + z / 1000 - tlmo])
    return pd.DataFrame(rows, columns=["Settlement Date", "Settlement Run Type",
                                       "Settlement Period", "Zone", "Off Taking", "Delivering"])


def _build(reg, tlm, season_value=lambda z: 1 + z / 100):
    zones = tr.zone_tlf_table(reg)
    stats, _ = tr.zone_averages(tlm, [(date(2025, 1, 1), date(2025, 1, 2))])
    season = pd.DataFrame({"Zone": range(1, 15), "1 + TLF": [season_value(z) for z in range(1, 15)],
                           "Confidence": "High"})
    season["Season Zone TLF"] = season["1 + TLF"] - 1
    return tr.build_report(tr.assign_zones(reg, zones), season, zones, stats).set_index("BM Unit ID")


def test_zone_mapping(reg, tlm):
    r = _build(reg, tlm)
    assert r.loc["T_GEN-1", "Zone"] == 14 and r.loc["T_GEN-1", "Zone Source"] == "TLF"
    assert pd.isna(r.loc["T_ODD-1", "Zone"]) and r.loc["T_ODD-1", "Zone Source"] == "Unmapped"
    assert r.loc["V__X001", "Zone"] == 13 and "GSP Group is zone 8" in r.loc["V__X001", "Notes"]
    assert r.loc["E_ODD", "Zone"] == 2 and r.loc["E_ODD", "Zone Source"] == "GSP Group"


def test_tlm_columns(reg, tlm):
    r = _build(reg, tlm)
    assert r.loc["T_GEN-1", "TLM"] == pytest.approx(1.14)            # 1 + season TLF
    assert r.loc["T_GEN-1", "Avg Settled TLM"] == pytest.approx(1.0125)  # delivering avg
    assert r.loc["2__PSUPP", "Avg Settled TLM"] == pytest.approx(1.0155)  # off taking avg
    assert r.loc["T_NEW-1", "Settled Basis"] == "Off Taking"
    assert "assumed C" in r.loc["T_NEW-1", "Notes"]
    assert r.loc["T_FLAG-1", "Settled Basis"] == "Delivering"


def test_out_of_range(tlm):
    with pytest.raises(ValueError):
        tr.zone_averages(tlm, [(date(2024, 12, 31), date(2025, 1, 2))])


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


def test_settled_average_over_segments(tlm):
    segs = [(date(2025, 1, 1), date(2025, 1, 1)), (date(2025, 1, 2), date(2025, 1, 2))]
    stats, _ = tr.zone_averages(tlm, segs)
    assert stats["Settlement Periods"].iloc[0] == 4


E_PLUS, E_MINUS = -0.014, 0.006


def _obs(pairs):
    return pd.DataFrame(pairs, columns=["BM Unit ID", "Stack TLM"])


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
    rel = pd.Series({z: z / 1000 for z in range(1, 15)}) - 0.001   # TLF(z) - TLF(1)
    level = 1.005
    bmu_zone = {"A": 1, "B": 5, "C": 14}
    status = {"A": "P", "B": "C", "C": "P"}
    obs = _obs([("A", level + E_PLUS), ("B", level + rel[5] + E_MINUS), ("C", level + rel[14] + E_PLUS)])
    res = tr.band_zone_tlf(obs, E_PLUS, E_MINUS, bmu_zone, status, rel).set_index("Zone")
    assert res.loc[1, "1 + TLF"] == pytest.approx(level)
    assert res.loc[9, "1 + TLF"] == pytest.approx(level + rel[9])
    assert res.loc[9, "Confidence"] == "High"
