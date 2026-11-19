# Input data

The files in this folder are **not committed to git**. They're Elexon data, which is subject to
Elexon's terms, and they go out of date. Download them yourself into this folder.

| File | Needed? | Source |
|---|---|---|
| `reg_bm_units.csv` | **Yes** | Elexon Portal → Registered BM Units |
| `tlm.csv` | Optional | Elexon Portal → TLM data |
| `Winter 2022 TLMs.xlsx` | Optional | The original reference report, if you have it (for `--compare`) |

The Elexon Portal is at https://www.elexonportal.co.uk. Registration is free. Menu names may have
changed since this was written, so use the descriptions below to recognise the right file. The
Portal's "Scripting" page also lists direct download URLs that use your personal API key. **Never
commit that key.**

## `reg_bm_units.csv`: Registered BM Units

A CSV of every BM Unit registered with the Central Registration Agency (about 9,350 rows in Nov 2026).

- The first line is the title `BM Units Registered with Central Registration Agency`, followed by a
  blank line. The tool skips these 2 lines.
- The header row includes these columns, which the tool uses:
  `BM Unit ID, BMU Name, Party Name, Party ID, BMU Type, NGC BMU Name, GSP Group Id, GSP Group Name, …, Prod/Cons Flag, Prod/Cons Status, TLF, …`
- An example row:
  `2__AANGE001,"AG-ALIM02",…,S,…,_A,"Eastern",…,C,0.0083094,…`

## `tlm.csv`: zonal settled TLMs (optional)

One row per settlement date, settlement period and zone, using the latest settlement run per date.
The file the tool was developed with covered 17 Jun 2024 to 10 Nov 2026, about 590,000 rows.

```
Settlement Date,Settlement Run Type,Settlement Period,Zone,Off Taking,Delivering
17/06/2024,DF,1,1,1.0141805,0.9851269
```

- Dates are in `DD/MM/YYYY` format. Zones run 1–14.
- With this file present, the tool adds settled averages and anchors the zone TLFs to its exact
  zone differences.
- Without it, the zone TLFs come from the API alone, which gives identical values in testing.

## `Winter 2022 TLMs.xlsx`: reference (optional)

The earlier report this tool replaces. It has one sheet with two columns, `BM Unit ID` and `TLM`,
and 5,275 rows. Its 14 zone values are recorded in
[../docs/METHOD.md](../docs/METHOD.md#2-what-the-original-winter-2022-tlmsxlsx-contained), so you
can still validate the tool without it.
