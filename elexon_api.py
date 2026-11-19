"""Download and cache the public Elexon data used to recover seasonal zonal TLFs.

Two sources, neither needing an API key:

* **ETLMO table**: Elexon's CSV of Estimated Transmission Loss Adjustments (ETLMO+ for
  delivering, ETLMO- for offtaking BMUs) by date band.
* **Settlement bid-offer stacks (ISPSTACK)** from the Elexon Insights API. Each entry carries
  the BMU's indicative TLM, which equals ``1 + TLF(zone, season) + ETLMO±``. Since ETLMO is
  known, ``1 + TLF`` can be recovered.

All downloads are cached under the cache directory, so reruns don't call the API again:

* ``etlmo.csv``: refreshed automatically when older than ``ETLMO_MAX_AGE_DAYS``.
* ``ispstack/<date>_<period>_<bid|offer>.json``: only ``[BMU id, TLM]`` pairs are kept.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ETLMO_URL = ("https://www.elexon.co.uk/bsc/documents/operations-settlement/transmission-losses/"
             "estimated-transmission-loss-adjustments-data/")
STACK_URL = "https://data.elexon.co.uk/bmrs/api/v1/balancing/settlement/stack/all/{side}/{d}/{sp}"
HEADERS = {"User-Agent": "elexon-tlm-per-bmu (python urllib)"}

ETLMO_MAX_AGE_DAYS = 30
HTTP_TIMEOUT_S = 60
HTTP_RETRIES = 3

# ETLMO values Elexon has announced but which were missing from its CSV when last checked.
# Rows are used only for dates after the CSV ends, so stale rows do no harm. Format:
# (from, to, ETLMO+, ETLMO-), with dates inclusive. Source for 2026/27: elexon.co.uk/bsc/article/
# new-2026-2027-estimated-transmission-losses-adjustment-values-published/
SUPPLEMENTARY_ETLMO = [
    ("2026-04-01", "2026-05-31", -0.01602, 0.00948),   # Spring A
    ("2026-06-01", "2026-08-31", -0.01612, 0.01068),   # Summer
    ("2026-09-01", "2026-11-30", -0.01706, 0.00991),   # Autumn
    ("2026-12-01", "2027-02-28", -0.01730, 0.00671),   # Winter
    ("2027-03-01", "2027-03-31", -0.01602, 0.00948),   # Spring B
]


def _get(url: str) -> bytes:
    """HTTP GET with a timeout and a simple linear back-off retry.

    Raises:
        The last ``urllib`` error if every attempt fails.
    """
    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as response:
                return response.read()
        except Exception:
            if attempt == HTTP_RETRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def _write_atomic(path: Path, data: bytes) -> None:
    """Write via a temporary file and rename, so an interrupted run can't leave a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- ETLMO


def load_etlmo(cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    """Load the ETLMO table, adding ``SUPPLEMENTARY_ETLMO`` rows beyond the end of Elexon's CSV.

    The cached copy is re-downloaded when ``refresh`` is set or the copy is older than
    ``ETLMO_MAX_AGE_DAYS`` (Elexon adds each new BSC Year in the spring). If a refresh fails,
    the cached copy is used with a note.

    Returns:
        One row per date band with columns From, To (inclusive ``date``s), ETLMO+ and ETLMO-,
        sorted by From.

    Raises:
        RuntimeError: if there's no cached copy and the download fails.
    """
    path = cache_dir / "etlmo.csv"
    stale = (not path.exists()
             or time.time() - path.stat().st_mtime > ETLMO_MAX_AGE_DAYS * 86400)
    if refresh or stale:
        try:
            _write_atomic(path, _get(ETLMO_URL))
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
    extra = extra[extra["From"] > df["To"].max()]
    return pd.concat([df, extra]).sort_values("From").reset_index(drop=True)


def etlmo_bands(etlmo: pd.DataFrame, start: date, end: date) -> list[dict]:
    """Split [start, end] into ETLMO bands, within which TLFs and ETLMOs are constant.

    Returns:
        A list of dicts with keys From, To, ETLMO+ and ETLMO-, clipped to the range.

    Raises:
        ValueError: if the table doesn't cover every day of the range. The message explains
            how to add the missing values.
    """
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


# --------------------------------------------------------------------------- settlement stacks


def compact(raw: dict) -> dict:
    """Reduce an ISPSTACK API response to the fields used: ``{"pairs": [[BMU id, TLM], ...]}``."""
    return {"pairs": [[x.get("id"), x.get("transmissionLossMultiplier")]
                      for x in raw.get("data", [])]}


def _stack(d: date, sp: int, side: str, cache_dir: Path) -> list[tuple[str, float | None]]:
    """(BMU id, TLM) pairs for one settlement period's bid or offer stack, from cache or API."""
    path = cache_dir / "ispstack" / f"{d}_{sp:02d}_{side}.json"
    if not path.exists():
        raw = json.loads(_get(STACK_URL.format(side=side, d=d, sp=sp)))
        _write_atomic(path, json.dumps(compact(raw), separators=(",", ":")).encode())
    try:
        obj = json.loads(path.read_bytes())
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)   # corrupt cache file: drop it so the next run re-fetches
        return []
    if "pairs" not in obj:             # cache file written before compact() existed
        obj = compact(obj)
    return [(bmu, tlm) for bmu, tlm in obj["pairs"]]


def stack_tlms(start: date, end: date, sps: list[int], cache_dir: Path,
               workers: int = 4) -> pd.DataFrame:
    """Distinct (BMU, TLM) pairs seen in the bid and offer stacks for the sampled periods.

    Calls ``2 x len(sps)`` endpoints per day, in parallel (``workers`` threads), using the cache
    where possible. Entries with TLM = 1.0 are dropped: the API uses 1.0 as a placeholder in
    older stacks (mostly before 2022) and in some later periods (e.g. 1-15 Apr 2025).

    Returns:
        A DataFrame with columns ``BM Unit ID`` and ``Stack TLM`` (rounded to 7 d.p.).
    """
    jobs = [(start + timedelta(n), sp, side)
            for n in range((end - start).days + 1) for sp in sps for side in ("bid", "offer")]
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda job: _stack(*job, cache_dir), jobs))
    pairs = {(bmu, round(tlm, 7))
             for result in results for bmu, tlm in result
             if bmu and tlm is not None and tlm != 1.0}
    return pd.DataFrame(sorted(pairs), columns=["BM Unit ID", "Stack TLM"])
