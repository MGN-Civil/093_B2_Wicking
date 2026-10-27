#!/usr/bin/env python3
r"""
wick_copy_xml.py - Copy a day's IREDES XML files into the XML delivery folders.

Takes the files wick_process.py wrote for a date:
    <root>\<yyyymmdd>\outputs\iredes\<design>\*.xml
and copies them (flat, without the design subfolder) to:
    <design>.quality.<HoleId>.mwd.xml  -> <xml>\MWD\
    <HoleId>.quality.xml               -> <xml>\XML\

Files are handled per hole, as a pair: a hole's quality and MWD files are copied together
or not at all, so the delivery folders never get one without the other.

  * Source orphans: a hole with only one of the two files in the day's output is reported
    and not copied.
  * Already delivered: a hole whose two files are both in the folders' _archive (searched
    including subfolders, case-insensitive) is skipped. If only one of the two is archived
    it is reported as partly archived and not copied.
  * Already in the delivery folder: an identical file is left alone; a file that differs is
    reported and not replaced unless --overwrite is given.
  * Name clash: the same file name from two designs is reported and not copied.
  * Finally the MWD and XML delivery folders are compared by HoleId and any orphan in
    either folder is reported.

Usage
  python wick_copy_xml.py                         # prompts for the date (Enter = today)
  python wick_copy_xml.py --date 20261027
  python wick_copy_xml.py --date 20261027 --dry-run   # show what would happen, copy nothing

Every run is appended to <root>\<yyyymmdd>\wick_copy_xml.log.
"""
import argparse, datetime, filecmp, glob, os, shutil, sys, time, traceback

DATA = r"C:\Users\GregLauer\OneDrive - mgncivil.com.au\Projects\093 B2 Paddock\Wicking\Data"
DEFAULT_ROOT = os.path.join(DATA, "_from_Machine")
DEFAULT_XML = os.path.join(DATA, "XML")
KINDS = {"MWD": ".mwd.xml", "XML": ".quality.xml"}   # delivery subfolder -> file suffix


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


def hole_id(name):
    """HoleId from a file name, or None if it isn't an IREDES quality/MWD file.
    'B1 WICK D v3.quality.3B2740.mwd.xml' -> '3B2740';  '3B2740.quality.xml' -> '3B2740'"""
    low = name.lower()
    if low.endswith(".mwd.xml") and ".quality." in low:
        return name[low.rindex(".quality.") + len(".quality."):-len(".mwd.xml")]
    if low.endswith(".quality.xml"):
        return name[:-len(".quality.xml")]
    return None


def kind_of(name):
    low = name.lower()
    if low.endswith(".mwd.xml"): return "MWD"
    if low.endswith(".quality.xml"): return "XML"
    return None


def valid_date(v):
    try:
        datetime.datetime.strptime(v, "%Y%m%d"); return True
    except ValueError:
        return False


def iredes_dir(root, date):
    return os.path.join(root, date, "outputs", "iredes")


def ask_date(root):
    today = datetime.date.today().strftime("%Y%m%d")
    if not sys.stdin.isatty():
        return today
    while True:
        v = input(f"Folder name (yyyymmdd) [{today}]: ").strip() or today
        if not valid_date(v):
            print("  Please enter a date as yyyymmdd, e.g. 20261027"); continue
        if not os.path.isdir(iredes_dir(root, v)):
            print(f"  No IREDES output for {v}: {iredes_dir(root, v)} not found (run run_wick.bat first)")
            continue
        return v


def names_in(folder, recursive):
    """Lower-cased file names in folder (optionally including subfolders)."""
    if not os.path.isdir(folder):
        return set()
    if recursive:
        return {f.lower() for _, _, files in os.walk(folder) for f in files}
    return {f.lower() for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))}


def listing(log, title, items, limit=50):
    """Log a heading and up to `limit` items."""
    items = sorted(items)
    log.print(f"  {title}: {len(items)}")
    for i in items[:limit]:
        log.print(f"    {i}")
    if len(items) > limit:
        log.print(f"    ... and {len(items) - limit} more")


def main(log):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="yyyymmdd (prompted for if omitted)")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="_from_Machine folder")
    ap.add_argument("--xml", default=DEFAULT_XML, help="XML delivery folder holding MWD\\ and XML\\")
    ap.add_argument("--dry-run", action="store_true", help="report what would be copied; copy nothing")
    ap.add_argument("--overwrite", action="store_true", help="replace files already in the delivery folder that differ")
    a = ap.parse_args()

    date = a.date or ask_date(a.root)
    if not valid_date(date):
        ap.error(f"--date must be yyyymmdd, got {date!r}")
    log["dir"] = os.path.join(a.root, date)
    src = iredes_dir(a.root, date)
    if not os.path.isdir(src):
        sys.exit(f"No IREDES output for {date}: {src} not found (run run_wick.bat first)")
    dest = {k: os.path.join(a.xml, k) for k in KINDS}
    for k, d in dest.items():
        if not os.path.isdir(d):
            sys.exit(f"Delivery folder not found: {d}")

    log.print(f"Date     : {date}{'   (DRY RUN - nothing will be copied)' if a.dry_run else ''}")
    log.print(f"From     : {src}")
    for k in KINDS:
        log.print(f"To {k:<5} : {dest[k]}")
        if not os.path.isdir(os.path.join(dest[k], "_archive")):
            log.print(f"  WARNING: no _archive folder in {dest[k]}; nothing will count as already delivered")
    archived = {k: names_in(os.path.join(dest[k], "_archive"), recursive=True) for k in KINDS}
    present = {k: names_in(dest[k], recursive=False) for k in KINDS}

    # ---- pair up the day's files: (design, HoleId) -> {"MWD": path, "XML": path}
    pairs, other = {}, []
    for f in sorted(glob.glob(os.path.join(glob.escape(src), "**", "*.xml"), recursive=True)):
        name, k = os.path.basename(f), kind_of(os.path.basename(f))
        hid = hole_id(name)
        if not k or hid is None:
            other.append(name); continue
        design = os.path.basename(os.path.dirname(f))
        pairs.setdefault((design, hid.lower()), {})[k] = f

    # same delivery file name from two designs would overwrite each other in the flat folders
    owners = {}
    for key, p in pairs.items():
        for k, f in p.items():
            owners.setdefault((k, os.path.basename(f).lower()), set()).add(key)
    clash_keys = {key for keys in owners.values() if len(keys) > 1 for key in keys}

    log.print(f"\nSource: {len(pairs)} holes in {len({d for d, _ in pairs})} design(s)")
    src_orphans, partly, clashes, done_before, to_copy = [], [], [], [], []
    for key in sorted(pairs):
        p = pairs[key]
        label = f"{key[0]} / {hole_id(os.path.basename(next(iter(p.values()))))}"
        if len(p) < 2:
            have = next(iter(p))
            src_orphans.append(f"{label}: only {KINDS[have]} ({os.path.basename(p[have])}), no {KINDS['MWD' if have == 'XML' else 'XML']}")
            continue
        if key in clash_keys:
            clashes.append(f"{label}: file name also produced by another design"); continue
        arch = {k: os.path.basename(p[k]).lower() in archived[k] for k in KINDS}
        if all(arch.values()):
            done_before.append(label); continue
        if any(arch.values()):
            partly.append(f"{label}: " + ", ".join(f"{KINDS[k]} {'in' if arch[k] else 'NOT in'} _archive" for k in KINDS))
            continue
        to_copy.append((label, p))

    # ---- copy complete pairs
    c = {"new": 0, "replaced": 0, "same": 0, "differs": 0}
    copied_holes, differs = [], []
    # delivery folder contents after this run: lower-cased name -> name as it appears on disk
    will_have = {k: {f.lower(): f for f in os.listdir(dest[k]) if os.path.isfile(os.path.join(dest[k], f))} for k in KINDS}
    for label, p in to_copy:
        acted = False
        for k in KINDS:
            f = p[k]; name = os.path.basename(f); target = os.path.join(dest[k], name)
            if name.lower() in present[k]:
                if filecmp.cmp(long_path(f), long_path(target), shallow=False):
                    c["same"] += 1; continue
                if not a.overwrite:
                    c["differs"] += 1; differs.append(f"{label}: {name}"); continue
                c["replaced"] += 1
            else:
                c["new"] += 1
            if not a.dry_run:
                shutil.copy2(long_path(f), long_path(target))
            will_have[k][name.lower()] = name; acted = True
        if acted:
            copied_holes.append(label)

    verb = "would copy" if a.dry_run else "copied"
    log.print(f"  {verb}: {len(copied_holes)} holes ({c['new']} new files"
              + (f", {c['replaced']} replaced" if c["replaced"] else "") + ")")
    log.print(f"  skipped, already in _archive: {len(done_before)} holes")
    if c["same"]:
        log.print(f"  skipped, identical file already in delivery folder: {c['same']} files")
    if a.dry_run and copied_holes:
        listing(log, "holes that would be copied", copied_holes)
    if src_orphans: listing(log, "NOT COPIED - orphan in source (only one of the pair)", src_orphans)
    if partly:      listing(log, "NOT COPIED - only one of the pair is in _archive", partly)
    if clashes:     listing(log, "NOT COPIED - same file name in two designs", clashes)
    if differs:     listing(log, "NOT REPLACED - differs from the file already in the delivery folder (use --overwrite)", differs)
    if other:       listing(log, "Ignored - not a quality/MWD file name", other)

    # ---- orphan check across the delivery folders (as they are now, or would be after copying)
    ids = {k: {} for k in KINDS}
    for k in KINDS:
        for n in will_have[k].values():
            hid = hole_id(n)
            if hid is not None and kind_of(n) == k:
                ids[k][hid.lower()] = n
    only = {k: sorted(set(ids[k]) - set(ids[o])) for k, o in (("MWD", "XML"), ("XML", "MWD"))}
    log.print(f"\nDelivery folders{' (after this run)' if a.dry_run else ''}: "
              f"MWD {len(ids['MWD'])} holes, XML {len(ids['XML'])} holes")
    if not only["MWD"] and not only["XML"]:
        log.print("  OK - every hole has both an MWD and a quality file")
    else:
        listing(log, "ORPHAN in MWD (no matching quality file in XML)", [ids["MWD"][h] for h in only["MWD"]])
        listing(log, "ORPHAN in XML (no matching MWD file in MWD)", [ids["XML"][h] for h in only["XML"]])

    problems = len(src_orphans) + len(partly) + len(clashes) + len(only["MWD"]) + len(only["XML"])
    log["summary"] = (f"{verb} {len(copied_holes)} holes, archived {len(done_before)}, "
                      f"source orphans {len(src_orphans)}, part-archived {len(partly)}, clashes {len(clashes)}, "
                      f"differs {len(differs)}, delivery orphans MWD {len(only['MWD'])} / XML {len(only['XML'])}")
    log.print(f"\n{'WARNING - ' + str(problems) + ' item(s) need checking, see above' if problems else 'No orphans.'}")


# ---------------------------------------------------------------------------- logging
class Log(dict):
    def __init__(self):
        super().__init__(dir=None, summary="", lines=[])
    def print(self, s=""):
        print(s); self["lines"].append(s)


def run():
    log, started, t0 = Log(), datetime.datetime.now(), time.time()
    status, code = "OK", 0
    try:
        main(log)
    except SystemExit as e:
        if e.code not in (None, 0):
            status, code = "FAILED", 1
            msg = e.code if isinstance(e.code, str) else f"exit {e.code}"
            if isinstance(e.code, str):
                print(msg)
            log["lines"].append(msg)
    except KeyboardInterrupt:
        status, code = "CANCELLED", 1
        print("Cancelled."); log["lines"].append("Cancelled.")
    except Exception:
        status, code = "FAILED", 1
        tb = traceback.format_exc(); print(tb, end=""); log["lines"].append(tb)
    if log["dir"] and os.path.isdir(log["dir"]):
        try:
            with open(os.path.join(log["dir"], "wick_copy_xml.log"), "a", encoding="utf-8") as f:
                f.write(f"===== {started:%Y-%m-%d %H:%M:%S}  {status}  ({time.time() - t0:.1f} s)  {' '.join(sys.argv[1:])}\n")
                f.write("\n".join(log["lines"]) + "\n" + (f"SUMMARY: {log['summary']}\n" if log["summary"] else "") + "\n")
        except OSError as e:
            print(f"(could not write log: {e})")
    sys.exit(code)


if __name__ == "__main__":
    run()
