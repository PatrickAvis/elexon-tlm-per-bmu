# Changelog

## 2026-11-19: first version
- Per-BMU TLM report for each Elexon BSC season: Spring (Apr–May + following Mar), Summer
  (Jun–Aug), Autumn (Sep–Nov), Winter (Dec–Feb).
- TLM = 1 + seasonal zonal TLF, recovered from Insights API settlement stacks minus Elexon's
  published ETLMO, and anchored to `tlm.csv` zone differences when that file is present.
- Validated against the original `Winter 2022 TLMs.xlsx`: 3,808 of 3,808 common BMUs match (207
  differ by 0.0000001 because of API precision). An Oct–Mar build matches 0, which confirms that
  BSC seasons are the right basis.
- Generated Spring 2022 to Summer 2026. Known issues are listed in docs/METHOD.md §8.
