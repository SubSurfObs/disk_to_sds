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
import errno
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import suds_convert as sc  # noqa: E402
from staging_buffer import BufferedStaging  # noqa: E402

DAY_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})$")
STA_RE = re.compile(r".*_([A-Za-z0-9]+)\.dmx(?:\.gz)?$")

_STOP = {"flag": False}


def _on_sigint(signum, frame):
    """First Ctrl-C: finish the current day, then exit cleanly. Second: hard stop."""
    _STOP["flag"] = True
    print("\n[stop requested -- finishing current day, then exiting; Ctrl-C again to force]",
          flush=True)
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def day_already_done_in(root, net, sta, dd) -> int:
    """Count non-empty component day-files for this net.sta + date under <root>.
    Used to decide skip (>=3) AND to merge buffer + remote views."""
    yr, doy = dd.year, dd.timetuple().tm_yday
    pat = str(Path(root) / f"{yr:04d}" / net / sta / "*.D"
              / f"{net}.{sta}.*.D.{yr:04d}.{doy:03d}")
    try:
        return sum(1 for f in glob.glob(pat) if os.path.getsize(f) > 0)
    except OSError:
        return 0


def day_already_done(buf: BufferedStaging, net, sta, dd) -> bool:
    """A day is 'done' if buffer + remote together have all 3 channel files
    (so resume covers the case where a day is mid-drain at restart)."""
    return (day_already_done_in(buf.write_root, net, sta, dd)
            + day_already_done_in(buf.remote, net, sta, dd)) >= 3


def discover_days(card_root, min_date, max_date, rings=("cont0",)):
    """Find day-dirs under the known EchoPro layout: <card>/<ring>/YYYY/MM/DD.

    We DON'T rglob -- the card has ~3M files (98 days * 24h * ~1440 .dmx) and
    walking them all takes many minutes on a slow USB. Instead we iterdir at
    exactly 4 levels (<ring>/Y/M/D) which is O(~100) stat calls.

    ONLY the rings in `rings` are walked (default cont0 = the continuous
    seismometer). EchoPro is a 6-ch recorder with arbitrarily-named cont<N>
    processor rings; non-cont0 rings are accelerometer / triggered-event /
    junk data we do NOT ingest (triggers are re-derivable STA/LTA). See memory
    echopro-rings-cont0-only."""
    days, skipped = [], []
    base = Path(card_root)
    want = set(rings)
    for cont in sorted(p for p in base.iterdir()
                       if p.is_dir() and p.name in want):
        for y in sorted(p for p in cont.iterdir()
                        if p.is_dir() and p.name.isdigit()):
            for mo in sorted(p for p in y.iterdir()
                             if p.is_dir() and p.name.isdigit()):
                for d in sorted(p for p in mo.iterdir()
                                if p.is_dir() and p.name.isdigit()):
                    try:
                        dd = date(int(y.name), int(mo.name), int(d.name))
                    except ValueError:
                        skipped.append((d, "invalid-date"))
                        continue
                    if dd < min_date or dd > max_date:
                        skipped.append((d, dd.isoformat()))
                        continue
                    days.append((d, dd))
    return days, skipped


def station_from_files(day_dir):
    """EchoPro filename = YYYY-MM-DD_HHMM_SS_STA.dmx -> STA. Tolerates corrupt
    USB dir entries (os.walk skips a bad scandir instead of raising)."""
    for root, _dirs, names in os.walk(str(day_dir), onerror=lambda e: None):
        for nm in names:
            if nm.endswith(".dmx"):
                m = STA_RE.match(nm)
                if m:
                    return m.group(1)
    return None


def _walk_dmx_tolerant(day_dir):
    """Yield *.dmx paths under day_dir, tolerating corrupt FAT/exFAT directory
    entries (dangling names with no valid inode -- common on flaky USB sticks
    left by an unclean unmount). os.walk(onerror=...) keeps going past a
    scandir failure instead of letting the exception abort the whole run, the
    way Path.rglob() does. Returns (paths, walk_errors)."""
    out, walk_errors = [], []
    def _onerr(err):
        walk_errors.append(f"{getattr(err, 'filename', '?')}: {err}")
    for root, _dirs, names in os.walk(str(day_dir), onerror=_onerr):
        for nm in names:
            if nm.endswith(".dmx"):
                out.append(Path(root) / nm)
    return sorted(out), walk_errors


def robust_copy_day(day_dir, scratch, retries=2):
    """Copy a day's .dmx to scratch; verify size; retry. Returns (files, failures).

    The directory WALK itself is fault-tolerant: a corrupt dir entry on the USB
    is recorded as a failure and skipped, not allowed to crash the whole run.
    Per-file copies that error (transient USB read) are retried then recorded."""
    out, failures = [], []
    paths, walk_errors = _walk_dmx_tolerant(day_dir)
    failures.extend(f"<walk> {e}" for e in walk_errors)
    for src in paths:
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
    p.add_argument("--ring", default="cont0",
                   help="comma-separated EchoPro processor ring(s) to ingest. "
                        "Default cont0 = the continuous seismometer record. "
                        "Other cont<N> rings (accelerometer / triggered-event / "
                        "junk -- ring names are arbitrary recorder-config labels) "
                        "are NOT ingested by default; triggers are re-derivable "
                        "STA/LTA. Override only if you know a specific ring holds "
                        "wanted continuous data. See memory echopro-rings-cont0-only.")
    p.add_argument("--scratch", default=None,
                   help="per-day scratch dir for the .dmx copy. Default: a "
                        "per-PID dir under /tmp (so concurrent runs can't "
                        "race on the same scratch).")
    p.add_argument("--no-copy", action="store_true", help="read straight from card (skip scratch)")
    p.add_argument("--reprocess", action="store_true",
                   help="re-do days even if already in the SDS (default: resume / skip done days)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--buffer-dir", default=None,
                   help="local SDS buffer dir. Days are written here, then "
                        "drained to the remote SDS root opportunistically. "
                        "Default: a per-PID dir under /tmp.")
    p.add_argument("--buffer-cap-mb", type=int, default=8000,
                   help="pause USB reads when the local SDS buffer reaches "
                        "this size in MB. Default 8000 (~8 GB, ~90 EchoPro "
                        "days). The mount can be down this long without "
                        "stalling production.")
    args = p.parse_args(argv[1:])

    if not args.scratch:
        args.scratch = f"/tmp/echopro_scratch.{os.getpid()}"
    if not args.buffer_dir:
        args.buffer_dir = f"/tmp/echopro_buffer.{os.getpid()}"
    min_date = date.fromisoformat(args.min_date)
    max_date = date.fromisoformat(args.max_date) if args.max_date else date.today()

    inv = None
    if args.inventory:
        from obspy import read_inventory
        inv = read_inventory(args.inventory)

    rings = tuple(r.strip() for r in args.ring.split(",") if r.strip())
    print(f"Discovering day-dirs under {args.card_root} (rings: {', '.join(rings)}) ...",
          flush=True)
    days, skipped = discover_days(args.card_root, min_date, max_date, rings=rings)
    if skipped:
        print(f"Skipping {len(skipped)} out-of-window/invalid day-dir(s) "
              f"(e.g. {skipped[0][1]})", flush=True)
    if args.limit:
        days = days[:args.limit]
    print(f"Found {len(days)} day(s) under {args.card_root}", flush=True)
    print(f"SDS remote : {args.sds_root}\n"
          f"SDS buffer : {args.buffer_dir} (cap {args.buffer_cap_mb} MB)\n"
          f"dmx scratch: {args.scratch}\n"
          f"copy-local : {not args.no_copy} | inventory: {bool(inv)}\n",
          flush=True)

    signal.signal(signal.SIGINT, _on_sigint)
    grand_tr = grand_bad = n_skip = 0
    flagged = []
    t0 = time.time()
    mount_script = Path(__file__).resolve().parent / "mount_staging.sh"
    if not mount_script.is_file():
        mount_script = None
    buf = BufferedStaging(args.buffer_dir, args.sds_root,
                          cap_mb=args.buffer_cap_mb,
                          mount_script=mount_script)
    n_done = 0
    for day_dir, dd in days:
        if _STOP["flag"]:
            print(f"  -- stop requested; exiting cleanly before {dd} (re-run to resume) --",
                  flush=True)
            break
        day_t0 = time.time()
        sta = args.station or station_from_files(day_dir)
        if not sta:
            print(f"  {dd}  SKIP: no station in filenames")
            continue
        net = args.network or sc.network_for_station(sta, args.registry)
        if not net:
            print(f"  {dd}  SKIP: no network for {sta} (not in registry; pass --network)")
            continue
        if not args.reprocess and day_already_done(buf, net, sta, dd):
            n_skip += 1
            print(f"  {dd}  {net}.{sta}  already done -> skip", flush=True)
            continue

        if args.no_copy:
            files, copy_fail = _walk_dmx_tolerant(day_dir)
        else:
            sdir = Path(args.scratch)
            if sdir.exists():
                shutil.rmtree(sdir)
            sdir.mkdir(parents=True)
            files, copy_fail = robust_copy_day(day_dir, sdir)

        st, qc = sc.convert_suds_files(files, network=net, station=sta, inv=inv)
        # Write to LOCAL buffer (fast, never blocks on SMB). Then attempt to
        # promote everything pending to staging -- if the mount is down the
        # promote is a no-op and the day waits in the buffer for the next try.
        written = sc.write_sds(st, str(buf.write_root))
        buf.promote_pending()
        # Block here only if buffer hit its cap (cap_mb-worth of unflushed
        # days). Below the cap, just press on with the next day.
        buf.wait_for_capacity()
        bad = len(qc["read_errors"]) + len(copy_fail)
        grand_tr += qc["n_traces"]
        grand_bad += bad
        tag = ""
        if bad or qc["dropped_components"]:
            flagged.append((dd.isoformat(), bad, qc["dropped_components"]))
            tag = f"  [QC: {bad} bad files, dropped={qc['dropped_components'] or '-'}]"
        day_dt = time.time() - day_t0
        n_done += 1
        # Running ETA: extrapolate from the wall time spent on actually-
        # converted days (skipped days are nearly free, so use the elapsed
        # wall time across the converted ones).
        elapsed = time.time() - t0
        remaining = len(days) - (n_done + n_skip)
        eta_s = (elapsed / n_done) * remaining if n_done > 0 else 0.0
        buf_tag = (f" buf={buf.current_mb:.0f}MB"
                   if buf.current_mb > 1 else "")
        print(f"  {dd}  {net}.{sta} {qc['rate_hz']}Hz -> {qc['n_traces']} traces, "
              f"{len(written)} SDS files  ({day_dt:.0f}s, "
              f"{n_done}/{len(days)-n_skip} done, ETA {eta_s/3600:.1f}h{buf_tag}){tag}",
              flush=True)

        if not args.no_copy:
            shutil.rmtree(args.scratch, ignore_errors=True)

    # Drain anything left in the buffer before we exit -- otherwise the user
    # has converted data sitting locally that never reaches staging.
    if buf.current_mb > 0:
        print(f"\nFinal drain: {buf.current_mb:.0f} MB still in buffer; "
              f"draining to staging ...", flush=True)
        ok = buf.drain_blocking(max_wait_seconds=1800)
        if not ok:
            print(f"  WARNING: {buf.current_mb:.0f} MB still in buffer at "
                  f"{buf.write_root} -- re-run to retry the drain.", flush=True)
    dt = time.time() - t0
    print(f"\nTOTAL: {len(days)} day(s): {n_skip} already-done/skipped, "
          f"{grand_tr} traces converted, {grand_bad} bad files, {dt:.1f}s",
          flush=True)
    if flagged:
        print(f"QC-flagged day(s) ({len(flagged)}): "
              + ", ".join(f"{d}({n})" for d, n, _ in flagged[:25]),
              flush=True)
    return 1 if grand_bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
