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

import json
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from staging_buffer import BufferedStaging, probe_mount, _banner  # noqa: E402

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
                remote_root: Path | None = None,
                network_override: str | None = None,
                location_override: str | None = None,
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
    # For network/location override: pad/truncate to exactly 2 ASCII bytes
    # (SEED net field at bytes 18:20, loc field at bytes 13:15).
    net_override_bytes = (network_override.encode("ascii").ljust(2)[:2]
                          if network_override else None)
    loc_override_bytes = (location_override.encode("ascii").ljust(2)[:2]
                          if location_override else None)

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
                cha = rec[15:18].decode("ascii", "replace").strip()
                if loc_override_bytes is not None:
                    # Rewrite the SEED location field in-place. Used when
                    # the Gecko's settings.ss had an empty/spaces location_id
                    # (which strips to "") -- VW policy is loc="00" for the
                    # primary sensor at every station.
                    rec = rec[:13] + loc_override_bytes + rec[15:]
                    loc = location_override
                else:
                    loc = rec[13:15].decode("ascii", "replace").strip()
                if net_override_bytes is not None:
                    # Rewrite the SEED network field in-place. Used for old
                    # cards whose Gecko was configured with a pre-FDSN code
                    # (e.g. "UM") that's now obsolete.
                    rec = rec[:18] + net_override_bytes + rec[20:]
                    net = network_override
                else:
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


def transfer_day_to_remote(local_paths: list[Path], local_root: Path,
                           remote_root: Path) -> tuple[bool, str]:
    """Copy each completed-day file to the corresponding remote SDS path.

    Uses `cp` to a `.partial` on the remote, verifies the byte count, then
    atomically renames into place. On the mediaflux SMB mount this is ~2x
    faster than `rsync -a --partial` (which pays delta-transfer + checksum
    + temp-file overhead that's pure waste for brand-new files), while
    keeping the same crash-safety guarantees:

      - mid-cp crash leaves a `.partial` (SeisComp ignores it)
      - rename is atomic on the same SMB filesystem
      - size mismatch (truncated SMB write) is caught and reported

    No `--delete` semantics anywhere — this only ever writes.

    Returns (success, message). On success the LOCAL files are NOT deleted
    here — the caller decides (so --keep-local can opt out).
    """
    if not local_paths:
        return True, "no files"

    for lp in local_paths:
        try:
            rel = lp.relative_to(local_root)
        except ValueError:
            return False, f"{lp} is not under local_root {local_root}"
        rp = remote_root / rel
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp_partial = rp.with_suffix(rp.suffix + ".partial")
        local_size = lp.stat().st_size

        r = subprocess.run(["cp", str(lp), str(rp_partial)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"cp {lp.name}: exit {r.returncode}: {r.stderr.strip()}"

        # Verify the full payload landed before promoting.
        try:
            remote_size = rp_partial.stat().st_size
        except OSError as e:
            return False, f"stat {rp_partial.name}: {e}"
        if remote_size != local_size:
            return False, (f"{lp.name}: size mismatch after cp "
                           f"(local {local_size}, remote {remote_size})")

        os.replace(rp_partial, rp)  # atomic rename on the same SMB mount

    return True, "ok"


def peek_identity(day_dir: Path, max_files: int = 5,
                  network_override: str | None = None
                  ) -> tuple[str, str, list[str], str]:
    """Read a few records from the first files in a day dir to learn net, sta,
    channel set, and the location seen in those records. Returns
    ("","",[],"") if more than one net/sta is seen (ambiguous card).
    Net + loc reflect what's IN the records (NOT any override) -- callers
    apply overrides separately."""
    nets: set[str] = set()
    stas: set[str] = set()
    chas: set[str] = set()
    locs: set[str] = set()
    for f in sorted(p for p in day_dir.rglob("*.ms") if p.is_file())[:max_files]:
        if f.stat().st_size < 48:
            continue
        data = f.read_bytes()
        reclen = reclen_from_first_record(data[:64])
        for i in range(len(data) // reclen):
            rec = data[i * reclen:(i + 1) * reclen]
            sta = rec[8:13].decode("ascii", "replace").strip()
            cha = rec[15:18].decode("ascii", "replace").strip()
            net = rec[18:20].decode("ascii", "replace").strip()
            loc_raw = rec[13:15].decode("ascii", "replace")  # don't strip -- "" vs "  " matters
            if net and sta and cha:
                nets.add(net)
                stas.add(sta)
                chas.add(cha)
                locs.add(loc_raw.strip())
    if len(nets) != 1 or len(stas) != 1:
        return "", "", [], ""
    return (network_override if network_override else next(iter(nets)),
            next(iter(stas)), sorted(chas),
            next(iter(locs)) if len(locs) == 1 else "")


def stamp_pull_card_json(cards_root: Path, settings_path: Path,
                         full_day_dirs: list[tuple[Path, int, int, int]],
                         remote_root: Path | None,
                         network_override: str | None = None,
                         location_override: str | None = None) -> None:
    """After a successful pull, record pull provenance into the card.json that
    rename_card.py created. Update-only: if no matching card.json exists, this
    prints a note and does nothing (run rename_card.py first). Never raises out;
    stamping is secondary to the data already being safely in staging."""
    if not full_day_dirs:
        print("card.json: no in-window days; skipping stamp.")
        return
    if not settings_path.is_file():
        print(f"card.json: no settings.ss at {settings_path}; skipping stamp.")
        return
    serial = ""
    for line in settings_path.read_text(errors="replace").splitlines():
        m = re.match(r'"(\w+)"\s*=\s*"?([^"\n]*?)"?\s*$', line.strip())
        if m and m.group(1) == "serial":
            serial = m.group(2).strip()
            break
    if not serial:
        print("card.json: serial not in settings.ss; skipping stamp.")
        return
    dates = [date(y, mo, d) for (_p, y, mo, d) in full_day_dirs]
    start, end = min(dates), max(dates)
    # card-id format MUST match rename_card.py.
    card_id = f"{start:%Y%m%d}-{end:%Y%m%d}_{serial[-4:]}"
    net, sta, channels, _loc = peek_identity(full_day_dirs[0][0],
                                             network_override=network_override)
    if not (net and sta):
        print("card.json: could not resolve a single net/sta from records; "
              "skipping stamp.")
        return
    card_path = cards_root / f"{net}.{sta}" / card_id / "card.json"
    if not card_path.exists():
        print(f"card.json: {card_path} not found — run rename_card.py first to "
              "register the card; skipping pull stamp.")
        return
    rec = json.loads(card_path.read_text())
    rec["pull_date"] = date.today().isoformat()
    rec["day_count"] = len(full_day_dirs)
    rec["data_span"] = {"start": start.isoformat(), "end": end.isoformat()}
    if channels:
        rec["channels"] = channels
    if remote_root is not None:
        rec["staging_path"] = f"{remote_root.name}/<YYYY>/{net}/{sta}"
    note = rec.get("notes", "")
    if (not note) or ("pending" in note):
        rec["notes"] = (f"Pulled to staging {rec['pull_date']} "
                        f"({len(full_day_dirs)} days); apply pending. "
                        "Pre-GPS-lock day-dirs excluded by date window.")
    card_path.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    print(f"card.json: stamped pull metadata -> {card_path}")


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
    p.add_argument("--min-date", default="2015-01-01",
                   help="skip day directories dated before this (YYYY-MM-DD). "
                        "Recorders write pre-GPS-lock bootup data with their "
                        "default clock (e.g. 2012-01-01) until satellite lock; "
                        "this drops it. Default 2015-01-01.")
    p.add_argument("--max-date", default=None,
                   help="skip day directories dated after this (YYYY-MM-DD). "
                        "Default: today (drops GPS week-rollover future dates).")
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
    p.add_argument("--cards-root",
                   default=str(Path(__file__).resolve().parents[2]
                               / "sds_staging_ledger" / "cards"),
                   help="ledger cards/ dir; after a successful pull, stamp pull "
                        "metadata (pull_date/day_count/channels/staging_path) "
                        "into the matching card.json that rename_card.py created. "
                        "Default: ../../sds_staging_ledger/cards.")
    p.add_argument("--no-card-json", action="store_true",
                   help="don't stamp card.json after the pull.")
    p.add_argument("--network", default=None,
                   help="override the SEED network field on every record (bytes "
                        "18:20 of each 512 B MSEED record). Use for cards whose "
                        "Gecko was configured with a pre-FDSN code (e.g. 'UM') "
                        "that's now obsolete. Affects record contents AND the "
                        "SDS path (NET/STA/CHA.D). 2 ASCII chars max.")
    p.add_argument("--location", default=None,
                   help="override the SEED location field on every record (bytes "
                        "13:15). Use for cards whose Gecko had empty/spaces "
                        "location_id (which strips to '' -- breaking SDS path "
                        "consistency). VW policy: primary sensor = '00'. "
                        "2 ASCII chars max.")
    args = p.parse_args(argv[1:])
    if args.network is not None and len(args.network.strip()) > 2:
        print(f"ERROR: --network must be <= 2 chars (got {args.network!r})")
        return 2
    if args.location is not None and len(args.location.strip()) > 2:
        print(f"ERROR: --location must be <= 2 chars (got {args.location!r})")
        return 2

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

    # Valid-date window: drop pre-GPS-lock bootup dirs (default clock, e.g.
    # 2012) and any future-dated dirs from GPS week-rollover glitches.
    min_date = date.fromisoformat(args.min_date)
    max_date = date.fromisoformat(args.max_date) if args.max_date else date.today()

    # Find day dirs: YYYY/MM/DD
    day_dirs: list[tuple[Path, int, int, int]] = []
    skipped_dates: list[tuple[Path, str]] = []
    for year_dir in sorted(p for p in sd_data_dir.iterdir() if p.is_dir() and p.name.isdigit()):
        for month_dir in sorted(p for p in year_dir.iterdir() if p.is_dir() and p.name.isdigit()):
            for day_dir in sorted(p for p in month_dir.iterdir() if p.is_dir() and p.name.isdigit()):
                m = DAY_RE.search(str(day_dir))
                if not m:
                    continue
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                try:
                    dd = date(y, mo, d)
                except ValueError:
                    skipped_dates.append((day_dir, f"invalid date {y:04d}-{mo:02d}-{d:02d}"))
                    continue
                if dd < min_date or dd > max_date:
                    skipped_dates.append((day_dir, dd.isoformat()))
                    continue
                day_dirs.append((day_dir, y, mo, d))

    if skipped_dates:
        print(f"Skipping {len(skipped_dates)} day-dir(s) outside date window "
              f"[{min_date} .. {max_date}] (pre-GPS-lock / clock-glitch garbage):")
        for pth, why in skipped_dates:
            print(f"  SKIP {why}: {pth}")
        print()

    full_day_dirs = list(day_dirs)  # full filtered set, for card.json span/count

    # Empty-location guard: VW policy is loc='00' for the primary sensor at
    # every station. If the Gecko's settings.ss had empty/spaces location_id,
    # the records carry empty loc -- which propagates to '..' double-dot SDS
    # filenames and breaks consistency with the rest of the network. Refuse
    # to proceed without an explicit --location override.
    if not args.location and day_dirs:
        _net, _sta, _cha, sample_loc = peek_identity(day_dirs[0][0],
                                                    network_override=args.network)
        if not sample_loc:
            print("ERROR: empty SEED location code on this card -- the Gecko's "
                  "settings.ss was misconfigured (location_id empty/spaces).")
            print("VW policy: primary sensor location = '00'. Re-run with:")
            print(f"  --location 00")
            print("This rewrites bytes 13:15 of every record to '00' in-flight, "
                  "homogenising with the rest of the network. Use a different "
                  "code only if this card is a secondary sensor at a co-located "
                  "site (rare).")
            return 2

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
            ok, msg = transfer_day_to_remote(paths, sds_root, remote_root)  # type: ignore[arg-type]
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
                _note_rsync_result(day_label, ok, msg)
            rsync_q.task_done()

    nonlocal_state = {"rsync_s": 0.0, "mount_down": False, "down_since": 0.0}

    def _note_rsync_result(day_label: str, ok: bool, msg: str) -> None:
        """State transitions on rsync results -- surfaces DROPPED/RESUMED
        banners when the mount comes and goes. Identifies a 'drop' by either
        an explicit mount-drop errno or just by probing the remote root."""
        if ok:
            if nonlocal_state["mount_down"]:
                dt = time.time() - nonlocal_state["down_since"]
                _banner("RESUMED", f"staging mount back after {dt:.0f} s "
                        f"(first success on {day_label})")
                nonlocal_state["mount_down"] = False
            return
        # Failure -- distinguish mount drop from a real fault.
        looks_dropped = (any(k in msg.lower() for k in
                             ("not connected", "no such file", "input/output",
                              "broken pipe", "host is down", "timed out"))
                        or not probe_mount(remote_root))
        if looks_dropped and not nonlocal_state["mount_down"]:
            nonlocal_state["mount_down"] = True
            nonlocal_state["down_since"] = time.time()
            _banner("DROPPED", f"staging mount unreachable while rsyncing "
                    f"{day_label}; local files kept for end-of-run drain")

    worker: threading.Thread | None = None
    if pipelined:
        worker = threading.Thread(target=_rsync_worker, name="rsync-worker", daemon=True)
        worker.start()

    try:
        for day_dir, y, mo, d in day_dirs:
            ts = time.time()
            recs, byts, status, written_paths = process_day(
                day_dir, sds_root, y, mo, d, buffer_bytes, args.skip_existing,
                remote_root, network_override=args.network,
                location_override=args.location)
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
                    ok, msg = transfer_day_to_remote(written_paths, sds_root, remote_root)
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
                    _note_rsync_result(day_label, ok, msg)
    finally:
        if worker is not None:
            rsync_q.put(None)
            worker.join()
            grand_rsync_s = nonlocal_state["rsync_s"]

    # Final drain: any day-channel files still in local scratch were either
    # failed rsyncs (mount drop) or --keep-local. Try to push them to staging
    # now using the shared buffer-and-drain helper, with auto-remount.
    if remote_root is not None and not args.keep_local:
        mount_script = Path(__file__).resolve().parent / "mount_staging.sh"
        if not mount_script.is_file():
            mount_script = None
        drain_buf = BufferedStaging(sds_root, remote_root,
                                    cap_mb=10**9, mount_script=mount_script)
        if drain_buf.current_mb > 0:
            print(f"\nFinal drain: {drain_buf.current_mb:.0f} MB still in local "
                  f"scratch; promoting to staging ...", flush=True)
            ok = drain_buf.drain_blocking(max_wait_seconds=1800)
            if not ok:
                print(f"  WARNING: {drain_buf.current_mb:.0f} MB still in "
                      f"{sds_root} -- re-run to retry the drain.", flush=True)

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

    if not args.no_card_json:
        try:
            stamp_pull_card_json(Path(args.cards_root),
                                 sd_data_dir.parent / "settings.ss",
                                 full_day_dirs, remote_root,
                                 network_override=args.network,
                                 location_override=args.location)
        except Exception as e:  # stamping must never fail a good pull
            print(f"card.json: stamp skipped ({e})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
