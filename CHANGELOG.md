# Changelog

## 2026-11-19: code tidy-up before going public
- Full docstrings and comments throughout `tlm_report.py` and `elexon_api.py`; `main()` split
  into `parse_args`, `resolve_periods`, `run_period` and `write_combined_workbook`.
- Cache files are written atomically, so an interrupted run can't leave a partial file.
- Vote ties are now broken deterministically (smallest value, labelled `Low`).
- More offline tests (15). All generated outputs are unchanged.

## 2026-11-19: first version
- Per-BMU TLM report for each Elexon BSC season: Spring (Apr–May + following Mar), Summer
  (Jun–Aug), Autumn (Sep–Nov), Winter (Dec–Feb).
- TLM = 1 + seasonal zonal TLF, recovered from Insights API settlement stacks minus Elexon's
  published ETLMO, and anchored to `tlm.csv` zone differences when that file is present.
- Validated against the original `Winter 2022 TLMs.xlsx`: 3,808 of 3,808 common BMUs match (207
  differ by 0.0000001 because of API precision). An Oct–Mar build matches 0, which confirms that
  BSC seasons are the right basis.
- Generated Spring 2022 to Summer 2026. Known issues are listed in docs/METHOD.md §8.
