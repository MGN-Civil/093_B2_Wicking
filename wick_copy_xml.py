#!/usr/bin/env python3
r"""
wick_copy_xml.py - Copy a day's IREDES XML files into the XML delivery folders.

Takes the files wick_process.py wrote for a date:
    <root>\<yyyymmdd>\outputs\iredes\<design>\*.xml
and copies them (flat, without the design subfolder) to:
    *.mwd.xml      -> <xml>\MWD\
    *.quality.xml  -> <xml>\XML\

A file is skipped when a file with the same name is already in that folder's _archive
(searched including subfolders), i.e. it has already been delivered. A file whose name is
already in the destination folder itself is left alone if identical and reported if it
differs; --overwrite replaces it.

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
TARGETS = [(".mwd.xml", "MWD"), (".quality.xml", "XML")]   # (file suffix, subfolder of --xml)


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


def main(log):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="yyyymmdd (prompted for if omitted)")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="_from_Machine folder")
    ap.add_argument("--xml", default=DEFAULT_XML, help="XML delivery folder holding MWD\\ and XML\\")
    ap.add_argument("--dry-run", action="store_true", help="report what would be copied; copy nothing")
    ap.add_argument("--overwrite", action="store_true", help="replace files already in the destination that differ")
    a = ap.parse_args()

    date = a.date or ask_date(a.root)
    if not valid_date(date):
        ap.error(f"--date must be yyyymmdd, got {date!r}")
    log["dir"] = os.path.join(a.root, date)
    src = iredes_dir(a.root, date)
    if not os.path.isdir(src):
        sys.exit(f"No IREDES output for {date}: {src} not found (run run_wick.bat first)")

    log.print(f"Date     : {date}{'   (DRY RUN - nothing will be copied)' if a.dry_run else ''}")
    log.print(f"From     : {src}")

    for _, sub in TARGETS:
        if not os.path.isdir(os.path.join(a.xml, sub)):
            sys.exit(f"Destination folder not found: {os.path.join(a.xml, sub)}")

    files = sorted(glob.glob(os.path.join(glob.escape(src), "**", "*.xml"), recursive=True))
    totals = {}
    for suffix, sub in TARGETS:
        dest = os.path.join(a.xml, sub)
        archive = os.path.join(dest, "_archive")
        log.print(f"\n{suffix:<13} -> {dest}")
        if not os.path.isdir(archive):
            log.print(f"  WARNING: no _archive folder at {archive}; nothing will be treated as already delivered")
        archived = names_in(archive, recursive=True)
        present = names_in(dest, recursive=False)

        mine = [f for f in files if f.lower().endswith(suffix)]
        # same file name from two designs would collide in the flat destination
        by_name = {}
        for f in mine:
            by_name.setdefault(os.path.basename(f).lower(), []).append(f)
        clashes = {n: fs for n, fs in by_name.items() if len(fs) > 1}

        c = {"copied": 0, "archived": 0, "same": 0, "differs": 0, "replaced": 0, "clash": 0}
        for f in mine:
            name = os.path.basename(f)
            key = name.lower()
            target = os.path.join(dest, name)
            if key in clashes:
                c["clash"] += 1; continue
            if key in archived:
                c["archived"] += 1; continue
            if key in present:
                if filecmp.cmp(long_path(f), long_path(target), shallow=False):
                    c["same"] += 1; continue
                if not a.overwrite:
                    c["differs"] += 1
                    log.print(f"  differs, not replaced: {name}")
                    continue
                c["replaced"] += 1
            else:
                c["copied"] += 1
            if a.dry_run:
                log.print(f"  would copy: {name}")
            else:
                shutil.copy2(long_path(f), long_path(target))
        for n, fs in sorted(clashes.items()):
            log.print(f"  NOT COPIED, same name in {len(fs)} designs: {os.path.basename(fs[0])} "
                      f"({', '.join(os.path.basename(os.path.dirname(x)) for x in fs)})")

        verb = "would copy" if a.dry_run else "copied"
        log.print(f"  {len(mine)} files: {verb} {c['copied']} new"
                  + (f", {'would replace' if a.dry_run else 'replaced'} {c['replaced']}" if c["replaced"] else "")
                  + f", skipped {c['archived']} already in _archive, {c['same']} already in folder"
                  + (f", {c['differs']} differ from the copy in the folder (use --overwrite)" if c["differs"] else "")
                  + (f", {c['clash']} name clashes" if c["clash"] else ""))
        totals[sub] = c
    log["summary"] = "  ".join(f"{sub}: {'would copy' if a.dry_run else 'copied'} {c['copied'] + c['replaced']}, "
                               f"archived {c['archived']}, present {c['same'] + c['differs']}" for sub, c in totals.items())


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
                f.write("\n".join(log["lines"]) + "\n" + (log["summary"] + "\n" if log["summary"] else "") + "\n")
        except OSError as e:
            print(f"(could not write log: {e})")
    sys.exit(code)


if __name__ == "__main__":
    run()
