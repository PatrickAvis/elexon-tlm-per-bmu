"""End-to-end check against known values (uses the live Elexon API; skipped by default).

Rebuilds the Winter 2022 (Dec 2022 - Feb 2023) zone values (1 + TLF) and compares them with the
values in the original 'Winter 2022 TLMs.xlsx' report, recorded in docs/METHOD.md section 2.

    RUN_NETWORK_TESTS=1 python -m pytest tests/test_known_values.py -v
    (PowerShell: $env:RUN_NETWORK_TESTS=1; python -m pytest tests/test_known_values.py -v)

Needs data/reg_bm_units.csv. API responses are cached in cache/.
"""

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import elexon_api as api  # noqa: E402
import tlm_report as tr  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("RUN_NETWORK_TESTS") != "1",
                                reason="set RUN_NETWORK_TESTS=1 to run (calls the Elexon API)")

# 1 + TLF per zone for BSC Winter 2022, from the original reference report.
WINTER_2022 = {
    1: 1.0075366, 2: 1.0100104, 3: 1.0238107, 4: 1.0037283, 5: 1.0199884, 6: 0.9929260,
    7: 0.9978328, 8: 1.0250176, 9: 1.0158121, 10: 1.0182911, 11: 1.0298018, 12: 0.9909048,
    13: 0.9892120, 14: 0.9601868,
}


def test_winter_2022_zone_values():
    if not tr.DEFAULT_REG.exists():
        pytest.skip("needs data/reg_bm_units.csv (see data/README.md)")
    reg = tr.load_registration(tr.DEFAULT_REG)
    zones = tr.zone_tlf_table(reg)
    reg = tr.assign_zones(reg, zones)
    etlmo = api.load_etlmo(tr.DEFAULT_CACHE)
    no_tlm = pd.DataFrame(columns=tr.TLM_COLUMNS)  # 2022 predates tlm.csv: API-only method

    result, _ = tr.season_zone_tlf(tr.season_segments("Winter 2022"), no_tlm, reg, etlmo,
                                   tr.DEFAULT_SPS, tr.DEFAULT_CACHE)
    got = result.set_index("Zone")["1 + TLF"]
    # 2e-7 tolerance: the API gives some BMUs' TLMs to 6 d.p. (zone 10 comes out 1.0182910)
    mismatches = {z: (got.get(z), v) for z, v in WINTER_2022.items()
                  if pd.isna(got.get(z)) or abs(got[z] - v) > 2e-7}
    assert not mismatches, f"zone: (calculated, expected) {mismatches}"
