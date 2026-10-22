#!/usr/bin/env python3
"""
wick_quality_extract.py - Extract Trimble Groundworks *.quality.db as-drilled data
to CSV and/or GeoPackage.

Walks one or more folders (e.g. a WorksManager export) and merges every
*.quality.db found. Pure standard library: no GDAL/geopandas needed.

Outputs
  CSV        <out>/holes.csv, <out>/depth_log.csv, <out>/layers.csv, <out>/sources.csv
  GeoPackage <out>.gpkg with layers:
               holes       PointZ   (collar) + all Hole attributes, toe coords, depth
               hole_axes   LineStringZ collar -> toe
               depth_log   non-spatial, long format (one row per channel value)
               layers      non-spatial strata picks
               sources     non-spatial, one row per source db

Usage
  python wick_quality_extract.py <folder> [<folder>...] -o <out_basename>
                                 [--csv] [--gpkg] [--srid 28356] [--design-filter "WICK"]
                                 [--skip-empty] [--since 2026-10-13]

If neither --csv nor --gpkg is given, both are written.
Coordinates are copied as stored (the design's local/site system unless the rig is
configured to grid). Use --srid to tag the GeoPackage with an EPSG code (default -1 =
undefined cartesian).
"""
import argparse, csv, os, sqlite3, struct, sys, datetime, hashlib, glob, re, tempfile, shutil

HOLE_COLS = ["id","item_id","time_start","time_end","start_x","start_y","start_z",
             "end_x","end_y","end_z","rotation","operator","machine","hrms","vrms",
             "note","completed","diameter","blast","IsOverridden"]

# ----------------------------------------------------------------------------- read
def table_cols(cur, table):
    return [r[1] for r in cur.execute(f'PRAGMA table_info("{table}")')]

def read_db(path, since=None):
    """Return dict with holes, depth_log, layers lists of dicts, plus meta."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = con.cursor()
    tables = {r[0] for r in cur.execute("select name from sqlite_master where type='table'")}
    meta = dict(cur.execute("select key,value from General").fetchall()) if "General" in tables else {}
    design = infer_design_name(path)
    src = os.path.relpath(path)
    out = {"holes": [], "depth_log": [], "layers": [], "meta": meta, "design": design, "source": src,
           "size": os.path.getsize(path), "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")}
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
                out["depth_log"].append({"design": design, "item_id": item, "hole_id": owner, "record_id": rid,
                                         "time": t, "depth": round(depth, 3),
                                         "channel": chans[i] if i < len(chans) else f"ch{i}", "value": v})
    if "Layer" in tables:
        item_by_hole = {h["id"]: h["item_id"] for h in holes}
        for hid, name, z in cur.execute("select hole_id, name, z from Layer"):
            if hid in hole_ids:
                out["layers"].append({"design": design, "item_id": item_by_hole.get(hid), "hole_id": hid, "name": name, "z": z})
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
    with open(path,"rb") as f:
        for chunk in iter(lambda: f.read(1<<20), b""): h.update(chunk)
    return h.hexdigest()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", help="folder(s) or .quality.db file(s) to scan")
    ap.add_argument("-o","--out", required=True, help="output basename (CSV dir / .gpkg path without extension)")
    ap.add_argument("--csv", action="store_true"); ap.add_argument("--gpkg", action="store_true")
    ap.add_argument("--srid", type=int, default=-1, help="EPSG code for GeoPackage (default -1 undefined)")
    ap.add_argument("--design-filter", help="only databases whose design name contains this text (case-insensitive)")
    ap.add_argument("--since", help="only holes with time_end >= this ISO timestamp, e.g. 2026-10-13")
    ap.add_argument("--skip-empty", action="store_true", help="omit empty databases from sources list")
    ap.add_argument("--drop-zero-channels", action="store_true", help="omit depth-log channels that are zero for every record (wick masts log only PP/PR)")
    a = ap.parse_args()
    if not a.csv and not a.gpkg: a.csv = a.gpkg = True

    dbs = []
    for p in a.folders:
        if os.path.isfile(p): dbs.append(p)
        else: dbs += glob.glob(os.path.join(p, "**", "*.quality.db"), recursive=True)
    # dedupe identical files (the same db often appears twice in an export)
    seen, uniq = {}, []
    for p in sorted(dbs):
        k = md5(p)
        if (k, infer_design_name(p)) in seen: continue
        seen[(k, infer_design_name(p))] = p; uniq.append((p, k))
    if a.design_filter:
        uniq = [(p,k) for p,k in uniq if a.design_filter.lower() in infer_design_name(p).lower()]
    if not uniq:
        sys.exit("No *.quality.db files found")

    holes, depth_log, layers, sources = [], [], [], []
    for p, k in uniq:
        r = read_db(p, a.since)
        holes += r["holes"]; depth_log += r["depth_log"]; layers += r["layers"]
        if r["holes"] or not a.skip_empty:
            sources.append({"source_db": r["source"], "design": r["design"], "creator": r["meta"].get("creator"),
                            "schema_version": r["meta"].get("version"), "size_bytes": r["size"], "modified": r["mtime"],
                            "holes": len(r["holes"]), "depth_records": len({d["record_id"] for d in r["depth_log"]}), "md5": k})
        print(f"{r['design']:<20} {len(r['holes']):>6} holes  {os.path.relpath(p)}")

    # de-dupe holes seen in more than one db (keep latest time_end)
    best = {}
    for h in holes:
        key = (h["design"], h["item_id"])
        if key not in best or h["time_end"] > best[key]["time_end"]: best[key] = h
    holes = sorted(best.values(), key=lambda h: (h["design"], h["time_start"]))

    if a.drop_zero_channels:
        live = {d["channel"] for d in depth_log if d["value"]}
        depth_log = [d for d in depth_log if d["channel"] in live]
        print(f"depth_log channels kept: {sorted(live)}")

    if a.csv:
        d = a.out if a.out.endswith(os.sep) else a.out + "_csv"
        os.makedirs(d, exist_ok=True)
        hole_fields = ["design","item_id","time_start","time_end","duration_s","start_x","start_y","start_z","end_x","end_y","end_z",
                       "depth_m","plan_offset_m","rotation","operator","machine","hrms","vrms","note","completed","diameter","blast","IsOverridden","source_db"]
        n1 = write_csv(holes, os.path.join(d,"holes.csv"), hole_fields)
        n2 = write_csv(depth_log, os.path.join(d,"depth_log.csv"))
        n3 = write_csv(layers, os.path.join(d,"layers.csv"), ["design","item_id","hole_id","name","z"])
        n4 = write_csv(sources, os.path.join(d,"sources.csv"))
        print(f"CSV  -> {d}  (holes {n1}, depth_log {n2}, layers {n3}, sources {n4})")
    if a.gpkg:
        g = a.out if a.out.endswith(".gpkg") else a.out + ".gpkg"
        write_gpkg(g, holes, depth_log, layers, sources, a.srid)
        print(f"GPKG -> {g}  (holes {len(holes)}, depth_log {len(depth_log)}, layers {len(layers)}, srid {a.srid})")

if __name__ == "__main__":
    main()
