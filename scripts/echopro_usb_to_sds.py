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

DAY_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})$")
STA_RE = re.compile(r".*_([A-Za-z0-9]+)\.dmx(?:\.gz)?$")

_STOP = {"flag": False}

# OSError errnos that almost always mean "SMB mount dropped" rather than a
# real programming/permissions bug. ENOTCONN is the one macOS raises first
# when an in-flight write hits a stale CIFS connection.
_MOUNT_DROP_ERRNOS = {errno.ENOTCONN, errno.EIO, errno.ENODEV, errno.ENOENT,
                      errno.EPIPE, errno.ETIMEDOUT, errno.EHOSTUNREACH}


def _probe_mount(path: Path) -> bool:
    """True iff `path` is reachable AND writable (cheap touch-and-unlink)."""
    try:
        if not path.is_dir():
            return False
        probe = path / f".mount_probe.{os.getpid()}"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _wait_for_mount(path: Path, mount_script: Path | None,
                    poll_seconds: int = 30) -> None:
    """Block until `path` is writable. Every other poll, try the mount script
    (idempotent: open smb:// just re-attaches if already mounted, no-op if
    already healthy). Designed to ride out an SMB drop and resume the same
    day's write once the share is back, without dropping any work."""
    attempts = 0
    t0 = time.time()
    while True:
        if _probe_mount(path):
            if attempts > 0:
                print(f"  [mount back after {time.time() - t0:.0f}s; "
                      f"resuming]", flush=True)
            return
        attempts += 1
        if mount_script is not None and attempts % 2 == 1:
            print(f"  [mount unreachable; auto-remount attempt {attempts}]",
                  flush=True)
            subprocess.run([str(mount_script)], check=False,
                           capture_output=True)
        else:
            print(f"  [mount unreachable; sleeping {poll_seconds}s "
                  f"(attempt {attempts})]", flush=True)
        time.sleep(poll_seconds)


def _with_mount_retry(fn, sds_root: Path, mount_script: Path | None):
    """Run fn(); if it raises an OSError that smells like an SMB drop AND
    the sds_root mount really is unreachable, stall until the mount is back,
    then retry fn() ONCE more. A second failure re-raises (genuine fault)."""
    try:
        return fn()
    except OSError as e:
        if e.errno not in _MOUNT_DROP_ERRNOS:
            raise
        if _probe_mount(sds_root):
            # Mount looks fine -- this OSError is something else (perms,
            # disk full, programming error). Don't paper over it.
            raise
        print(f"  [OSError {e.errno} ({errno.errorcode.get(e.errno, '?')}) "
              f"on staging write; waiting for mount]", flush=True)
        _wait_for_mount(sds_root, mount_script)
        return fn()  # one retry; second failure propagates


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
    """Find day-dirs under the known EchoPro layout: <card>/cont*/YYYY/MM/DD.

    We DON'T rglob -- the card has ~3M files (98 days * 24h * ~1440 .dmx) and
    walking them all takes many minutes on a slow USB. Instead we iterdir at
    exactly 4 levels (cont*/Y/M/D) which is O(~100) stat calls."""
    days, skipped = [], []
    base = Path(card_root)
    for cont in sorted(p for p in base.iterdir()
                       if p.is_dir() and p.name.startswith("cont")):
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
    p.add_argument("--scratch", default=None,
                   help="per-day scratch dir for the .dmx copy. Default: a "
                        "per-PID dir under /tmp (so concurrent runs can't "
                        "race on the same scratch).")
    p.add_argument("--no-copy", action="store_true", help="read straight from card (skip scratch)")
    p.add_argument("--reprocess", action="store_true",
                   help="re-do days even if already in the SDS (default: resume / skip done days)")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args(argv[1:])

    if not args.scratch:
        args.scratch = f"/tmp/echopro_scratch.{os.getpid()}"
    min_date = date.fromisoformat(args.min_date)
    max_date = date.fromisoformat(args.max_date) if args.max_date else date.today()

    inv = None
    if args.inventory:
        from obspy import read_inventory
        inv = read_inventory(args.inventory)

    print(f"Discovering day-dirs under {args.card_root} ...", flush=True)
    days, skipped = discover_days(args.card_root, min_date, max_date)
    if skipped:
        print(f"Skipping {len(skipped)} out-of-window/invalid day-dir(s) "
              f"(e.g. {skipped[0][1]})", flush=True)
    if args.limit:
        days = days[:args.limit]
    print(f"Found {len(days)} day(s) under {args.card_root}", flush=True)
    print(f"SDS out: {args.sds_root} | scratch: {args.scratch} | "
          f"copy-local: {not args.no_copy} | inventory: {bool(inv)}\n",
          flush=True)

    signal.signal(signal.SIGINT, _on_sigint)
    grand_tr = grand_bad = n_skip = 0
    flagged = []
    t0 = time.time()
    sds_root_path = Path(args.sds_root)
    mount_script = Path(__file__).resolve().parent / "mount_staging.sh"
    if not mount_script.is_file():
        mount_script = None
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
        if not args.reprocess and _with_mount_retry(
                lambda: day_already_done(args.sds_root, net, sta, dd),
                sds_root_path, mount_script):
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
        written = _with_mount_retry(
            lambda: sc.write_sds(st, args.sds_root),
            sds_root_path, mount_script)
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
        print(f"  {dd}  {net}.{sta} {qc['rate_hz']}Hz -> {qc['n_traces']} traces, "
              f"{len(written)} SDS files  ({day_dt:.0f}s, "
              f"{n_done}/{len(days)-n_skip} done, ETA {eta_s/3600:.1f}h){tag}",
              flush=True)

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
