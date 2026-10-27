#!/usr/bin/env python3
r"""
wick_process.py - Daily processing of Trimble Groundworks *.quality.db as-drilled data.

Asks for a date (yyyymmdd), finds that day's raw data under the _from_Machine folder,
and writes everything to one place:

  <root>\<yyyymmdd>\outputs\
      holes.csv, depth_log.csv, layers.csv, sources.csv
      wick_asdrilled.gpkg      holes (PointZ collar), hole_axes (LineStringZ collar->toe),
                               depth_log, layers, sources
      manifest.json            what was read, options used, counts
      wick_process.log         full output of every run for this date (appended)
      iredes\<design>\
          <HoleId>.quality.xml                DRPQual  (hole quality record)
          <design>.quality.<HoleId>.mwd.xml   DRMWD    (measure-while-drilling log)

Raw data for a date is every folder directly under <root> whose name contains the date,
e.g. "20261027" or "Export.20261027_111309". All matching folders are searched
recursively for *.quality.db and merged.

Usage
  python wick_process.py                       # prompts for the date (Enter = today)
  python wick_process.py --date 20261027       # no prompt
  python wick_process.py --date 20261027 --all-designs --srid 7856

Options
  --root           raw-data folder (default DEFAULT_ROOT below)
  --date           yyyymmdd; prompted for if omitted (unattended runs use today)
  --design-filter  only designs whose name contains this text (default WICK)
  --all-designs    process every design (ignores --design-filter)
  --since          only holes with time_end >= ISO timestamp, e.g. 2026-10-13
  --keep-zero-channels  keep depth-log channels that are zero throughout (dropped by default)
  --srid           EPSG code to tag the GeoPackage with (default -1 = undefined local grid)
  --sysver         EqpSysVer stamped in IREDES headers (default 2.0.23201.521)

Notes
  * Coordinates are copied as stored (the design's local site system).
  * A hole found in more than one database is written once, from the copy with the
    latest time_end. Identical database files are read once.
  * IREDES: Hstatus is "OK" when Hole.completed = 1, otherwise "Fail". ChkSum is a CRC32
    of the content before <IR:GenTrailer>, not Groundworks' own algorithm.
  * Re-running for the same date rewrites that date's outputs (the log is appended).
  * Every run, including failed ones, adds one summary line to <root>\wick_process.log.
"""
import argparse, csv, json, os, sqlite3, struct, sys, datetime, hashlib, glob, re, tempfile, shutil, zlib, traceback, time
from xml.sax.saxutils import escape

DEFAULT_ROOT = r"C:\Users\GregLauer\OneDrive - mgncivil.com.au\Projects\093 B2 Paddock\Wicking\Data\_from_Machine"

# ============================================================================ extract (CSV / GeoPackage)
HOLE_COLS = ["id","item_id","time_start","time_end","start_x","start_y","start_z",
             "end_x","end_y","end_z","rotation","operator","machine","hrms","vrms",
             "note","completed","diameter","blast","IsOverridden"]

# ----------------------------------------------------------------------------- read
def long_path(path):
    r"""On Windows, prefix with \\?\ so paths over 260 characters can be opened."""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):                      # UNC share
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p

def safe_relpath(path):
    """relpath, falling back to an absolute path when on a different Windows drive."""
    try:
        return os.path.relpath(path)
    except ValueError:
        return os.path.abspath(path)

def table_cols(cur, table):
    return [r[1] for r in cur.execute(f'PRAGMA table_info("{table}")')]

def read_db(path, since=None):
    """Return dict with holes, depth_log, layers lists of dicts, plus meta."""
    tmpdir = None
    open_path = path
    if os.name == "nt" and len(os.path.abspath(path)) >= 250:
        # SQLite can't open over-long Windows paths; read a short temporary copy instead
        tmpdir = tempfile.mkdtemp()
        open_path = os.path.join(tmpdir, "src.quality.db")
        shutil.copyfile(long_path(path), open_path)
    try:
        return _read_db(path, open_path, since)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

def _read_db(path, open_path, since):
    con = sqlite3.connect(f"file:{open_path}?mode=ro", uri=True)
    cur = con.cursor()
    tables = {r[0] for r in cur.execute("select name from sqlite_master where type='table'")}
    meta = dict(cur.execute("select key,value from General").fetchall()) if "General" in tables else {}
    design = infer_design_name(path)
    src = safe_relpath(path)
    out = {"holes": [], "depth_log": [], "layers": [], "meta": meta, "design": design, "source": src,
           "size": os.path.getsize(long_path(path)), "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(long_path(path))).isoformat(timespec="seconds")}
    if "Hole" not in tables:
        return out

    cols = table_cols(cur, "Hole")
    collist = ",".join('"%s"' % c for c in cols)
    q = f"select {collist} from Hole"
    params = ()
    if since:
        q += " where time_end >= ?"; params = (since,)
    q += " order by time_start"
    holes = [dict(zip(cols, r)) for r in cur.execute(q, params)]
    hole_ids = {h["id"] for h in holes}
    for h in holes:
        h["depth_m"] = round(h["start_z"] - h["end_z"], 3)
        dx, dy = h["end_x"]-h["start_x"], h["end_y"]-h["start_y"]
        h["plan_offset_m"] = round((dx*dx+dy*dy) ** 0.5, 3)
        try:
            t0 = datetime.datetime.fromisoformat(h["time_start"][:26]); t1 = datetime.datetime.fromisoformat(h["time_end"][:26])
            h["duration_s"] = round((t1-t0).total_seconds(), 1)
        except Exception:
            h["duration_s"] = None
        h["design"] = design; h["source_db"] = src
    out["holes"] = holes

    # depth log: columns string per hole, values in row order per record
    if {"Hole_DepthRecording","Hole_DepthRecord","Hole_DepthRecordValue"} <= tables:
        chan_by_item = {r[0]: r[1].split(",") for r in cur.execute("select item_id, columns from Hole_DepthRecording")}
        item_by_hole = {h["id"]: h["item_id"] for h in holes}
        recs = cur.execute("select id, owner_id, time, depth from Hole_DepthRecord order by owner_id, id").fetchall()
        vals = {}
        for rid, v in cur.execute("select depthRecord_id, value from Hole_DepthRecordValue order by rowid"):
            vals.setdefault(rid, []).append(v)
        for rid, owner, t, depth in recs:
            if owner not in hole_ids: continue
            item = item_by_hole.get(owner)
            chans = chan_by_item.get(item, [])
            for i, v in enumerate(vals.get(rid, [])):
                out["depth_log"].append({"design": design, "source_db": src, "item_id": item, "hole_id": owner, "record_id": rid,
                                         "time": t, "depth": round(depth, 3),
                                         "channel": chans[i] if i < len(chans) else f"ch{i}", "value": v})
    if "Layer" in tables:
        item_by_hole = {h["id"]: h["item_id"] for h in holes}
        for hid, name, z in cur.execute("select hole_id, name, z from Layer"):
            if hid in hole_ids:
                out["layers"].append({"design": design, "source_db": src, "item_id": item_by_hole.get(hid), "hole_id": hid, "name": name, "z": z})
    con.close()
    return out

def infer_design_name(path):
    # "B1 WICK A v3.B1 WICK A v3.985E0EDB.quality.db" -> "B1 WICK A v3"
    base = os.path.basename(path)
    m = re.match(r"^(.*?)\.\1\.[0-9A-Fa-f]{8}\.quality\.db$", base)
    if m: return m.group(1)
    return re.sub(r"\.quality\.db$", "", base).split(".")[0]

# ----------------------------------------------------------------------------- csv
def write_csv(rows, path, fieldnames=None):
    if not rows:
        open(path, "w").close(); return 0
    fieldnames = fieldnames or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    return len(rows)

# ----------------------------------------------------------------------------- gpkg
def gpkg_point_z(x, y, z, srid):
    hdr = b"GP" + bytes([0, 0b00000001]) + struct.pack("<i", srid)      # little-endian, no envelope
    wkb = struct.pack("<BIddd", 1, 1001, x, y, z)                         # PointZ
    return hdr + wkb

def gpkg_line_z(pts, srid):
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]; zs=[p[2] for p in pts]
    hdr = b"GP" + bytes([0, 0b00000101]) + struct.pack("<i", srid)      # envelope xyz (code 2 -> flags bits 0b0100 + endian)
    env = struct.pack("<6d", min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))
    wkb = struct.pack("<BII", 1, 1002, len(pts)) + b"".join(struct.pack("<ddd", *p) for p in pts)
    return hdr + env + wkb

def srs_wkt(srid):
    if srid in (-1, 0): return "undefined"
    try:
        from pyproj import CRS
        return CRS.from_epsg(srid).to_wkt()
    except Exception:
        return f'PROJCS["EPSG:{srid}"]'  # placeholder; QGIS/GDAL resolve by organization_coordsys_id

def write_gpkg(path, holes, depth_log, layers, sources, srid):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmpdir = tempfile.mkdtemp(); tmp = os.path.join(tmpdir, "out.gpkg")
    try:
        _write_gpkg(tmp, holes, depth_log, layers, sources, srid)
        shutil.copyfile(tmp, path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def _write_gpkg(path, holes, depth_log, layers, sources, srid):
    con = sqlite3.connect(path); cur = con.cursor()
    cur.execute("PRAGMA application_id = 0x47504B47"); cur.execute("PRAGMA user_version = 10300")
    cur.executescript("""
    CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL,
      organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
    CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
      description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
      min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
      CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
    CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
      srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
      CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
      CONSTRAINT uk_gc_table_name UNIQUE (table_name),
      CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
      CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id));
    """)
    cur.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", [
        ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
        ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
        ("WGS 84 geodetic", 4326, "EPSG", 4326, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]', "WGS 84"),
    ])
    if srid not in (-1, 0, 4326):
        cur.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", (f"EPSG:{srid}", srid, "EPSG", srid, srs_wkt(srid), None))

    attr_cols = [c for c in HOLE_COLS if c != "id"] + ["depth_m","plan_offset_m","duration_s","design","source_db"]
    def sqltype(c):
        if c in ("completed","blast","IsOverridden"): return "INTEGER"
        if c in ("item_id","time_start","time_end","operator","machine","note","design","source_db"): return "TEXT"
        return "REAL"
    coldefs = ", ".join(f'"{c}" {sqltype(c)}' for c in attr_cols)

    # holes (PointZ)
    cur.execute(f'CREATE TABLE holes (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom BLOB, {coldefs})')
    cur.executemany(f'INSERT INTO holes (geom, {",".join(chr(34)+c+chr(34) for c in attr_cols)}) VALUES (?{",?"*len(attr_cols)})',
        [(gpkg_point_z(h["start_x"], h["start_y"], h["start_z"], srid), *[h.get(c) for c in attr_cols]) for h in holes])
    # hole_axes (LineStringZ)
    cur.execute(f'CREATE TABLE hole_axes (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom BLOB, {coldefs})')
    cur.executemany(f'INSERT INTO hole_axes (geom, {",".join(chr(34)+c+chr(34) for c in attr_cols)}) VALUES (?{",?"*len(attr_cols)})',
        [(gpkg_line_z([(h["start_x"],h["start_y"],h["start_z"]),(h["end_x"],h["end_y"],h["end_z"])], srid), *[h.get(c) for c in attr_cols]) for h in holes])

    # non-spatial
    cur.execute("CREATE TABLE depth_log (fid INTEGER PRIMARY KEY AUTOINCREMENT, design TEXT, item_id TEXT, hole_id INTEGER, record_id INTEGER, time TEXT, depth REAL, channel TEXT, value REAL)")
    cur.executemany("INSERT INTO depth_log (design,item_id,hole_id,record_id,time,depth,channel,value) VALUES (?,?,?,?,?,?,?,?)",
        [(d["design"],d["item_id"],d["hole_id"],d["record_id"],d["time"],d["depth"],d["channel"],d["value"]) for d in depth_log])
    cur.execute("CREATE INDEX depth_log_item ON depth_log(design,item_id)")
    cur.execute("CREATE TABLE layers (fid INTEGER PRIMARY KEY AUTOINCREMENT, design TEXT, item_id TEXT, hole_id INTEGER, name TEXT, z REAL)")
    cur.executemany("INSERT INTO layers (design,item_id,hole_id,name,z) VALUES (?,?,?,?,?)",
        [(l["design"],l["item_id"],l["hole_id"],l["name"],l["z"]) for l in layers])
    cur.execute("CREATE TABLE sources (fid INTEGER PRIMARY KEY AUTOINCREMENT, source_db TEXT, design TEXT, creator TEXT, schema_version TEXT, size_bytes INTEGER, modified TEXT, holes INTEGER, depth_records INTEGER, md5 TEXT)")
    cur.executemany("INSERT INTO sources (source_db,design,creator,schema_version,size_bytes,modified,holes,depth_records,md5) VALUES (?,?,?,?,?,?,?,?,?)",
        [(s["source_db"],s["design"],s["creator"],s["schema_version"],s["size_bytes"],s["modified"],s["holes"],s["depth_records"],s["md5"]) for s in sources])

    xs=[h["start_x"] for h in holes]+[h["end_x"] for h in holes]; ys=[h["start_y"] for h in holes]+[h["end_y"] for h in holes]
    ext = (min(xs),min(ys),max(xs),max(ys)) if xs else (None,)*4
    cur.executemany("INSERT INTO gpkg_contents (table_name,data_type,identifier,description,min_x,min_y,max_x,max_y,srs_id) VALUES (?,?,?,?,?,?,?,?,?)", [
        ("holes","features","holes","As-drilled holes, collar point", *ext, srid),
        ("hole_axes","features","hole_axes","As-drilled hole axis collar->toe", *ext, srid),
        ("depth_log","attributes","depth_log","Per-depth channel log (long format)", None,None,None,None, None),
        ("layers","attributes","layers","Strata picks", None,None,None,None, None),
        ("sources","attributes","sources","Source quality.db files", None,None,None,None, None),
    ])
    cur.executemany("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)", [
        ("holes","geom","POINT",srid,1,0), ("hole_axes","geom","LINESTRING",srid,1,0)])
    con.commit(); con.close()

# ----------------------------------------------------------------------------- main
def md5(path):
    h = hashlib.md5()
    with open(long_path(path),"rb") as f:
        for chunk in iter(lambda: f.read(1<<20), b""): h.update(chunk)
    return h.hexdigest()

# ============================================================================ IREDES XML
UNITS = {"PR": ("m/min", "PenetrRate"), "DP": ("Bar", "DampPressure"), "PP": ("Bar", "PercPressure"),
         "FP": ("Bar", "FeedPressure"), "RP": ("Bar", "RotPressure"), "FLP": ("Bar", "FlushPressure"),
         "FP_FORWARD": ("Bar", "FeedPressureFwd"), "FP_BACK": ("Bar", "FeedPressureBack")}
NAMED_PARAMS = 6   # Groundworks names the first six channels and leaves the rest as empty slots

def fmt(x, dp=3):
    """Number to fixed decimals; blank when the database holds NULL."""
    return "" if x is None else f"{x:.{dp}f}"

def ts(s):
    """'2026-10-13 09:30:18.2401552' -> '2026-10-13T09:30:18'"""
    return s[:19].replace(" ", "T")

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
    <HoleQualityData firstname="{escape(h["operator"] or "", {'"': '&quot;'})}" lastname="" machinename="{escape(h["machine"] or "", {'"': '&quot;'})}" interruptedItem="{not completed}" HRMS="{fmt(h["hrms"], 4)}" VRMS="{fmt(h["vrms"], 4)}" IsOverridden="{over}">
      <Hole>
        <HoleId>{escape(h["item_id"])}</HoleId>
        <HoleName />
        <StartPoint>
          <IR:PointX>{fmt(h["start_x"])}</IR:PointX>
          <IR:PointY>{fmt(h["start_y"])}</IR:PointY>
          <IR:PointZ>{fmt(h["start_z"])}</IR:PointZ>
        </StartPoint>
        <EndPoint>
          <IR:PointX>{fmt(h["end_x"])}</IR:PointX>
          <IR:PointY>{fmt(h["end_y"])}</IR:PointY>
          <IR:PointZ>{fmt(h["end_z"])}</IR:PointZ>
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
        v = " ".join(fmt(x) for x in vals[:NAMED_PARAMS]) + " "
        sample_lines.append(f'    <Samples TimeTag="{ts(t)}" DepthTag="{fmt(depth)}">{v}</Samples>')
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

def safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name)


# ============================================================================ IREDES writer
def write_iredes_db(path, design, holes_wanted, outdir, sysver, created):
    """Write IREDES files for the holes in holes_wanted (set of item_id) from one database."""
    tmpdir = None
    open_path = path
    if os.name == "nt" and len(os.path.abspath(path)) >= 250:
        tmpdir = tempfile.mkdtemp()
        open_path = os.path.join(tmpdir, "src.quality.db")
        shutil.copyfile(long_path(path), open_path)
    try:
        con = sqlite3.connect(f"file:{open_path}?mode=ro", uri=True); con.row_factory = sqlite3.Row
        cur = con.cursor()
        tables = {r[0] for r in cur.execute("select name from sqlite_master where type='table'")}
        if "Hole" not in tables:
            con.close(); return 0
        holes = [dict(r) for r in cur.execute("select * from Hole order by time_start") if r["item_id"] in holes_wanted]
        chan_by_item = {r[0]: r[1].split(",") for r in cur.execute("select item_id, columns from Hole_DepthRecording")} if "Hole_DepthRecording" in tables else {}
        recs, vals = {}, {}
        if {"Hole_DepthRecord", "Hole_DepthRecordValue"} <= tables:
            for rid, owner, t, d in cur.execute("select id, owner_id, time, depth from Hole_DepthRecord order by owner_id, id"):
                recs.setdefault(owner, []).append((rid, t, d))
            for rid, v in cur.execute("select depthRecord_id, value from Hole_DepthRecordValue order by rowid"):
                vals.setdefault(rid, []).append(v)
        con.close()
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    os.makedirs(long_path(outdir), exist_ok=True)
    for h in holes:
        hid = safe(h["item_id"])
        channels = chan_by_item.get(h["item_id"], ["PR","DP","PP","FP","RP","FLP","FP_FORWARD","FP_BACK"])
        samples = [(t, d, vals.get(rid, [])) for rid, t, d in recs.get(h["id"], [])]
        with open(long_path(os.path.join(outdir, f"{hid}.quality.xml")), "w", encoding="utf-8", newline="\n") as f:
            f.write(qual_xml(design, h, sysver, created))
        with open(long_path(os.path.join(outdir, f"{safe(design)}.quality.{hid}.mwd.xml")), "w", encoding="utf-8", newline="\n") as f:
            f.write(mwd_xml(design, h, channels, samples, sysver, created))
    return len(holes)

# ============================================================================ main
def valid_date(v):
    try:
        datetime.datetime.strptime(v, "%Y%m%d"); return True
    except ValueError:
        return False

def find_dbs(folder):
    """*.quality.db under folder, ignoring anything inside an outputs folder."""
    return [d for d in glob.glob(os.path.join(glob.escape(folder), "**", "*.quality.db"), recursive=True)
            if os.sep + "outputs" + os.sep not in d[len(folder):]]

def input_folders(root, date):
    """Folders directly under root whose name contains date and which hold at least one database."""
    return sorted(p for p in glob.glob(os.path.join(glob.escape(root), f"*{date}*"))
                  if os.path.isdir(p) and find_dbs(p))

def ask_date(root):
    today = datetime.date.today().strftime("%Y%m%d")
    if not sys.stdin.isatty():
        return today
    while True:
        v = input(f"Folder name (yyyymmdd) [{today}]: ").strip() or today
        if not valid_date(v):
            print("  Please enter a date as yyyymmdd, e.g. 20261027"); continue
        if not input_folders(root, v):
            print(f"  No folder containing '{v}' with *.quality.db files in {root}")
            recent = sorted({m.group(0) for p in os.listdir(root) for m in [re.search(r"20\d{6}", p)] if m and os.path.isdir(os.path.join(root, p)) and find_dbs(os.path.join(root, p))})[-5:]
            if recent: print(f"  Dates available: {', '.join(recent)}")
            continue
        return v

# ============================================================================ logging
class Tee:
    """Copy everything written to the console into a buffer for the log files."""
    def __init__(self, stream, buf):
        self.stream, self.buf = stream, buf
    def write(self, s):
        self.buf.append(s); return self.stream.write(s)
    def flush(self):
        self.stream.flush()
    def isatty(self):
        return self.stream.isatty()
    def __getattr__(self, name):
        return getattr(self.stream, name)

RUN = {"root": DEFAULT_ROOT, "date": None, "out_dir": None, "summary": ""}

def write_logs(buf, status, started, elapsed):
    text = "".join(buf)
    header = f"===== {started:%Y-%m-%d %H:%M:%S}  {status}  ({elapsed:.1f} s)  {' '.join(sys.argv[1:])}\n"
    if RUN["out_dir"] and os.path.isdir(RUN["out_dir"]):
        try:
            with open(os.path.join(RUN["out_dir"], "wick_process.log"), "a", encoding="utf-8") as f:
                f.write(header + text + ("" if text.endswith("\n") else "\n") + "\n")
        except OSError as e:
            print(f"(could not write run log: {e})", file=sys.__stdout__)
    if os.path.isdir(RUN["root"]):
        last_err = next((l.strip() for l in reversed(text.splitlines()) if l.strip()), "") if status != "OK" else ""
        line = f"{started:%Y-%m-%d %H:%M:%S}\t{status}\tdate={RUN['date'] or '-'}\t{RUN['summary'] or last_err}\t{getpass_user()}\n"
        try:
            with open(os.path.join(RUN["root"], "wick_process.log"), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            print(f"(could not write history log: {e})", file=sys.__stdout__)

def getpass_user():
    try:
        import getpass; return getpass.getuser()
    except Exception:
        return ""

def run():
    buf = []
    started, t0 = datetime.datetime.now(), time.time()
    sys.stdout, sys.stderr = Tee(sys.__stdout__, buf), Tee(sys.__stderr__, buf)
    status, code = "OK", 0
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            status, code = "FAILED", 1
            if isinstance(e.code, str):
                print(e.code)
            else:
                code = e.code
    except KeyboardInterrupt:
        status, code = "CANCELLED", 1
        print("Cancelled.")
    except Exception:
        status, code = "FAILED", 1
        traceback.print_exc()
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        write_logs(buf, status, started, time.time() - t0)
    sys.exit(code)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT, help="raw-data folder (default: _from_Machine)")
    ap.add_argument("--date", help="yyyymmdd (prompted for if omitted)")
    ap.add_argument("--design-filter", default="WICK", help="only designs whose name contains this text (default WICK)")
    ap.add_argument("--all-designs", action="store_true", help="process every design")
    ap.add_argument("--since", help="only holes with time_end >= this ISO timestamp")
    ap.add_argument("--keep-zero-channels", action="store_true", help="keep depth-log channels that are zero throughout")
    ap.add_argument("--srid", type=int, default=-1, help="EPSG code for the GeoPackage (default -1 undefined)")
    ap.add_argument("--sysver", default="2.0.23201.521", help="EqpSysVer stamped in IREDES headers")
    ap.add_argument("--name", default="wick_asdrilled", help="GeoPackage base name (default wick_asdrilled)")
    a = ap.parse_args()
    RUN["root"] = a.root
    if not os.path.isdir(a.root):
        sys.exit(f"Raw-data folder not found: {a.root}")

    date = a.date or ask_date(a.root)
    RUN["date"] = date
    if not valid_date(date):
        ap.error(f"--date must be yyyymmdd, got {date!r}")
    inputs = input_folders(a.root, date)
    if not inputs:
        sys.exit(f"No folder containing '{date}' with *.quality.db files in {a.root}")
    design_filter = None if a.all_designs else a.design_filter
    out_dir = os.path.join(a.root, date, "outputs")
    RUN["out_dir"] = out_dir
    print(f"Date     : {date}")
    ired_dir = os.path.join(out_dir, "iredes")
    print(f"Raw data : {', '.join(os.path.basename(p) for p in inputs)}")
    print(f"Designs  : {'all' if not design_filter else 'containing ' + repr(design_filter)}")
    print(f"Outputs  : {out_dir}\n")

    # find databases (skip anything inside an outputs folder)
    dbs = []
    for p in inputs:
        dbs += find_dbs(p)
    if design_filter:
        dbs = [p for p in dbs if design_filter.lower() in infer_design_name(p).lower()]
    seen, uniq = set(), []
    for p in sorted(dbs):
        k = md5(p)
        if (k, infer_design_name(p)) in seen: continue
        seen.add((k, infer_design_name(p))); uniq.append((p, k))
    if not uniq:
        sys.exit("No matching *.quality.db files found")

    # read
    holes, depth_log, layers, sources = [], [], [], []
    for p, k in uniq:
        r = read_db(p, a.since)
        holes += r["holes"]; depth_log += r["depth_log"]; layers += r["layers"]
        if r["holes"]:
            sources.append({"source_db": r["source"], "design": r["design"], "creator": r["meta"].get("creator"),
                            "schema_version": r["meta"].get("version"), "size_bytes": r["size"], "modified": r["mtime"],
                            "holes": len(r["holes"]), "depth_records": len({d["record_id"] for d in r["depth_log"]}), "md5": k})
        print(f"  {r['design']:<20} {len(r['holes']):>6} holes  {os.path.basename(p)}")

    # one copy per hole: keep the latest time_end, and only that copy's depth log / layers
    best = {}
    for h in holes:
        key = (h["design"], h["item_id"])
        if key not in best or h["time_end"] > best[key]["time_end"]: best[key] = h
    holes = sorted(best.values(), key=lambda h: (h["design"], h["time_start"]))
    keep = {(h["source_db"], h["id"]) for h in holes}
    src_of = {}
    for h in holes: src_of.setdefault(h["source_db"], set()).add(h["item_id"])
    depth_log = [d for d in depth_log if (d["source_db"], d["hole_id"]) in keep]
    layers = [l for l in layers if (l["source_db"], l["hole_id"]) in keep]
    if not a.keep_zero_channels:
        live = {d["channel"] for d in depth_log if d["value"]}
        depth_log = [d for d in depth_log if d["channel"] in live]

    if not holes:
        sys.exit("No holes found for that date and filter")
    os.makedirs(out_dir, exist_ok=True)

    # CSV + GeoPackage
    hole_fields = ["design","item_id","time_start","time_end","duration_s","start_x","start_y","start_z","end_x","end_y","end_z",
                   "depth_m","plan_offset_m","rotation","operator","machine","hrms","vrms","note","completed","diameter","blast","IsOverridden","source_db"]
    n = {"holes.csv": write_csv(holes, os.path.join(out_dir, "holes.csv"), hole_fields),
         "depth_log.csv": write_csv(depth_log, os.path.join(out_dir, "depth_log.csv"),
                                    ["design","item_id","hole_id","record_id","time","depth","channel","value"]),
         "layers.csv": write_csv(layers, os.path.join(out_dir, "layers.csv"), ["design","item_id","hole_id","name","z"]),
         "sources.csv": write_csv(sources, os.path.join(out_dir, "sources.csv"))}
    gpkg = os.path.join(out_dir, a.name if a.name.endswith(".gpkg") else a.name + ".gpkg")
    write_gpkg(gpkg, holes, depth_log, layers, sources, a.srid)

    # IREDES
    if os.path.isdir(ired_dir):
        shutil.rmtree(long_path(ired_dir))
    created = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    n_ired = 0
    for p, k in uniq:
        wanted = src_of.get(safe_relpath(p), set())
        if wanted:
            design = infer_design_name(p)
            n_ired += write_iredes_db(p, design, wanted, os.path.join(ired_dir, safe(design)), a.sysver, created)

    # manifest
    per_design = {}
    for h in holes: per_design[h["design"]] = per_design.get(h["design"], 0) + 1
    manifest = {
        "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "date": date, "inputs": [os.path.abspath(p) for p in inputs],
        "options": {"design_filter": design_filter, "since": a.since, "drop_zero_channels": not a.keep_zero_channels,
                    "srid": a.srid, "sysver": a.sysver},
        "holes_total": len(holes), "holes_per_design": dict(sorted(per_design.items())),
        "latest_time_end": max(h["time_end"] for h in holes),
        "rows": {"holes": len(holes), "depth_log": len(depth_log), "layers": len(layers), "sources": len(sources)},
        "outputs": dict(n, **{"gpkg": os.path.basename(gpkg), "iredes_holes": n_ired, "iredes_files": n_ired * 2}),
        "sources": sources,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n  {len(holes)} holes  ({', '.join(f'{k}: {v}' for k, v in sorted(per_design.items()))})")
    print(f"  CSV + {os.path.basename(gpkg)} + manifest.json -> {out_dir}")
    print(f"  IREDES {n_ired * 2} files -> {ired_dir}")
    print(f"  Log    -> {os.path.join(out_dir, 'wick_process.log')}")
    RUN["summary"] = f"{len(holes)} holes, {n_ired * 2} IREDES files, from {', '.join(os.path.basename(p) for p in inputs)}"

if __name__ == "__main__":
    run()
