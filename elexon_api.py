"""Public Elexon data used to recover seasonal zonal TLFs.

* ETLMO table (Estimated Transmission Loss Adjustments), published by Elexon as CSV.
* Settlement bid-offer stacks (ISPSTACK) from the Insights API. Each entry carries the
  BMU's indicative TLM, which is 1 + TLF(zone, season) + ETLMO+ (delivering) or
  ETLMO- (offtaking). ETLMO is known, so 1 + TLF can be recovered.

All downloads are cached on disk so reruns don't hit the API again.
"""

from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ETLMO_URL = ("https://www.elexon.co.uk/bsc/documents/operations-settlement/transmission-losses/"
             "estimated-transmission-loss-adjustments-data/")
STACK_URL = "https://data.elexon.co.uk/bmrs/api/v1/balancing/settlement/stack/all/{side}/{d}/{sp}"
HEADERS = {"User-Agent": "tlm-report/1.0 (python urllib)"}

# ETLMO values announced by Elexon but missing from the CSV when last checked.
# Source: elexon.co.uk/bsc/article/new-2026-2027-estimated-transmission-losses-adjustment-values-published/
SUPPLEMENTARY_ETLMO = [
    ("2026-04-01", "2026-05-31", -0.01602, 0.00948),
    ("2026-06-01", "2026-08-31", -0.01612, 0.01068),
    ("2026-09-01", "2026-11-30", -0.01706, 0.00991),
    ("2026-12-01", "2027-02-28", -0.01730, 0.00671),
    ("2027-03-01", "2027-03-31", -0.01602, 0.00948),
]


def _get(url: str, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


ETLMO_MAX_AGE_DAYS = 30


def load_etlmo(cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    """ETLMO bands: From, To (inclusive dates), ETLMO+ and ETLMO-.

    The cached copy is re-downloaded when older than ETLMO_MAX_AGE_DAYS (Elexon adds a new
    BSC Year each spring). If the download fails, the cached copy is used.
    """
    path = cache_dir / "etlmo.csv"
    stale = not path.exists() or (time.time() - path.stat().st_mtime) > ETLMO_MAX_AGE_DAYS * 86400
    if refresh or stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_bytes(_get(ETLMO_URL))
        except Exception as e:
            if not path.exists():
                raise RuntimeError(f"Could not download the ETLMO table from {ETLMO_URL}: {e}") from e
            print(f"Note: could not refresh the ETLMO table ({e}); using the cached copy.")
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Effective From Date": "From", "Effective To Date": "To",
                            "ETLMOj+": "ETLMO+", "ETLMOj-": "ETLMO-"})
    df["From"] = pd.to_datetime(df["From"], dayfirst=True).dt.date
    df["To"] = pd.to_datetime(df["To"], dayfirst=True).dt.date
    extra = pd.DataFrame(SUPPLEMENTARY_ETLMO, columns=["From", "To", "ETLMO+", "ETLMO-"])
    extra["From"] = pd.to_datetime(extra["From"]).dt.date
    extra["To"] = pd.to_datetime(extra["To"]).dt.date
    extra = extra[extra["From"] > df["To"].max()]  # only fill beyond what the CSV has
    return pd.concat([df, extra]).sort_values("From").reset_index(drop=True)


def etlmo_bands(etlmo: pd.DataFrame, start: date, end: date) -> list[dict]:
    """Split [start, end] into ETLMO bands (TLFs and ETLMOs are constant within each)."""
    bands = []
    for f, t, ep, em in zip(etlmo["From"], etlmo["To"], etlmo["ETLMO+"], etlmo["ETLMO-"]):
        lo, hi = max(f, start), min(t, end)
        if lo <= hi:
            bands.append({"From": lo, "To": hi, "ETLMO+": float(ep), "ETLMO-": float(em)})
    covered = sum((b["To"] - b["From"]).days + 1 for b in bands)
    if covered != (end - start).days + 1:
        raise ValueError(
            f"ETLMO table does not cover all of {start}..{end} (it ends {etlmo['To'].max()}). "
            "Find Elexon's 'Estimated Transmission Loss Adjustment values' announcement for that "
            "BSC Year and add its rows to SUPPLEMENTARY_ETLMO in elexon_api.py "
            "(see docs/MAINTENANCE.md).")
    return bands


def compact(raw: dict) -> dict:
    """Keep only what is used from a stack response: [BMU id, TLM] pairs."""
    return {"pairs": [[x.get("id"), x.get("transmissionLossMultiplier")] for x in raw.get("data", [])]}


def _stack(d: date, sp: int, side: str, cache_dir: Path) -> list[dict]:
    path = cache_dir / "ispstack" / f"{d}_{sp:02d}_{side}.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.loads(_get(STACK_URL.format(side=side, d=d, sp=sp)))
        path.write_text(json.dumps(compact(raw), separators=(",", ":")))
    try:
        obj = json.loads(path.read_bytes())
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return []
    if "pairs" not in obj:  # older full-response cache file
        obj = compact(obj)
    return [{"id": i, "transmissionLossMultiplier": t} for i, t in obj["pairs"]]


def stack_tlms(start: date, end: date, sps: list[int], cache_dir: Path,
               workers: int = 4) -> pd.DataFrame:
    """Distinct (BMU, TLM) pairs seen in bid/offer stacks for the sampled periods.

    TLM = 1.0 entries are dropped: older stacks use 1.0 as a placeholder.
    """
    jobs = [(start + timedelta(n), sp, side)
            for n in range((end - start).days + 1) for sp in sps for side in ("bid", "offer")]
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda j: _stack(*j, cache_dir), jobs))
    rows = {(x["id"], round(x["transmissionLossMultiplier"], 7))
            for res in results for x in res
            if x.get("id") and x.get("transmissionLossMultiplier") not in (None, 1.0)}
    return pd.DataFrame(sorted(rows), columns=["BM Unit ID", "Stack TLM"])
