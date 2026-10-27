# 093 B2 Wicking – Groundworks quality.db processing

One Python script, `wick_process.py`, turns a day's Trimble Groundworks `*.quality.db` files (a WorksManager export) into everything needed for project 093 B2 Wicking: CSVs and a GeoPackage for Excel/QGIS, plus the per-hole IREDES XML files Groundworks itself exports. `run_wick.bat` runs it with a double-click. Runs on any PC with Python 3.8+; no extra packages.

## Daily use

1. Put the day's export in `...\Wicking\Data\_from_Machine\` (any folder whose name contains the date, e.g. `Export.20261027_111309` or `20261027`).
2. Double-click `run_wick.bat`.
3. Enter the date as `yyyymmdd` (Enter = today). If no folder for that date exists, it says so and lists the dates it can see.

Everything is written to `_from_Machine\<yyyymmdd>\outputs\`:

```
_from_Machine\
  Export.20261027_111309\        raw data (any folder name containing the date)
  20261027\
    wick_process.log             full output of every run for this date (appended)
    outputs\
      holes.csv  depth_log.csv  layers.csv  sources.csv
      wick_asdrilled.gpkg        holes (PointZ collar), hole_axes (LineStringZ), depth_log, layers, sources
      manifest.json              inputs, options, holes per design, latest time_end, counts
      iredes\
        B1 WICK D v3\
          <HoleId>.quality.xml               DRPQual
          B1 WICK D v3.quality.<HoleId>.mwd.xml  DRMWD
```

Running the same date again rewrites that date's outputs.

## Logs

- `_from_Machine\<yyyymmdd>\wick_process.log` – everything the run printed (files read, hole counts, output paths) and the full error if it failed. Each run is appended under a `=====` header with time, result and duration.
- `_from_Machine\wick_process.log` – one tab-separated line per run, for every date, including failed runs: time, `OK`/`FAILED`/`CANCELLED`, date, a summary (holes, IREDES files, source folders) or the error, and the Windows user.

A run that fails before it finds any data (e.g. no folder for that date) only appears in the history log.

## Copying XML to the delivery folders (testing)

`wick_copy_xml.py` (double-click `run_copy_xml.bat`) copies a date's IREDES files from `_from_Machine\<yyyymmdd>\outputs\iredes\` into the delivery folders, without the design subfolders:

| Files | Copied to |
|---|---|
| `*.mwd.xml` | `Data\XML\MWD\` |
| `*.quality.xml` | `Data\XML\XML\` |

- A file is **skipped** if a file with the same name is anywhere in that folder's `_archive` (subfolders included, case-insensitive), so already-delivered holes are not sent again.
- A file already in the delivery folder is left alone if identical. If it differs it is reported and not replaced unless `--overwrite` is given.
- The same file name coming from two designs is reported and not copied, since the delivery folders are flat.
- Both delivery folders must exist; nothing is copied if either is missing.
- `--dry-run` lists what would be copied without copying. The batch file offers a dry run first and then asks before copying.
- Each run is appended to `_from_Machine\<yyyymmdd>\wick_copy_xml.log`.

Defaults (`DEFAULT_ROOT`, `DEFAULT_XML` at the top of the script) can be changed with `--root` and `--xml`.

## Options

Pass them after the batch file or script, e.g. `run_wick.bat --all-designs`.

| Option | Default | Meaning |
|---|---|---|
| `--date yyyymmdd` | prompt | Skip the prompt. Unattended runs (no console) use today. |
| `--root <folder>` | `C:\Users\GregLauer\OneDrive - mgncivil.com.au\Projects\093 B2 Paddock\Wicking\Data\_from_Machine` | Raw-data folder (`DEFAULT_ROOT` at the top of the script). |
| `--design-filter <text>` | `WICK` | Only designs whose name contains this text. |
| `--all-designs` | off | Process every design (PREDRILL etc.). |
| `--since <ISO date>` | none | Only holes with `time_end >=` this, e.g. `2026-10-13`. |
| `--keep-zero-channels` | off | Keep depth-log channels that are zero throughout (normally dropped; wick masts log only PP/PR). |
| `--srid <EPSG>` | −1 | Tag the GeoPackage with a coordinate system. Coordinates are the design's local site system, so leave at −1 unless the rig writes grid coordinates. |
| `--sysver` | `2.0.23201.521` | `EqpSysVer` in IREDES headers. |
| `--name` | `wick_asdrilled` | GeoPackage file name. |

## How it works / business rules

- **Inputs**: every folder directly under the root whose name contains the date is searched recursively for `*.quality.db`. Identical files (same MD5 and design) are read once.
- **Design name** comes from the file name: `B1 WICK A v3.B1 WICK A v3.985E0EDB.quality.db` → `B1 WICK A v3`.
- **Duplicate holes**: a hole in more than one database is kept once, from the copy with the latest `time_end`; its depth log, layers and IREDES files all come from that copy.
- `depth_m = start_z − end_z` (vertical depth). `plan_offset_m` is collar-to-toe horizontal drift. `duration_s` from start/end times.
- **IREDES**
  - `Hstatus` = `OK` when `Hole.completed = 1`, otherwise `Fail`; `interruptedItem` is the inverse.
  - Channel units: PR m/min; DP, PP, FP, RP, FLP Bar. The first six channels are named, the rest are empty `<Parameter />` slots.
  - Fixed values: identity transform matrix, `CsysType L`, `TypeOfHole EasersHole`, `DrillingMethod: COPROD`, `SampleDistance 1.0`.
  - `DrillBitDia` assumes `Hole.diameter` is in metres and writes millimetres.
  - `ChkSum` is a CRC32 of the file before `<IR:GenTrailer>`, **not** Groundworks' own algorithm. Consumers that validate it will reject the files.
- Windows paths over 260 characters (deep OneDrive export folders) are handled.

## Expected inputs

Trimble Groundworks `*.quality.db` SQLite files, opened read-only. Required: a `Hole` table with `id, item_id, time_start, time_end, start_x/y/z, end_x/y/z, operator, machine, hrms, vrms, note, completed, diameter`. Depth logs need `Hole_DepthRecording (item_id, columns)`, `Hole_DepthRecord (id, owner_id, time, depth)` and `Hole_DepthRecordValue (depthRecord_id, value)`.

## Updating

Double-click `git_pull.bat` in the scripts folder to get the latest version from GitHub.

## Known issues

- NULL `operator`, `machine`, `hrms`, `vrms` or depth-log values are written as blanks in IREDES. NULL coordinates still stop the run (the error is in the log).
- IREDES files always list all six channels even though wick masts log only PP/PR.
- Empty CSVs are written with no header row.
- The GeoPackage has no spatial index or `gpkg_extensions` table; fine for QGIS, slow for very large sets.

## Data & privacy

No project data is in this repo. Input databases and all outputs stay on the machine where the script runs. Outputs include operator names from `Hole.operator`.

## Dependencies

Python 3.8+ standard library only. Optional: `pyproj`, used to write full WKT for `--srid`; without it a placeholder is written and QGIS resolves the EPSG code.

## Platform support

Windows (primary), macOS and Linux.
