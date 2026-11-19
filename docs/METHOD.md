# Method and evidence

This records **how** the tool works and **why**, including the checks that support each design
choice. It's written so someone who wasn't involved can trust or challenge the results.

## 1. Background: what a TLM is

GB transmission losses are shared out through **Transmission Loss Multipliers** (BSC Section T).
A BM Unit's metered energy is multiplied by its TLM in settlement:

```
TLM(i, j) = 1 + TLF(zone of i, season of j) + TLMO±(j)
```

| Term | Meaning | Varies by |
|---|---|---|
| **TLF** | Transmission Loss Factor. Set before the year for each of 14 TLM zones and each BSC season (modification P350, April 2018). | zone, season |
| **TLMO+ / TLMO−** | Transmission Loss Adjustment for **delivering** / **offtaking** BMUs. Calculated in settlement so that total losses are shared out. | settlement period |
| **ETLMO+ / ETLMO−** | *Estimated* TLMO, published by Elexon for each season and used for indicative prices. | season |

The 14 TLM zones match the 14 GSP groups:

| Zone | GSP Group | Name | Zone | GSP Group | Name |
|---|---|---|---|---|---|
| 1 | `_A` | Eastern | 8 | `_H` | Southern |
| 2 | `_B` | East Midlands | 9 | `_J` | South Eastern |
| 3 | `_C` | London | 10 | `_K` | South Wales |
| 4 | `_D` | Merseyside and North Wales | 11 | `_L` | South Western |
| 5 | `_E` | Midlands | 12 | `_M` | Yorkshire |
| 6 | `_F` | Northern | 13 | `_N` | South Scotland |
| 7 | `_G` | North Western | 14 | `_P` | North Scotland |

**How we know the zone order:** in the Elexon TLM file (`tlm.csv`), `Off Taking − 1 − TLF(zone)` is
identical across all 14 zones for any settlement period, when the zones are matched to GSP groups
in this order.

## 2. What the original "Winter 2022 TLMs.xlsx" contained

The reference workbook has two columns, `BM Unit ID` and `TLM`, and 5,275 rows. Analysis showed:

- It has **exactly 14 distinct values**, one per zone. Generators and demand units in the same zone
  have the same value, so it is **not** a delivering/offtaking TLM.
- For Dec 2022 – Feb 2023, every BMU's indicative TLM in the API minus the reference value equals
  exactly ETLMO+ (−0.01444) or ETLMO− (+0.00608) for that season.
- So **reference value = 1 + TLF(zone, Winter 2022/23)**. The tool's `TLM` column uses this definition.

Reference zone values (1 + TLF, Winter 2022 = Dec 2022 – Feb 2023). These are also used by
`tests/test_known_values.py`:

| Zone | 1 + TLF | Zone | 1 + TLF |
|---|---|---|---|
| 1 `_A` | 1.0075366 | 8 `_H` | 1.0250176 |
| 2 `_B` | 1.0100104 | 9 `_J` | 1.0158121 |
| 3 `_C` | 1.0238107 | 10 `_K` | 1.0182911 |
| 4 `_D` | 1.0037283 | 11 `_L` | 1.0298018 |
| 5 `_E` | 1.0199884 | 12 `_M` | 0.9909048 |
| 6 `_F` | 0.9929260 | 13 `_N` | 0.9892120 |
| 7 `_G` | 0.9978328 | 14 `_P` | 0.9601868 |

## 3. Seasons

TLFs are set for each **BSC season** within each BSC Year (1 April – 31 March). The seasons are
Elexon's own, as used in their ETLMO announcements:

| Season (of BSC Year YYYY/YY+1) | Dates |
|---|---|
| Spring ("Spring A" + "Spring B") | Apr–May YYYY + Mar YYYY+1 |
| Summer | Jun–Aug YYYY |
| Autumn | Sep–Nov YYYY |
| Winter | Dec YYYY – Feb YYYY+1 |

**Evidence that Winter means Dec–Feb, not the trading-season Oct–Mar.** These are the
zone 1 + TLF values from Oct 2022 to Mar 2023:

| Zone | Reference | Oct–Nov 22 | **Dec 22–Feb 23** | Mar 23 | Oct–Mar average |
|---|---|---|---|---|---|
| 1 Eastern | 1.00754 | 1.00378 | **1.00754** | 1.00276 | 1.00546 |
| 14 North Scotland | 0.96019 | 0.97600 | **0.96019** | 0.97388 | 0.96782 |

Building Winter 2022 as Dec–Feb matches **3,808 of 3,808** BMUs that appear in both files. Building
it as Oct–Mar matches **0**.

**Evidence that Spring means Apr–May plus the *following* March.** These are zone 1 + TLF values:

| Spring | Apr–May | Following March | Preceding March |
|---|---|---|---|
| 2022 (zone 1) | 1.00276 | **1.00276** | 1.00514 |
| 2023 (zone 3) | 1.02123 | **1.02123** | 1.01456 |
| 2024 (zone 14) | 0.97432 | **0.97432** | 0.96189 |

All 14 zones match the same way for 2022–2024. 2025 is an exception (see §8).

## 4. Recovering seasonal zone TLFs

Seasonal zonal TLFs are on the Elexon Portal behind a login, so the tool derives them from public
data instead.

1. **ETLMO table.** This is Elexon's CSV of ETLMO± by date band. Values missing from the CSV are
   added from Elexon's announcements via `SUPPLEMENTARY_ETLMO` in `elexon_api.py`.
2. **Settlement stacks.** The tool calls
   `GET https://data.elexon.co.uk/bmrs/api/v1/balancing/settlement/stack/all/{bid|offer}/{date}/{period}`.
   - Each entry has `id` (the BMU) and `transmissionLossMultiplier`, which equals 1 + TLF + ETLMO±.
   - By default the tool samples 6 settlement periods a day (2, 10, 18, 26, 34, 42), for both bids
     and offers.
   - Entries with TLM = 1.0 are placeholders and are dropped. These are common before 2022 and
     throughout 1–15 Apr 2025.
3. **Candidates.** Each (BMU, TLM) pair gives two candidates for 1 + TLF:
   - TLM − ETLMO+, if the BMU was delivering
   - TLM − ETLMO−, if it was offtaking
4. **Vote.**
   - **Without `tlm.csv`:** for each zone, the value supported by the most *distinct* BMUs wins.
     Ties go to the candidate backed by more BMUs whose registered P/C status matches
     (P = delivering, C = offtaking).
   - **With `tlm.csv` covering the band:** zone-to-zone differences are taken exactly from
     `tlm.csv`. TLF(z) − TLF(1) equals OffTaking(z) − OffTaking(1) in every period. A single vote
     across all zones then sets the overall level, which is very robust.
5. **Combine bands.** A season can cross several ETLMO bands; Spring always does. Each zone's
   value is a day-weighted average over the bands **that have data** for that zone. Bands without
   data are excluded and flagged. If the band values differ within a season, which shouldn't
   normally happen, the result is flagged as a blend.

**Confidence labels** in the Zone Summary sheet:

| Label | Meaning |
|---|---|
| `High` | The winning value is backed by 2 or more distinct BMUs and beats the runner-up. With anchoring, 3 or more BMUs. |
| `Medium` | Only 1 BMU, but no competing value. |
| `Medium (decided by P/C status)` | Tied on BMU count, and decided by registered P/C status. |
| `Low` / `None` | Unresolved, or no data. |

Extra notes like "data for X of Y days" or "TLF changed within season (blend)" are added when relevant.

## 5. Assigning BMUs to zones

- **Current zone TLFs:** from the registered BMU file, take the most common TLF of the supplier
  (`S`) BMUs in each GSP group. All 14 values are distinct.
- **Each BMU's zone:** match its registered `TLF` to those values. This covers transmission (`T_`)
  and interconnector (`I_`) units, which have no GSP group.
- **Fallback:** the BMU's `GSP Group Id`. Disagreements between the two are noted per BMU.
- **Unmatched:** BMUs with neither are left without a zone. In Nov 2026 this was just one:
  `T_DEPKG-1`, which has TLF 0.
- **Snapshot limitation:** the zone comes from the *current* registration. A BMU that changed zone
  would be shown in its current zone for past seasons, but zone changes are rare. BMUs that have
  been deregistered don't appear: about 1,467 of the 2022 reference BMUs are no longer registered.

## 6. Settled averages (optional, needs `tlm.csv`)

`tlm.csv` gives settled zonal TLMs for each period, `Delivering` and `Off Taking`, with the latest
settlement run per date. For each zone it gives:
- `Avg Delivering TLM` and `Avg Off Taking TLM`, averaged over the season
- `Avg Settled TLM`: Delivering for P BMUs, Off Taking for C BMUs (blank status is treated as C)

**Consistency check:** `Off Taking − Delivering` should be the same in every zone. Periods where a
zone has `Delivering == Off Taking` are counted and reported (see §8).

## 7. Validation performed (2026-11-19)

| Check | Result |
|---|---|
| Winter 2022 vs the reference workbook | 3,808 of 3,808 common BMUs match. 3,601 are identical to 7 d.p.; 207 (zone 10) differ by 0.0000001 because the API gives 6 d.p. |
| Winter 2022 built as Oct–Mar | 0 match |
| API-only vs `tlm.csv`-anchored, Winters 2024 and 2025 | All 14 zones identical |
| Settled Off Taking − (1 + TLF) across zones | Constant within each season, as expected |
| Range of all outputs, Spring 2022 – Summer 2026 | 0.953 to 1.032; no missing zones |

## 8. Known data issues

- **Spring 2025 is a blend.** Elexon corrected the 2025/26 TLFs ("Corrected TLFs for 2025/26",
  May 2025).
  - 1–15 Apr 2025: the stack TLMs are all 1.0, so there's no data.
  - 16 Apr – 31 May 2025: the 2024/25 Spring TLFs applied.
  - Mar 2026: the 2025/26 Spring values applied.

  The tool reports a day-weighted blend and flags it.
- **API coverage gaps.** Zone 6 has no stack data in Apr–May and Oct–Nov 2023. The affected
  seasons use the other bands' data and are flagged.
- **Anomaly in `tlm.csv`.** In RF runs from 16 Jul 2024 to 14 Jan 2025, some zones (mostly 5, also
  3, 8 and 11) have `Delivering == Off Taking`. `Off Taking` agrees with the other zones, so it's
  `Delivering` that looks wrong. This only affects the optional settled Delivering averages.

## 9. Things investigated and ruled out

- **Insights API TLM dataset:** there isn't one. A search of the OpenAPI spec for "loss" and "tlm"
  only finds `transmissionLossMultiplier` in settlement stacks, and `transmissionLossFactor` in BMU
  reference data, which is current values only.
- **Elexon Portal TLM page:** Elexon retired the BSC Portal "TLM Data" page in May 2023. Historic
  TLM files still exist on the Elexon Portal but need a login.
- **Settlement stack TLM as the "true" TLM:** it's indicative (it uses ETLMO, not the settled TLMO)
  and only covers BMUs with accepted bids or offers. That makes it suitable for recovering TLF, but
  not as a settled TLM.
- **Insights `/reference/bmunits/all`** has about 3,100 BMUs but no supplier (`2__`) units, so the
  Portal registration file is used instead.
