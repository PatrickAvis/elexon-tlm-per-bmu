# Maintenance and troubleshooting

This tool depends on outside data sources that can change over time. This page lists what is
most likely to break, how to spot it and how to fix it.

## Environment

- **Python:** 3.10 or later. Developed on Python 3.12.4 with pandas 2.3.3, openpyxl 3.1.2 and
  pytest 7.4.4.
- **Libraries:** `requirements.txt` allows pandas 2.x and openpyxl 3.x. If a future pandas 3.x is
  ever installed and the tests fail, install the tested versions instead:
  `pip install "pandas==2.3.3" "openpyxl==3.1.2"`. If those don't install on a newer Python, use
  Python 3.12.
- **Always run `python -m pytest` first.** It runs offline in under a second.

## Refreshing inputs

| File | When | How |
|---|---|---|
| `data/reg_bm_units.csv` | Every time. It decides which BMUs are listed and which zone each is in. | Elexon Portal → Registered BM Units. See [../data/README.md](../data/README.md). |
| `data/tlm.csv` | Optional. Only needed for settled averages. | Elexon Portal → TLM data. |
| ETLMO table | Automatic. Re-downloaded when the cached copy is more than 30 days old. | `--refresh-etlmo` forces it. |

## Likely failures and fixes

| Symptom | Cause | Fix |
|---|---|---|
| `ETLMO table does not cover all of …` | Elexon hasn't added the new BSC Year to its CSV yet. In Nov 2026 the CSV stopped at Mar 2026 even though the 2026/27 values had been announced. | Search elexon.co.uk for "New 20XX-20YY Estimated Transmission Losses Adjustment values". Add each date band to `SUPPLEMENTARY_ETLMO` in `elexon_api.py` as `("YYYY-MM-DD", "YYYY-MM-DD", ETLMO+, ETLMO-)`. Rows are only used beyond the end of the CSV, so stale extras do no harm. |
| HTTP 404 or changed JSON from `data.elexon.co.uk` | The Insights API changed its path or fields. | Update `STACK_URL` in `elexon_api.py`, and the field names (`id`, `transmissionLossMultiplier`) in `compact()`. Check the developer portal (developer.data.elexon.co.uk) for the "Settlement bid-offer stacks (ISPSTACK)" endpoint. |
| HTTP 429 or timeouts | Rate limiting. | Rerun: completed calls are cached. Or lower `workers` in `stack_tlms()` (default 4). |
| `KeyError` / `No supplier BMUs…` / `Zone TLFs are not unique` while loading the BMU file | The Portal file format changed, e.g. header lines or column names. | Adjust `load_registration()` in `tlm_report.py`. It currently skips 2 title lines and expects `BM Unit ID`, `BMU Type`, `GSP Group Id`, `TLF`, `Prod/Cons Status`, `Prod/Cons Flag`. |
| Many `low-confidence zones` or `data for X of Y days` | Few BMUs from that zone appear in the sampled stacks, or the stacks only hold 1.0 placeholders. | Sample more periods: `--api-sps 2,6,10,14,18,22,26,30,34,38,42,46`. This doubles the API calls. If the stacks are all 1.0, the data simply isn't there. |
| Values look odd for a season | A real TLF change within the season, e.g. a correction by Elexon. | Look for "TLF changed within season (blend)" in the Info sheet, and check Elexon news for TLF corrections. |
| The Winter 2022 network test fails | The method or API has drifted. | Look at which zones differ. A 0.0000001 difference is tolerated. Bigger gaps suggest an API change or wrong ETLMO values. |
| BMU zone changes | The current registration says a BMU moved zone. | That's expected behaviour. Past seasons use the BMU's *current* zone (see METHOD §5). |

## Adding a new season

Once the season has ended, run:

```bash
python tlm_report.py --bsc-year 2027          # or --season "Summer 2027"
```

Seasons that haven't ended yet are skipped with `season not finished until …`. Spring finishes
latest, because it includes the following March.

## Where things live

| What | Where |
|---|---|
| Season definitions | `BSC_SEASONS` in `tlm_report.py` |
| Zone ↔ GSP group mapping | `GSP_ZONE` in `tlm_report.py` |
| Settlement periods sampled per day | `DEFAULT_SPS` in `tlm_report.py` |
| API URLs | `ETLMO_URL`, `STACK_URL` in `elexon_api.py` |
| Extra ETLMO values | `SUPPLEMENTARY_ETLMO` in `elexon_api.py` |
| Cache | `cache/etlmo.csv`, `cache/ispstack/<date>_<period>_<bid|offer>.json`. Safe to delete; it's re-downloaded when needed. |

## Verifying end-to-end

```bash
RUN_NETWORK_TESTS=1 python -m pytest tests/test_known_values.py -v      # PowerShell: $env:RUN_NETWORK_TESTS=1
```

This rebuilds the Winter 2022 zone values from the live API and checks them against the values
recorded in [METHOD.md §2](METHOD.md#2-what-the-original-winter-2022-tlmsxlsx-contained). If you
also have the original reference workbook, put it in `data/` and run:

```bash
python tlm_report.py --season "Winter 2022" --compare "data/Winter 2022 TLMs.xlsx"
```
