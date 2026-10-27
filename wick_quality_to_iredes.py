#!/usr/bin/env python3
"""
wick_quality_to_iredes.py - Write per-hole IREDES XML files from Trimble Groundworks *.quality.db

For every hole in every *.quality.db found, writes two files in the style Groundworks
itself produces:

  <out>/<design>/<HoleId>.quality.xml                 DRPQual  (hole quality record)
  <out>/<design>/<design>.quality.<HoleId>.mwd.xml    DRMWD    (measure-while-drilling log)

Usage
  python wick_quality_to_iredes.py <folder|db> [...]
        [--master <dir>] [--date yyyymmdd] | [-o <out_dir>]
        [--design-filter WICK] [--hole 3B2740 ...] [--since 2026-10-13]
        [--flat] [--overwrite] [--sysver 2.0.23201.521]

  Output goes to <master>/<yyyymmdd>/iredes/ unless -o is given.
  --master      master folder (default: the 093 B2 Paddock Scripts/out folder, see DEFAULT_MASTER)
  --date        yyyymmdd folder name; if omitted you are prompted (Enter = today)
  -o            write to this folder instead, ignoring --master/--date

  --flat        write all files into <out_dir> without the per-design subfolder
  --overwrite   rewrite files that already exist (default: skip, so re-runs are incremental)

Notes
  * Depth log channel order and units follow the rig's own MWD header:
    PR m/min, DP/PP/FP/RP/FLP Bar. Channels beyond the first six (FP_FORWARD, FP_BACK)
    are emitted as empty <Parameter /> slots, matching Groundworks output.
  * Hstatus is "OK" when Hole.completed = 1, otherwise "Fail"; interruptedItem is the inverse.
  * ChkSum is a CRC32 of the file content preceding <IR:GenTrailer>. Groundworks' exact
    algorithm is undocumented; consumers rarely validate it.
"""
import argparse, os, sqlite3, glob, re, sys, zlib, datetime, tempfile, shutil
from xml.sax.saxutils import escape

DEFAULT_MASTER = r"C:\Users\GregLauer\OneDrive - mgncivil.com.au\Projects\093 B2 Paddock\Wicking\Data\Scripts\out"

UNITS = {"PR": ("m/min", "PenetrRate"), "DP": ("Bar", "DampPressure"), "PP": ("Bar", "PercPressure"),
         "FP": ("Bar", "FeedPressure"), "RP": ("Bar", "RotPressure"), "FLP": ("Bar", "FlushPressure"),
         "FP_FORWARD": ("Bar", "FeedPressureFwd"), "FP_BACK": ("Bar", "FeedPressureBack")}
NAMED_PARAMS = 6   # Groundworks names the first six channels and leaves the rest as empty slots

def ts(s):
    """'2026-10-13 09:30:18.2401552' -> '2026-10-13T09:30:18'"""
    return s[:19].replace(" ", "T")

def infer_design_name(path):
    base = os.path.basename(path)
    m = re.match(r"^(.*?)\.\1\.[0-9A-Fa-f]{8}\.quality\.db$", base)
    return m.group(1) if m else re.sub(r"\.quality\.db$", "", base).split(".")[0]

def head(sysver, created):
    return f"""  <IR:GenHead>
    <IR:FileCreateDate>{created}</IR:FileCreateDate>
    <IR:IRversion DownwCompat="V 1.0">V 1.0</IR:IRversion>
    <IR:EquipmentInfo>
      <IR:EqpManufact>Trimble</IR:EqpManufact>
      <IR:EqpType>Groundworks</IR:EqpType>
      <IR:EqpSerNo>0</IR:EqpSerNo>
      <IR:EqpSysVer>{sysver}</IR:EqpSysVer>
      <IR:EqpInfo>DrillingMethod: COPROD</IR:EqpInfo>
    </IR:EquipmentInfo>
  </IR:GenHead>
"""

def finish(body, created, root):
    """append GenTrailer (CRC32 of everything before it) and close the root element"""
    chk = zlib.crc32(body.encode("utf-8")) & 0xFFFFFFFF
    return body + f"""  <IR:GenTrailer>
    <IR:FileCloseDate>{created}</IR:FileCloseDate>
    <IR:ChkSum>{chk}</IR:ChkSum>
  </IR:GenTrailer>
</{root}>
"""

def qual_xml(design, h, sysver, created):
    completed = bool(h["completed"]); over = bool(h.get("IsOverridden") or 0)
    dia = h.get("diameter"); dia_mm = f"{round(dia*1000):d}" if dia is not None else ""
    body = f"""<?xml  version = "1.0" encoding="utf-8"?>
<DRPQual xmlns="http://www.iredes.org/xml/DrillRig" xmlns:IR="http://www.iredes.org/xml" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" schemaLocation="http://www.iredes.org/xml/DrillRig" IRVersion="V 1.0" IRDownwCompat="V 1.0" DRPPlanDownwCompat="V 1.0" DRPPlanVersion="V 1.0">
{head(sysver, created)}  <IR:StartLogTime>{ts(h["time_start"])}</IR:StartLogTime>
  <IR:EndLogTime>{ts(h["time_end"])}</IR:EndLogTime>
  <IR:PlanIdRef>{escape(design)}</IR:PlanIdRef>
  <PositionQuality>
    <Coordsystem>
      <IR:CoordSysName>Local</IR:CoordSysName>
      <IR:TMatrix>
        <IR:Col>
          <IR:x>1.000</IR:x>
          <IR:y>0.000</IR:y>
          <IR:z>0.000</IR:z>
          <IR:w>0.000</IR:w>
        </IR:Col>
        <IR:Col>
          <IR:x>0.000</IR:x>
          <IR:y>1.000</IR:y>
          <IR:z>0.000</IR:z>
          <IR:w>0.000</IR:w>
        </IR:Col>
        <IR:Col>
          <IR:x>0.000</IR:x>
          <IR:y>0.000</IR:y>
          <IR:z>1.000</IR:z>
          <IR:w>0.000</IR:w>
        </IR:Col>
        <IR:Col>
          <IR:x>0.000</IR:x>
          <IR:y>0.000</IR:y>
          <IR:z>0.000</IR:z>
          <IR:w>1.000</IR:w>
        </IR:Col>
      </IR:TMatrix>
      <IR:CsysType>L</IR:CsysType>
    </Coordsystem>
  </PositionQuality>
  <DrillQuality>
    <HoleQualityData firstname="{escape(h["operator"], {'"': '&quot;'})}" lastname="" machinename="{escape(h["machine"], {'"': '&quot;'})}" interruptedItem="{not completed}" HRMS="{h["hrms"]:.4f}" VRMS="{h["vrms"]:.4f}" IsOverridden="{over}">
      <Hole>
        <HoleId>{escape(h["item_id"])}</HoleId>
        <HoleName />
        <StartPoint>
          <IR:PointX>{h["start_x"]:.3f}</IR:PointX>
          <IR:PointY>{h["start_y"]:.3f}</IR:PointY>
          <IR:PointZ>{h["start_z"]:.3f}</IR:PointZ>
        </StartPoint>
        <EndPoint>
          <IR:PointX>{h["end_x"]:.3f}</IR:PointX>
          <IR:PointY>{h["end_y"]:.3f}</IR:PointY>
          <IR:PointZ>{h["end_z"]:.3f}</IR:PointZ>
        </EndPoint>
        <TypeOfHole>EasersHole</TypeOfHole>
        <DrillBitDia>{dia_mm}</DrillBitDia>
        {"<Notes>" + escape(h["note"]) + "</Notes>" if h["note"] else "<Notes />"}
      </Hole>
      <StartHoleTime>{ts(h["time_start"])}</StartHoleTime>
      <EndHoleTime>{ts(h["time_end"])}</EndHoleTime>
      <AutoMode>
        <PosMode />
        <AnchoringMode />
        <CollaringMode />
        <DrillMode>0</DrillMode>
        <ActivationMode />
      </AutoMode>
      <Hstatus>{"OK" if completed else "Fail"}</Hstatus>
    </HoleQualityData>
  </DrillQuality>
"""
    return finish(body, created, "DRPQual")

def mwd_xml(design, h, channels, samples, sysver, created):
    params = []
    for i, ch in enumerate(channels):
        if i < NAMED_PARAMS and ch in UNITS:
            unit, full = UNITS[ch]
            params.append(f'      <Parameter Unit="{unit}" Full="{full}">{ch}</Parameter>')
        else:
            params.append("      <Parameter />")
    sample_lines = []
    for t, depth, vals in samples:
        v = " ".join(f"{x:.3f}" for x in vals[:NAMED_PARAMS]) + " "
        sample_lines.append(f'    <Samples TimeTag="{ts(t)}" DepthTag="{depth:.3f}">{v}</Samples>')
    body = f"""<?xml  version = "1.0" encoding="utf-8"?>
<DRMWD xmlns="http://www.iredes.org/xml/DrillRig" xmlns:IR="http://www.iredes.org/xml" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" schemaLocation="http://www.iredes.org/xml/DrillRig" IRVersion="V 1.0" IRDownwCompat="V 1.0" DRPPlanDownwCompat="V 1.0" DRPPlanVersion="V 1.0" DRMWDVersion="V 1.0">
{head(sysver, created)}  <IR:ReportId>{ts(h["time_end"])}</IR:ReportId>
  <IR:StartLogTime>{ts(h["time_start"])}</IR:StartLogTime>
  <IR:EndLogTime>{ts(h["time_end"])}</IR:EndLogTime>
  <IR:PlanIdRef>{escape(design)}</IR:PlanIdRef>
  <IR:QualityReportIdRef>{escape(design)}</IR:QualityReportIdRef>
  <MWDholeId>{escape(h["item_id"])}</MWDholeId>
  <SampleDistance>1.000000</SampleDistance>
  <CompactMWDdata>
    <MWDparams>
{chr(10).join(params)}
    </MWDparams>
{chr(10).join(sample_lines)}
  </CompactMWDdata>
"""
    return finish(body, created, "DRMWD")

def long_path(path):
    r"""On Windows, prefix with \\?\ so paths over 260 characters can be opened."""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p

def safe_relpath(path):
    try:
        return os.path.relpath(path)
    except ValueError:                     # different Windows drive
        return os.path.abspath(path)

def ask_date():
    today = datetime.date.today().strftime("%Y%m%d")
    if not sys.stdin.isatty():
        return today
    while True:
        v = input(f"Folder name (yyyymmdd) [{today}]: ").strip() or today
        try:
            datetime.datetime.strptime(v, "%Y%m%d"); return v
        except ValueError:
            print("  Please enter a date as yyyymmdd, e.g. 20261027")

def safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name)

def process_db(path, a, stats):
    design = infer_design_name(path)
    if a.design_filter and a.design_filter.lower() not in design.lower():
        return
    tmpdir = None
    open_path = path
    if os.name == "nt" and len(os.path.abspath(path)) >= 250:
        # SQLite can't open over-long Windows paths; read a short temporary copy instead
        tmpdir = tempfile.mkdtemp()
        open_path = os.path.join(tmpdir, "src.quality.db")
        shutil.copyfile(long_path(path), open_path)
    try:
        _process_db(path, open_path, design, a, stats)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

def _process_db(path, open_path, design, a, stats):
    con = sqlite3.connect(f"file:{open_path}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    cur = con.cursor()
    tables = {r[0] for r in cur.execute("select name from sqlite_master where type='table'")}
    if "Hole" not in tables:
        return
    q, p = "select * from Hole", []
    conds = []
    if a.since: conds.append("time_end >= ?"); p.append(a.since)
    if a.hole: conds.append(f"item_id in ({','.join('?'*len(a.hole))})"); p += a.hole
    if conds: q += " where " + " and ".join(conds)
    holes = [dict(r) for r in cur.execute(q + " order by time_start", p)]
    if not holes:
        print(f"{design:<20} 0 holes (after filters)  {safe_relpath(path)}"); return

    chan_by_item = {r[0]: r[1].split(",") for r in cur.execute("select item_id, columns from Hole_DepthRecording")} if "Hole_DepthRecording" in tables else {}
    recs, vals = {}, {}
    if {"Hole_DepthRecord", "Hole_DepthRecordValue"} <= tables:
        for rid, owner, t, d in cur.execute("select id, owner_id, time, depth from Hole_DepthRecord order by owner_id, id"):
            recs.setdefault(owner, []).append((rid, t, d))
        for rid, v in cur.execute("select depthRecord_id, value from Hole_DepthRecordValue order by rowid"):
            vals.setdefault(rid, []).append(v)
    con.close()

    outdir = a.out if a.flat else os.path.join(a.out, safe(design))
    os.makedirs(long_path(outdir), exist_ok=True)
    created = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    n_new = n_skip = 0
    for h in holes:
        hid = safe(h["item_id"])
        f_q = os.path.join(outdir, f"{hid}.quality.xml")
        f_m = os.path.join(outdir, f"{safe(design)}.quality.{hid}.mwd.xml")
        if not a.overwrite and os.path.exists(long_path(f_q)) and os.path.exists(long_path(f_m)):
            n_skip += 1; continue
        channels = chan_by_item.get(h["item_id"], ["PR","DP","PP","FP","RP","FLP","FP_FORWARD","FP_BACK"])
        samples = [(t, d, vals.get(rid, [])) for rid, t, d in recs.get(h["id"], [])]
        with open(long_path(f_q), "w", encoding="utf-8", newline="\n") as f: f.write(qual_xml(design, h, a.sysver, created))
        with open(long_path(f_m), "w", encoding="utf-8", newline="\n") as f: f.write(mwd_xml(design, h, channels, samples, a.sysver, created))
        n_new += 1
    stats["written"] += n_new; stats["skipped"] += n_skip
    print(f"{design:<20} {len(holes):>5} holes  wrote {n_new:>5}  skipped {n_skip:>5}  -> {outdir}")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="folder(s) to scan or .quality.db file(s)")
    ap.add_argument("--master", default=DEFAULT_MASTER, help="master folder; output goes to <master>/<yyyymmdd>/iredes/")
    ap.add_argument("--date", help="yyyymmdd folder name (prompted for if omitted)")
    ap.add_argument("-o", "--out", help="write here instead of <master>/<yyyymmdd>/iredes/")
    ap.add_argument("--design-filter"); ap.add_argument("--hole", nargs="*", help="only these HoleIds")
    ap.add_argument("--since", help="only holes with time_end >= ISO timestamp")
    ap.add_argument("--flat", action="store_true"); ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--sysver", default="2.0.23201.521", help="EqpSysVer to stamp in the header")
    a = ap.parse_args()
    if a.out and a.date:
        ap.error("--date only applies when -o is not given")
    dbs = []
    for p in a.inputs:
        dbs += [p] if os.path.isfile(p) else glob.glob(os.path.join(p, "**", "*.quality.db"), recursive=True)
    if not dbs: sys.exit("No *.quality.db files found")
    if not a.out:
        date = a.date or ask_date()
        try:
            datetime.datetime.strptime(date, "%Y%m%d")
        except ValueError:
            ap.error(f"--date must be yyyymmdd, got {date!r}")
        a.out = os.path.join(a.master, date, "iredes")
    print(f"Output: {a.out}")
    stats = {"written": 0, "skipped": 0}
    for p in sorted(set(dbs)):
        process_db(p, a, stats)
    print(f"Done: {stats['written']} holes written ({stats['written']*2} files), {stats['skipped']} skipped (already present)")

if __name__ == "__main__":
    main()
