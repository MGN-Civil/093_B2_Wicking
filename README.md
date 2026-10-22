# 093 B2 Wicking – Groundworks quality.db tools

Two Python scripts for getting as-drilled wick data out of Trimble Groundworks `*.quality.db` files (for example a WorksManager export) on project 093 B2 Wicking. `wick_quality_extract.py` merges every database into CSV and/or a GeoPackage for review in Excel or QGIS. `wick_quality_to_iredes.py` rebuilds the per-hole IREDES XML files Groundworks itself exports, for systems that take IREDES. Both run on any PC with Python 3.8+ and need no extra packages.

## Features

| Area | `wick_quality_extract.py` | `wick_quality_to_iredes.py` |
|---|---|---|
| Purpose | Analysis and GIS | Data exchange (IREDES V1.0) |
| Output | `holes.csv`, `depth_log.csv`, `layers.csv`, `sources.csv` and/or one `.gpkg` (`holes` PointZ, `hole_axes` LineStringZ, `depth_log`, `layers`, `sources`) | Per hole: `<HoleId>.quality.xml` (DRPQual) and `<design>.quality.<HoleId>.mwd.xml` (DRMWD) |
| Shape | All databases merged into one dataset | One folder per design (or `--flat`) |
| Tables read | `General`, `Hole`, `Hole_DepthRecording`, `Hole_DepthRecord`, `Hole_DepthRecordValue`, `Layer` | `Hole` and the depth-recording tables |
| Derived fields | `depth_m`, `plan_offset_m`, `duration_s` | – |
| Depth log | Every channel, long format | First 6 channels named, the rest as empty `<Parameter />` slots |
| Duplicates | Skips identical files (MD5 + design); keeps latest `time_end` per hole | Skips files already written, so re-runs are incremental |

## How it works / business rules

- **Design name** is taken from the file name: `B1 WICK A v3.B1 WICK A v3.985E0EDB.quality.db` → `B1 WICK A v3`.
- **`--since`** keeps holes with `time_end >=` the given ISO timestamp (text comparison, so use `YYYY-MM-DD`).
- **`--design-filter`** keeps databases whose design name contains the text (case-insensitive).
- **Extract**
  - `depth_m = start_z − end_z` (vertical depth, not along-hole length).
  - `--drop-zero-channels` removes depth-log channels that are zero for every record (wick masts log only PP/PR). Applied across all designs together.
  - Coordinates are copied as stored. `--srid` tags the GeoPackage with an EPSG code (default −1, undefined). Use the code for the coordinate system the design is in.
- **IREDES**
  - `Hstatus` = `OK` when `Hole.completed = 1`, otherwise `Fail`; `interruptedItem` is the inverse.
  - Channel units: PR m/min; DP, PP, FP, RP, FLP Bar.
  - Fixed values: identity transform matrix, `CsysType L`, `TypeOfHole EasersHole`, `DrillingMethod: COPROD`, `SampleDistance 1.0`, `EqpSysVer` from `--sysver` (default `2.0.23201.521`).
  - `DrillBitDia` assumes `Hole.diameter` is in metres and writes millimetres.
  - `ChkSum` is a CRC32 of the file before `<IR:GenTrailer>`, **not** Groundworks' own algorithm. Consumers that validate it will reject the files.

## Expected inputs

Trimble Groundworks `*.quality.db` SQLite files, passed as files or folders (folders are searched recursively). Required: a `Hole` table with `id, item_id, time_start, time_end, start_x/y/z, end_x/y/z, operator, machine, hrms, vrms, note, completed, diameter`. Depth logs need `Hole_DepthRecording (item_id, columns)`, `Hole_DepthRecord (id, owner_id, time, depth)` and `Hole_DepthRecordValue (depthRecord_id, value)`. Databases are opened read-only.

## Running

```bash
# CSV + GeoPackage, wick designs only, since 13 Oct, tagged MGA2020 zone 56
python wick_quality_extract.py "D:\WorksManager\Export" -o out\b2_wicks --design-filter WICK --since 2026-10-13 --srid 7856 --drop-zero-channels

# CSV only
python wick_quality_extract.py "D:\WorksManager\Export" -o out\b2_wicks --csv

# IREDES XML, incremental (re-runs only add new holes)
python wick_quality_to_iredes.py "D:\WorksManager\Export" -o out\iredes --design-filter WICK

# Rebuild specific holes
python wick_quality_to_iredes.py "D:\WorksManager\Export" -o out\iredes --hole 3B2740 3B2741 --overwrite
```

No deployment needed; copy the scripts to the PC that has the export.

## Known issues

**Extract**
- A hole found in two different databases is deduplicated, but its depth-log rows from both are kept, so its log appears twice.
- Empty CSVs are written with no header row.
- The GeoPackage has no spatial index or `gpkg_extensions` table; fine for QGIS, slow for very large sets.

**IREDES**
- Crashes on NULL `operator`, `machine`, `hrms`, `vrms`, coordinates or depth-log values.
- When the same hole is in two databases, the first one by path wins (or the last with `--overwrite`), not the newest.
- File create/close dates are the run time, so `--overwrite` changes them on every rerun.
- Wick masts log only PP/PR, but all six channels are written.

**Both**
- `os.path.relpath` fails on Windows if the database is on a different drive from the working directory. Run from the same drive.

## Data & privacy

No project data is in this repo. Input databases and all outputs stay on the machine where the scripts run. Note that outputs include operator names from `Hole.operator`.

## Dependencies

Python 3.8+ standard library only (`sqlite3`, `csv`, `struct`, `zlib`, `xml.sax.saxutils`). Optional: `pyproj`, used by the extract script to write full WKT for `--srid`; without it a placeholder definition is written and QGIS resolves the EPSG code.

## Platform support

Windows, macOS and Linux.
