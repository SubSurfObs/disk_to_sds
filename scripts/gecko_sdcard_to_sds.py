#!/usr/bin/env python3
"""
SAFETY CONTRACT
---------------
This script is STRICTLY READ-ONLY against the SD card / input directory.
The input is only ever opened with mode "rb" and only read() is called on
it. No path under <sdcard_data_dir> is ever opened for write, renamed,
unlinked, or chmod'd. A startup guard also refuses to run if the SDS
output root sits inside the input directory.

Convert a Gecko SD card (YYYY/MM/DD/HH/*.ms) directly into an SDS archive.

Gecko minute files are concatenated 512-byte MiniSEED records that already
carry the correct net/sta/loc/cha codes, so this just streams raw record
bytes and appends them to:

    SDS_ROOT/YEAR/NET/STA/CHA.D/NET.STA.LOC.CHA.D.YEAR.DOY

The script is a pure streamer: it never holds more than one day's worth of
data in flight, and it issues large buffered writes to keep network mounts
(SMB-mounted mediaflux / staging SDS) happy.

Usage:
    gecko_sdcard_to_sds.py <sdcard_data_dir> <sds_root> [--buffer-mb N]

Examples:
    # Local disk
    gecko_sdcard_to_sds.py "/Volumes/NO NAME/data" ./local_sds

    # Straight to SMB-mounted staging SDS, with 16 MB write buffers per
    # channel so each minute of records doesn't pay SMB latency:
    gecko_sdcard_to_sds.py "/Volumes/NO NAME/data" \
        /Volumes/mediaflux/staging_sds --buffer-mb 16
"""

from __future__ import annotations

import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

DAY_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})$")
DEFAULT_RECLEN = 512  # Gecko default; verified per-file via blockette 1000


def reclen_from_first_record(buf: bytes) -> int:
    """Return record length in bytes from blockette 1000 in the first record."""
    if len(buf) < 48:
        return DEFAULT_RECLEN
    ofs = struct.unpack(">H", buf[46:48])[0]
    if ofs + 8 > len(buf):
        return DEFAULT_RECLEN
    blk_type = struct.unpack(">H", buf[ofs:ofs + 2])[0]
    if blk_type != 1000:
        return DEFAULT_RECLEN
    return 1 << buf[ofs + 6]


def sds_path(sds_root: Path, year: int, doy: int, net: str, sta: str,
             loc: str, cha: str) -> Path:
    return (sds_root / f"{year:04d}" / net / sta / f"{cha}.D"
            / f"{net}.{sta}.{loc}.{cha}.D.{year:04d}.{doy:03d}")


def process_day(day_dir: Path, sds_root: Path, year: int, month: int, day: int,
                buffer_bytes: int = 0, skip_existing: bool = False,
                remote_root: Path | None = None
                ) -> tuple[int, int, str, list[Path]]:
    """Returns (records_written, bytes_written, status, final_local_paths).

    status is one of: "ok", "skipped_existing", "no_data".

    Each output file is built as <sds_path>.partial and atomically renamed
    to <sds_path> only when the day completes successfully. A crash leaves
    .partial files (which SeisComp ignores) but never half-written SDS files.

    If remote_root is given, the skip-existing check looks at the REMOTE
    SDS for already-completed days (the local scratch is expected to be
    empty after a successful rsync).
    """
    doy = date(year, month, day).timetuple().tm_yday
    ms_files = sorted(p for p in day_dir.rglob("*.ms") if p.is_file())
    if not ms_files:
        return 0, 0, "no_data", []

    # Peek at first non-empty file to get record length.
    reclen = DEFAULT_RECLEN
    for f in ms_files:
        if f.stat().st_size >= 48:
            with f.open("rb") as fh:
                reclen = reclen_from_first_record(fh.read(64))
            break

    handles: dict[str, any] = {}          # key -> open file
    partial_paths: dict[str, Path] = {}   # key -> partial path
    final_paths: dict[str, Path] = {}     # key -> final SDS path
    total_records = 0
    total_bytes = 0
    failed = False

    try:
        for f in ms_files:
            sz = f.stat().st_size
            if sz == 0:
                continue
            if sz % reclen != 0:
                print(f"  WARN: {f.name} size {sz} not a multiple of {reclen}; reading what we can")
            # Whole-file read in one syscall (Gecko minute files are ~50 KB);
            # cuts the per-file open/read/close overhead that dominates SD card
            # ingest time. Memory footprint is one minute file (~80 KB) at a
            # time, which is trivial.
            data = f.read_bytes()
            n_records = len(data) // reclen
            for i in range(n_records):
                rec = data[i * reclen:(i + 1) * reclen]
                # SEED fixed header: sta 8-13, loc 13-15, cha 15-18, net 18-20
                sta = rec[8:13].decode("ascii", "replace").strip()
                loc = rec[13:15].decode("ascii", "replace").strip()
                cha = rec[15:18].decode("ascii", "replace").strip()
                net = rec[18:20].decode("ascii", "replace").strip()
                # Drop bootup / no-GPS-lock records that have factory-default
                # codes (empty net, or default station like "GECK6"). These
                # otherwise pollute the SDS tree with malformed paths.
                if not net or not sta or not cha:
                    continue
                key = f"{net}.{sta}.{loc}.{cha}"
                fh_out = handles.get(key)
                if fh_out is None:
                    final_path = sds_path(sds_root, year, doy, net, sta, loc, cha)
                    if skip_existing:
                        check_path = (sds_path(remote_root, year, doy, net, sta, loc, cha)
                                      if remote_root is not None else final_path)
                        if check_path.exists() and check_path.stat().st_size > 0:
                            return 0, 0, "skipped_existing", []
                    partial_path = final_path.with_suffix(final_path.suffix + ".partial")
                    partial_path.parent.mkdir(parents=True, exist_ok=True)
                    # Start fresh if a stale .partial from a prior crash exists.
                    if partial_path.exists():
                        partial_path.unlink()
                    if buffer_bytes > 0:
                        fh_out = open(partial_path, "wb", buffering=buffer_bytes)
                    else:
                        fh_out = open(partial_path, "wb")
                    handles[key] = fh_out
                    partial_paths[key] = partial_path
                    final_paths[key] = final_path
                fh_out.write(rec)
                total_records += 1
                total_bytes += reclen
    except Exception:
        failed = True
        raise
    finally:
        # Close all handles (this flushes buffered writes).
        for fh_out in handles.values():
            try:
                fh_out.close()
            except Exception:
                failed = True

        if failed:
            # Leave .partial files behind for debugging; never promote them.
            pass
        else:
            # Atomic-promote each .partial to its final SDS path.
            for key, partial_path in partial_paths.items():
                final_path = final_paths[key]
                # os.replace is atomic on POSIX and overwrites if dest exists.
                os.replace(partial_path, final_path)

    return total_records, total_bytes, "ok", list(final_paths.values())


def rsync_day_to_remote(local_paths: list[Path], local_root: Path,
                        remote_root: Path, parallel: bool = False
                        ) -> tuple[bool, str]:
    """Rsync each completed-day file to the corresponding remote SDS path.

    parallel=False by default: on the mediaflux SMB mount, concurrent
    rsyncs are ~10x slower than serial (per-connection bandwidth cap).
    parallel=True is kept as an opt-in for other mounts where SMB can
    actually multiplex.

    Returns (success, message). On success the local files are NOT deleted
    here — the caller decides (so --keep-local can opt out).
    """
    if not local_paths:
        return True, "no files"

    # Pre-compute (lp, rp) pairs and ensure remote dirs exist (mkdir is the
    # only step we MUST do before launching rsyncs).
    plans: list[tuple[Path, Path]] = []
    for lp in local_paths:
        try:
            rel = lp.relative_to(local_root)
        except ValueError:
            return False, f"{lp} is not under local_root {local_root}"
        rp = remote_root / rel
        rp.parent.mkdir(parents=True, exist_ok=True)
        plans.append((lp, rp))

    def _rsync_cmd(lp: Path, rp: Path) -> list[str]:
        # -a preserves perms/times; --partial keeps a half-transferred file
        # around so a retry resumes instead of re-sending. No --delete:
        # we never want this script to remove anything on the remote.
        return ["rsync", "-a", "--partial", str(lp), str(rp)]

    if not parallel or len(plans) <= 1:
        for lp, rp in plans:
            r = subprocess.run(_rsync_cmd(lp, rp), capture_output=True, text=True)
            if r.returncode != 0:
                return False, f"rsync exit {r.returncode}: {r.stderr.strip()}"
        return True, "ok"

    # Launch all rsyncs concurrently; wait for all and collect failures.
    procs = [(lp, rp, subprocess.Popen(_rsync_cmd(lp, rp),
                                       stderr=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL))
             for lp, rp in plans]
    failures: list[str] = []
    for lp, rp, proc in procs:
        proc.wait()
        if proc.returncode != 0:
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            failures.append(f"{lp.name}: exit {proc.returncode}: {err}")
        proc.stderr.close()
    if failures:
        return False, "; ".join(failures)
    return True, "ok"


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Gecko SD card -> SDS archive (slow/safe by default, "
                    "use --fast for big SMB writes).")
    p.add_argument("sdcard_data_dir", help="path to .../data on the SD card")
    p.add_argument("sds_root", help="output SDS root")
    p.add_argument("--buffer-mb", type=int, default=0,
                   help="per-channel write buffer in MB (0 = OS default ~8 KB; "
                        "use 8-16 when writing to an SMB mount)")
    p.add_argument("--fast", action="store_true",
                   help="shortcut for --buffer-mb 16; safety unchanged")
    p.add_argument("--skip-existing", action="store_true",
                   help="skip any day whose first channel SDS file already "
                        "exists (idempotent re-run after a crash)")
    p.add_argument("--limit", type=int, default=0,
                   help="only process the first N day directories (for testing)")
    p.add_argument("--rsync-to", default=None,
                   help="after each day finishes locally, rsync the 3 day-channel "
                        "files to this REMOTE SDS root and delete the local copies. "
                        "Use this to keep local footprint <100 MB and avoid the "
                        "SD->SMB direct-write slowdown.")
    p.add_argument("--keep-local", action="store_true",
                   help="with --rsync-to, do NOT delete local files after rsync "
                        "(for debugging / verification).")
    p.add_argument("--no-pipeline", action="store_true",
                   help="serialize write and rsync (for debugging). Default "
                        "is pipelined: while day N+1 is being read from the SD "
                        "card, day N is being rsynced. Saves ~30%% wall time.")
    p.add_argument("--pipeline-depth", type=int, default=2,
                   help="max number of completed days queued for rsync at "
                        "once. Bigger = more headroom against rsync stalls "
                        "at the cost of /tmp footprint (~100 MB/day). "
                        "Default 2.")
    args = p.parse_args(argv[1:])

    sd_data_dir = Path(args.sdcard_data_dir)
    sds_root = Path(args.sds_root)
    remote_root = Path(args.rsync_to) if args.rsync_to else None
    if args.fast and args.buffer_mb == 0:
        args.buffer_mb = 16
    buffer_bytes = args.buffer_mb * 1024 * 1024

    # Safety: refuse to run if the SDS output (or rsync target) sits inside
    # the SD card / input directory. The SD card must remain untouched.
    try:
        sd_resolved = sd_data_dir.resolve(strict=True)
    except FileNotFoundError:
        print(f"ERROR: input {sd_data_dir} does not exist")
        return 2
    if not sd_data_dir.is_dir():
        print(f"ERROR: {sd_data_dir} is not a directory")
        return 2
    for label, candidate in (("sds_root", sds_root),
                             ("rsync-to", remote_root) if remote_root else (None, None)):
        if label is None:
            continue
        cand_resolved = candidate.resolve()
        if str(cand_resolved).startswith(str(sd_resolved) + os.sep) \
                or cand_resolved == sd_resolved:
            print(f"REFUSING: {label} {cand_resolved} is inside input "
                  f"{sd_resolved}. The SD card must not be written to.")
            return 2
    sds_root.mkdir(parents=True, exist_ok=True)
    if remote_root is not None:
        if not remote_root.parent.exists():
            print(f"ERROR: --rsync-to parent {remote_root.parent} does not exist "
                  "(is the staging mount actually mounted?)")
            return 2
        remote_root.mkdir(parents=True, exist_ok=True)

    # Find day dirs: YYYY/MM/DD
    day_dirs: list[tuple[Path, int, int, int]] = []
    for year_dir in sorted(p for p in sd_data_dir.iterdir() if p.is_dir() and p.name.isdigit()):
        for month_dir in sorted(p for p in year_dir.iterdir() if p.is_dir() and p.name.isdigit()):
            for day_dir in sorted(p for p in month_dir.iterdir() if p.is_dir() and p.name.isdigit()):
                m = DAY_RE.search(str(day_dir))
                if not m:
                    continue
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                day_dirs.append((day_dir, y, mo, d))

    if args.limit > 0:
        day_dirs = day_dirs[:args.limit]

    pipelined = (remote_root is not None) and (not args.no_pipeline)

    print(f"Found {len(day_dirs)} day directories to process under {sd_data_dir}")
    print(f"Writing local scratch SDS to {sds_root.resolve()}")
    if remote_root is not None:
        print(f"Rsyncing each completed day to {remote_root.resolve()}")
        print(f"Delete local after rsync: {not args.keep_local}")
        print(f"Pipelined rsync: {pipelined}"
              + (f"  (depth={args.pipeline_depth})" if pipelined else ""))
    print(f"Per-channel write buffer: {args.buffer_mb} MB"
          + ("  (--fast)" if args.fast else ""))
    print(f"Skip-existing: {args.skip_existing}"
          + ("  (checks REMOTE)" if remote_root is not None else ""))
    print()

    grand_recs = 0
    grand_bytes = 0
    grand_write_s = 0.0
    grand_rsync_s = 0.0
    skipped = 0
    rsync_failures: list[str] = []
    t0 = time.time()

    # ------- pipelined rsync worker -------
    # The main thread reads from SD and writes to local scratch. While it's
    # busy on day N+1, this worker rsyncs day N to the remote. This hides
    # the smaller of (write, rsync) per day. The bounded queue caps /tmp
    # footprint at ~pipeline_depth days.
    rsync_q: queue.Queue = queue.Queue(maxsize=max(1, args.pipeline_depth))
    rsync_results_lock = threading.Lock()

    def _rsync_worker() -> None:
        while True:
            item = rsync_q.get()
            if item is None:
                rsync_q.task_done()
                return
            day_label, paths = item
            tr = time.time()
            ok, msg = rsync_day_to_remote(paths, sds_root, remote_root)  # type: ignore[arg-type]
            dt = time.time() - tr
            with rsync_results_lock:
                nonlocal_state["rsync_s"] += dt
                if ok:
                    if not args.keep_local:
                        for lp in paths:
                            try:
                                lp.unlink()
                            except OSError as e:
                                print(f"  WARN: could not delete {lp}: {e}", flush=True)
                    print(f"    {day_label}  rsync {dt:6.2f}s", flush=True)
                else:
                    rsync_failures.append(f"{day_label}: {msg}")
                    print(f"    {day_label}  rsync FAIL ({msg})", flush=True)
            rsync_q.task_done()

    nonlocal_state = {"rsync_s": 0.0}
    worker: threading.Thread | None = None
    if pipelined:
        worker = threading.Thread(target=_rsync_worker, name="rsync-worker", daemon=True)
        worker.start()

    try:
        for day_dir, y, mo, d in day_dirs:
            ts = time.time()
            recs, byts, status, written_paths = process_day(
                day_dir, sds_root, y, mo, d, buffer_bytes, args.skip_existing,
                remote_root)
            dt_write = time.time() - ts
            mb = byts / (1024 * 1024)
            rate = mb / dt_write if dt_write > 0 else 0.0
            day_label = f"{y:04d}-{mo:02d}-{d:02d}"

            tag = "" if status == "ok" else f"  [{status}]"
            print(f"  {day_label}  {recs:>7d} recs  {mb:8.2f} MB  "
                  f"write {dt_write:6.2f}s {rate:7.2f} MB/s{tag}", flush=True)
            if status == "skipped_existing":
                skipped += 1
            grand_recs += recs
            grand_bytes += byts
            grand_write_s += dt_write

            if status == "ok" and remote_root is not None and written_paths:
                if pipelined:
                    rsync_q.put((day_label, written_paths))  # blocks if depth full
                else:
                    tr = time.time()
                    ok, msg = rsync_day_to_remote(written_paths, sds_root, remote_root)
                    dt_rsync = time.time() - tr
                    grand_rsync_s += dt_rsync
                    if ok:
                        if not args.keep_local:
                            for lp in written_paths:
                                try:
                                    lp.unlink()
                                except OSError as e:
                                    print(f"  WARN: could not delete {lp}: {e}", flush=True)
                        print(f"    {day_label}  rsync {dt_rsync:6.2f}s", flush=True)
                    else:
                        rsync_failures.append(f"{day_label}: {msg}")
                        print(f"    {day_label}  rsync FAIL ({msg})", flush=True)
    finally:
        if worker is not None:
            rsync_q.put(None)
            worker.join()
            grand_rsync_s = nonlocal_state["rsync_s"]

    dt_total = time.time() - t0
    mb_total = grand_bytes / (1024 * 1024)
    print()
    print(f"TOTAL: {grand_recs} records, {mb_total:.2f} MB in {dt_total:.2f}s "
          f"({mb_total / dt_total if dt_total > 0 else 0:.2f} MB/s)  "
          f"write={grand_write_s:.2f}s rsync={grand_rsync_s:.2f}s skipped={skipped}")
    if rsync_failures:
        print()
        print(f"RSYNC FAILURES ({len(rsync_failures)} day(s)) — local files kept:")
        for f in rsync_failures:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
