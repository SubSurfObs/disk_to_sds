#!/usr/bin/env python3
"""EchoPro USB (PC-SUDS) -> miniSEED SDS adapter (disk_to_sds, 2nd adapter).

Mirrors the Gecko pull's shape but converts SUDS->miniSEED, per day, so the Mac
never holds the whole card:

  per day -> robust-copy that day's files to a small local scratch (verify size +
  retry, so a flaky USB read doesn't leave truncated files) -> sudspy convert +
  channel map (suds_convert) -> write STEIM2 SDS day-files to <sds_root> -> QC ->
  free the scratch.

Footprint stays ~one day (~130 MB), never the whole 64 GB. Designed to run
detached/overnight (near-zero CPU; the cost is the USB read).

Usage:
  echopro_usb_to_sds.py <card_root> <sds_root>
      [--station OUTU] [--network VW] [--registry PATH] [--inventory station.xml]
      [--min-date 2015-01-01] [--max-date YYYY-MM-DD] [--scratch DIR]
      [--no-copy] [--limit N]
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import signal
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import suds_convert as sc  # noqa: E402

DAY_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})$")
STA_RE = re.compile(r".*_([A-Za-z0-9]+)\.dmx(?:\.gz)?$")

_STOP = {"flag": False}


def _on_sigint(signum, frame):
    """First Ctrl-C: finish the current day, then exit cleanly. Second: hard stop."""
    _STOP["flag"] = True
    print("\n[stop requested -- finishing current day, then exiting; Ctrl-C again to force]",
          flush=True)
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def day_already_done(sds_root, net, sta, dd) -> bool:
    """True only if all 3 component day-files for this net.sta + date exist and are
    non-empty. Requiring 3 means a half-written day (e.g. after a hard kill) is
    re-done rather than skipped -- so resume never leaves a day incomplete."""
    yr, doy = dd.year, dd.timetuple().tm_yday
    pat = str(Path(sds_root) / f"{yr:04d}" / net / sta / "*.D"
              / f"{net}.{sta}.*.D.{yr:04d}.{doy:03d}")
    return sum(1 for f in glob.glob(pat) if os.path.getsize(f) > 0) >= 3


def discover_days(card_root, min_date, max_date):
    """Find YYYY/MM/DD day-dirs (EchoPro: .../cont*/YYYY/MM/DD), date-filtered."""
    days, skipped = [], []
    for p in sorted(Path(card_root).rglob("*")):
        if not p.is_dir():
            continue
        m = DAY_RE.search(str(p))
        if not m:
            continue
        try:
            dd = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            skipped.append((p, "invalid-date"))
            continue
        if dd < min_date or dd > max_date:
            skipped.append((p, dd.isoformat()))
            continue
        days.append((p, dd))
    return days, skipped


def station_from_files(day_dir):
    """EchoPro filename = YYYY-MM-DD_HHMM_SS_STA.dmx -> STA."""
    for f in day_dir.rglob("*.dmx"):
        m = STA_RE.match(f.name)
        if m:
            return m.group(1)
    return None


def robust_copy_day(day_dir, scratch, retries=2):
    """Copy a day's .dmx to scratch; verify size; retry. Returns (files, failures)."""
    out, failures = [], []
    for src in sorted(day_dir.rglob("*.dmx")):
        dst = scratch / src.name
        ok = False
        for _ in range(retries + 1):
            try:
                shutil.copyfile(src, dst)
                if dst.stat().st_size == src.stat().st_size and dst.stat().st_size > 0:
                    ok = True
                    break
            except OSError:
                pass
        (out if ok else failures).append(dst if ok else str(src))
    return out, failures


def main(argv):
    p = argparse.ArgumentParser(description="EchoPro USB PC-SUDS -> miniSEED SDS")
    p.add_argument("card_root", help="USB card root (or any dir containing .../YYYY/MM/DD)")
    p.add_argument("sds_root", help="output SDS root (staging mount or scratch)")
    p.add_argument("--station", default=None, help="override station (else from filenames)")
    p.add_argument("--network", default=None, help="override network (else from registry)")
    default_reg = str(Path(__file__).resolve().parents[2]
                      / "eqserver_2_seiscomp" / "metadata" / "station_registry.yaml")
    p.add_argument("--registry", default=default_reg)
    p.add_argument("--inventory", default=None, help="StationXML for channel-by-rate (optional)")
    p.add_argument("--min-date", default="2015-01-01")
    p.add_argument("--max-date", default=None)
    p.add_argument("--scratch", default="/tmp/echopro_scratch")
    p.add_argument("--no-copy", action="store_true", help="read straight from card (skip scratch)")
    p.add_argument("--reprocess", action="store_true",
                   help="re-do days even if already in the SDS (default: resume / skip done days)")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args(argv[1:])

    min_date = date.fromisoformat(args.min_date)
    max_date = date.fromisoformat(args.max_date) if args.max_date else date.today()

    inv = None
    if args.inventory:
        from obspy import read_inventory
        inv = read_inventory(args.inventory)

    days, skipped = discover_days(args.card_root, min_date, max_date)
    if skipped:
        print(f"Skipping {len(skipped)} out-of-window/invalid day-dir(s) "
              f"(e.g. {skipped[0][1]})")
    if args.limit:
        days = days[:args.limit]
    print(f"Found {len(days)} day(s) under {args.card_root}")
    print(f"SDS out: {args.sds_root} | scratch: {args.scratch} | "
          f"copy-local: {not args.no_copy} | inventory: {bool(inv)}\n")

    signal.signal(signal.SIGINT, _on_sigint)
    grand_tr = grand_bad = n_skip = 0
    flagged = []
    t0 = time.time()
    for day_dir, dd in days:
        if _STOP["flag"]:
            print(f"  -- stop requested; exiting cleanly before {dd} (re-run to resume) --")
            break
        sta = args.station or station_from_files(day_dir)
        if not sta:
            print(f"  {dd}  SKIP: no station in filenames")
            continue
        net = args.network or sc.network_for_station(sta, args.registry)
        if not net:
            print(f"  {dd}  SKIP: no network for {sta} (not in registry; pass --network)")
            continue
        if not args.reprocess and day_already_done(args.sds_root, net, sta, dd):
            n_skip += 1
            print(f"  {dd}  {net}.{sta}  already done -> skip")
            continue

        if args.no_copy:
            files, copy_fail = sorted(day_dir.rglob("*.dmx")), []
        else:
            sdir = Path(args.scratch)
            if sdir.exists():
                shutil.rmtree(sdir)
            sdir.mkdir(parents=True)
            files, copy_fail = robust_copy_day(day_dir, sdir)

        st, qc = sc.convert_suds_files(files, network=net, station=sta, inv=inv)
        written = sc.write_sds(st, args.sds_root)
        bad = len(qc["read_errors"]) + len(copy_fail)
        grand_tr += qc["n_traces"]
        grand_bad += bad
        tag = ""
        if bad or qc["dropped_components"]:
            flagged.append((dd.isoformat(), bad, qc["dropped_components"]))
            tag = f"  [QC: {bad} bad files, dropped={qc['dropped_components'] or '-'}]"
        print(f"  {dd}  {net}.{sta} {qc['rate_hz']}Hz -> {qc['n_traces']} traces, "
              f"{len(written)} SDS files{tag}")

        if not args.no_copy:
            shutil.rmtree(args.scratch, ignore_errors=True)

    dt = time.time() - t0
    print(f"\nTOTAL: {len(days)} day(s): {n_skip} already-done/skipped, "
          f"{grand_tr} traces converted, {grand_bad} bad files, {dt:.1f}s")
    if flagged:
        print(f"QC-flagged day(s) ({len(flagged)}): "
              + ", ".join(f"{d}({n})" for d, n, _ in flagged[:25]))
    return 1 if grand_bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
