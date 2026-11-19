# Elexon TLM per BMU

Builds a spreadsheet of **Transmission Loss Multipliers (TLMs) for every BM Unit** in the GB
electricity market, for each Elexon BSC season (Spring, Summer, Autumn, Winter). The output
continues an older `Winter 2022 TLMs.xlsx` report (`BM Unit ID`, `TLM`) and reproduces all
3,808 BMUs that appear in both. 207 of them differ by 0.0000001 because of API rounding.

The tool uses public Elexon data and needs no API key:
- the **Insights API** (`data.elexon.co.uk`)
- the **ETLMO table** on elexon.co.uk

It also needs one file you download yourself: the **Registered BM Units** list from the Elexon Portal.

> **Status:** written in November 2026 as a mostly one-off analysis tool, last verified
> 2026-11-19. If you're picking this up after a long gap, start with
> [Picking this up again](#picking-this-up-again).

---

## Contents
- [Quick start](#quick-start)
- [Picking this up again](#picking-this-up-again)
- [What you get](#what-you-get)
- [Seasons](#seasons)
- [How it works (summary)](#how-it-works-summary)
- [Command-line options](#command-line-options)
- [Repository layout](#repository-layout)
- [Testing](#testing)
- [Known data issues](#known-data-issues)
- [Data sources and licensing](#data-sources-and-licensing)

More detail is in **[docs/METHOD.md](docs/METHOD.md)** (method and evidence),
**[docs/MAINTENANCE.md](docs/MAINTENANCE.md)** (what can break and how to fix it) and
**[data/README.md](data/README.md)** (input files).

---

## Quick start

**You need:**
- Python 3.10 or later (tested on 3.12)
- git
- internet access
- a free [Elexon Portal](https://www.elexonportal.co.uk) account, to download the BMU list

```bash
git clone https://github.com/PatrickAvis/elexon-tlm-per-bmu.git
cd elexon-tlm-per-bmu

python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS / Linux:         source .venv/bin/activate
pip install -r requirements.txt

python -m pytest            # quick offline checks; should all pass
```

1. **Download the Registered BM Units file** from the Elexon Portal and save it as
   `data/reg_bm_units.csv`. See [data/README.md](data/README.md) for how to recognise the right file.
2. **Run it:**

   ```bash
   python tlm_report.py --bsc-year 2025                  # Spring, Summer, Autumn, Winter of BSC Year 2025/26
   python tlm_report.py --season "Winter 2025"           # a single season
   python tlm_report.py                                  # the latest complete winter
   ```
3. **Collect the results** from `output/`. There is one workbook per season, plus
   `TLMs by season.xlsx` when you run more than one season.

**Run time:** the first run of a season makes about 1,100 small API calls, which takes about a
minute. Responses are cached in `cache/`, so reruns take seconds. Doing four BSC years from
scratch takes about 15–20 minutes.

---

## Picking this up again

A checklist for coming back to this after months or years:

1. **Get the code and environment working:** `git pull`, recreate `.venv`, `pip install -r requirements.txt`,
   then `python -m pytest`. If the install or tests fail because of newer library versions, see
   [MAINTENANCE: environment](docs/MAINTENANCE.md#environment).
2. **Refresh the BMU list:** download a new `data/reg_bm_units.csv` from the Elexon Portal. The report
   lists BMUs that are registered *now*, and uses the current registration to place each BMU in a zone.
3. **Optionally refresh `data/tlm.csv`** (zonal settled TLMs from the Elexon Portal). It adds settled
   averages, but the core TLM column doesn't need it. See [data/README.md](data/README.md).
4. **Run the seasons you need,** e.g. `python tlm_report.py --bsc-year 2027 --bsc-year 2028`. Seasons
   that haven't ended yet are skipped automatically.
5. **Read the console output.** Look for `WARNING`, `low-confidence zones`, or
   `averaged over part of the range`. The same notes appear in each workbook's **Info** and
   **Zone Summary** sheets.
6. **Check it still reproduces the known answer:**
   `RUN_NETWORK_TESTS=1 python -m pytest tests/test_known_values.py`. In PowerShell, set the
   variable first: `$env:RUN_NETWORK_TESTS=1`. This rebuilds Winter 2022 from the live API and
   compares it with the published reference values.
7. **If something errors,** see [docs/MAINTENANCE.md](docs/MAINTENANCE.md), which covers the likely
   failures. The most likely one is a new BSC Year missing from Elexon's ETLMO table, and the error
   message says what to add.

---

## What you get

`output/<Season> <Year> TLMs.xlsx`, e.g. `Winter 2025 TLMs.xlsx`:

| Sheet | Contents |
|---|---|
| **TLMs** | One row per registered BMU. `BM Unit ID`, **`TLM`**, BMU name, party, type, GSP group, zone, zone source, P/C status, registered TLF, the season zone TLF and its confidence, the settled average TLM (if `tlm.csv` covers the season), and notes. |
| **Zone Summary** | One row per zone (1–14): 1 + TLF, how it was derived, how many API BMUs supported it, its confidence, and settled min/avg/max where available. |
| **Info** | Dates, method, data checks, sources, and BMUs without a zone. |
| **Reference Check** | Only with `--compare`: BMU-by-BMU comparison with a reference workbook. |

`output/TLMs by season.xlsx` (written when you run more than one season):

| Sheet | Contents |
|---|---|
| BMU TLMs by season | Each BMU's TLM, one column per season. |
| Zone TLMs by season | Each zone's 1 + TLF, one column per season. |
| Zone detail | The same zone values in long format, with settled averages. |

**The `TLM` column is 1 + TLF**: the BMU's zonal Transmission Loss Factor for that season, plus 1.
This is exactly what the 2022 report contained. It leaves out the small per-settlement-period
adjustment (TLMO±) that settlement adds on top. Where available, `Avg Settled TLM` shows the
season-average *settled* TLM, which includes that adjustment.

---

## Seasons

Elexon sets TLFs for each **BSC season**. Season *YYYY* belongs to BSC Year *YYYY/YY+1*, which runs
1 April YYYY to 31 March YYYY+1:

| Season YYYY | Dates | Elexon name |
|---|---|---|
| Spring | Apr–May YYYY **+** Mar YYYY+1 | "Spring A" + "Spring B" (same TLFs) |
| Summer | Jun–Aug YYYY | Summer |
| Autumn | Sep–Nov YYYY | Autumn |
| Winter | Dec YYYY – Feb YYYY+1 | Winter |

So **Winter 2022 = 1 Dec 2022 – 28 Feb 2023**.

> **Common misunderstanding:** the GB trading "seasons" (Summer = Apr–Sep, Winter = Oct–Mar) are
> **not** the periods TLFs are set for. Building Winter 2022 as Oct–Mar matches **0** BMUs in the
> reference file; Dec–Feb matches **all** of them. See [docs/METHOD.md](docs/METHOD.md#seasons).

For an arbitrary range, use `--start/--end`. Any range that crosses season boundaries is averaged
by day.

---

## How it works (summary)

1. **TLM = 1 + TLF(zone, season) + TLMO±(period).** This is BSC Section T. There are 14 TLM zones,
   one per GSP group (`_A` to `_P`).
2. **Zonal TLFs aren't published in a convenient public file,** so the tool recovers them:
   - The Insights API's settlement **bid-offer stack** gives each BMU's indicative TLM, which is
     1 + TLF + ETLMO±.
   - Elexon publishes **ETLMO** (the estimated TLMO) for each season.
   - So 1 + TLF = stack TLM − ETLMO. The tool takes a vote across many BMUs and periods.
3. **Each BMU's zone** comes from matching its registered TLF to the current zone TLFs, with its GSP
   group as the fallback.
4. **If `data/tlm.csv` covers the season,** its exact zone-to-zone differences are used, and the API
   only sets the overall level. Testing shows the API-only and anchored methods agree exactly.

Full detail and evidence: [docs/METHOD.md](docs/METHOD.md).

---

## Command-line options

| Option | Meaning |
|---|---|
| `--season "Winter 2022"` | One BSC season. Repeatable. |
| `--bsc-year 2025` | All four seasons of BSC Year 2025/26. Repeatable. |
| `--start YYYY-MM-DD --end YYYY-MM-DD [--label NAME]` | Any date range (day-weighted across seasons). |
| `--compare PATH` | Compare with a reference workbook (`BM Unit ID`, `TLM`). Applies to the season named in its file name. |
| `--reg PATH` | Registered BM Units CSV (default `data/reg_bm_units.csv`). |
| `--tlm PATH` | Zonal settled TLM CSV (default `data/tlm.csv`; optional). |
| `--out PATH` | Output file, when running a single report. |
| `--cache DIR` | Download cache (default `cache/`). |
| `--api-sps 2,10,18,26,34,42` | Settlement periods sampled per day from the API. More periods = more evidence, but slower. |
| `--refresh-etlmo` | Force a re-download of the ETLMO table. This happens automatically after 30 days. |

With no options, the tool builds the latest complete winter.

---

## Repository layout

```
tlm_report.py          Main script: seasons, zone assignment, TLF recovery, Excel output
elexon_api.py          Elexon downloads (ETLMO table, settlement stacks) with on-disk cache
tests/                 Offline unit tests, plus an optional network test with known values
docs/METHOD.md         How and why it works, with the evidence
docs/MAINTENANCE.md    What can break over time and how to fix it
data/README.md         Input files: what they are and where to get them
data/                  Your input files (git-ignored)
output/                Generated workbooks (git-ignored)
cache/                 API download cache, about 100 MB per 4 BSC years (git-ignored)
```

---

## Testing

```bash
python -m pytest                                   # offline unit tests (also run by GitHub Actions)
RUN_NETWORK_TESTS=1 python -m pytest tests/test_known_values.py   # end-to-end check vs known Winter 2022 values
```

The network test needs `data/reg_bm_units.csv` and internet access. The first time, it fetches
about 1,100 API responses.

---

## Known data issues

- **Spring 2025 is a blend.** Elexon corrected the 2025/26 TLFs from 16 Apr 2025:
  - From 16 Apr to 31 May 2025, the 2024/25 Spring TLFs applied.
  - For 1–15 Apr 2025, the API has only placeholder values (1.0), so those days are excluded.
  - March 2026 carries the corrected 2025/26 Spring TLFs.

  The tool flags this.
- **Gaps in API coverage.** In some periods no BMUs from a given zone appear in the stacks (e.g.
  zone 6 in Apr–May 2023). Those days are excluded and flagged ("data for X of Y days").
- **Precision.** For some BMUs the API gives TLMs to 6 decimal places instead of 7. This can leave a
  difference of 0.0000001: for Winter 2022, zone 10 comes out as 1.0182910 against 1.0182911.
- **Anomaly in `tlm.csv` (Jul 2024 – Jan 2025, RF runs).** Some zones show `Delivering` equal to
  `Off Taking`. This only affects the optional settled averages, and the tool flags it.

---

## Data sources and licensing

- **Code:** MIT licence, see [LICENSE](LICENSE).
- **Data:**
  - All market data comes from **Elexon**: the Insights API, elexon.co.uk and the Elexon Portal.
    It is subject to Elexon's own licence and terms of use.
  - Input files and generated outputs are deliberately **not** committed. Check Elexon's terms
    before sharing outputs publicly.
- **Not affiliated:** this project is independent and not affiliated with or endorsed by Elexon.
