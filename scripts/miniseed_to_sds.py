#!/usr/bin/env python3
"""Ingest pre-downloaded MiniSEED into an SDS tree (the generic "MiniSEED on disk"
adapter reserved in CLAUDE.md). First consumer: Seismosphere web click-downloads
(DU.ARKL2 backfill) — stations with no SeedLink/slarchive path whose data would
otherwise age out of Seismosphere's ~25-day retention.

SAFETY CONTRACT
---------------
STRICTLY READ-ONLY against the inputs (opened only for reading; .zip read in
memory). Writes only under --sds-out, atomically (.partial -> os.replace). No
fetch (files arrive out-of-band, by hand), no format conversion (input is
already MiniSEED).

WHAT IT DOES
------------
- Gathers MiniSEED from any mix of inputs: .ms/.mseed/.seed files, directories
  (scanned), or .zip archives (Seismosphere ships a zip of one all-station .ms
  plus per-station StationXML; the .xml are ignored, the .ms read from memory).
- FILTERS to the station(s) you want (--select, NSLC globs) because a Seismosphere
  window is ALL-STATION. Optional --remap OLD=NEW fixes a wrong code case-by-case
  (codes are usually already correct, so remap is the exception).
- DEDUPS overlapping downloads and routes records to SDS day-files BY RECORD
  START-TIME (merge(method=1) per channel resolves overlaps; split() keeps real
  gaps), cuts on UTC midnights, writes STEIM2 day-files.
- Does NOT write provenance itself: apply.py places the staged days into LT with
  --source-kind seismosphere_backfill + --source-extra-json. Decide-mode is the
  natural apply mode here (gap-fill: write missing, keep fuller LT, override only
  where the download is materially more complete).

Usage (run where the downloads + an obspy env live, e.g. the Mac kelunji env,
writing to the staging mount):
  miniseed_to_sds.py <inputs...> --sds-out <staging>/seiscomp_archive \
      --select 'DU.ARKL2.00.EHZ' [--remap 'DU.ARKL2.00.HHZ=DU.ARKL2.00.EHZ'] [--dry-run]
Then: apply.py --net DU --sta ARKL2 --mode decide --source-kind seismosphere_backfill ...
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import os
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

from obspy import Stream, UTCDateTime, read

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resample_to_sds import consolidate_overlapping  # noqa: E402  (shared jitter-bridge + dedup)

MSEED_EXTS = (".ms", ".mseed", ".seed", ".miniseed")


def sds_path(root: Path, net, sta, loc, cha, year, doy) -> Path:
    return (root / f"{year}" / net / sta / f"{cha}.D"
            / f"{net}.{sta}.{loc}.{cha}.D.{year}.{doy:03d}")


def _read_any(path: Path) -> Stream:
    """Read MiniSEED from a file, or the .ms member(s) of a .zip, in memory."""
    st = Stream()
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.lower().endswith(MSEED_EXTS):
                        try:
                            st += read(io.BytesIO(z.read(name)))
                        except Exception as e:
                            print(f"  WARN: {path.name}:{name}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  WARN: cannot open zip {path}: {e}", file=sys.stderr)
    else:
        try:
            st += read(str(path))
        except Exception as e:
            print(f"  WARN: cannot read {path}: {e}", file=sys.stderr)
    return st


def gather(inputs) -> Stream:
    """Collect all MiniSEED traces from files / dirs / zips / globs."""
    out = Stream()
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and (f.suffix.lower() in MSEED_EXTS or f.suffix.lower() == ".zip"):
                    out += _read_any(f)
        elif p.exists():
            out += _read_any(p)
        else:
            print(f"  WARN: no such input {p}", file=sys.stderr)
    return out


def apply_remap(tr, remap):
    new = remap.get(tr.id)
    if new:
        net, sta, loc, cha = new.split(".")
        tr.stats.network, tr.stats.station, tr.stats.location, tr.stats.channel = net, sta, loc, cha
    return tr


def day_pieces(tr):
    t, end, delta = tr.stats.starttime, tr.stats.endtime, tr.stats.delta
    while t <= end:
        day0 = UTCDateTime(t.year, t.month, t.day)
        day1 = day0 + 86400
        piece = tr.slice(t, min(end, day1 - delta))
        if piece.stats.npts > 0:
            yield day0.year, day0.julday, piece
        t = day1


def main(argv):
    p = argparse.ArgumentParser(description="Pre-downloaded MiniSEED -> SDS (e.g. Seismosphere backfill)")
    p.add_argument("inputs", nargs="+", help=".ms/.mseed files, dirs, and/or .zip archives")
    p.add_argument("--sds-out", required=True, help="output SDS root (staging tree)")
    p.add_argument("--select", default="*", help="comma-separated NSLC globs to keep (default '*' = all). "
                                                 "e.g. 'DU.ARKL2.00.EHZ' or 'DU.ARKL2.*'")
    p.add_argument("--remap", action="append", default=[], help="OLD_NSLC=NEW_NSLC (repeatable)")
    p.add_argument("--reclen", type=int, default=512)
    p.add_argument("--bridge-gap-s", type=float, default=0.01,
                   help="fill positive-gap holes shorter than this (seconds) -- sub-sample timing "
                        "jitter at EQ-server minute-file boundaries; larger holes stay as real "
                        "gaps. 0 = faithful (bridge nothing). Default 0.01 (10 ms).")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv[1:])

    sds_out = Path(args.sds_out).resolve()
    selects = [s.strip() for s in args.select.split(",") if s.strip()]
    remap = dict(r.split("=", 1) for r in args.remap)

    print(f"inputs: {len(args.inputs)}  select: {selects}  remap: {remap or '(none)'}")
    print(f"out: {sds_out}  dry_run={args.dry_run}")

    raw = gather(args.inputs)
    if not raw:
        print("No MiniSEED read from inputs."); return 2
    # report everything present (so an all-station download is transparent)
    present = sorted({tr.id for tr in raw})
    print(f"read {len(raw)} trace(s), {len(present)} distinct channels across inputs")

    # select + remap
    kept = Stream()
    for tr in raw:
        apply_remap(tr, remap)
        if any(fnmatch.fnmatch(tr.id, pat) for pat in selects):
            kept += tr
    sel_ids = sorted({tr.id for tr in kept})
    print(f"selected: {sel_ids or '(none matched!)'}")
    if not kept:
        print("Nothing matched --select. Present channels were:")
        for c in present:
            print("   ", c)
        return 2

    # Route every record to its SDS day by record start-time. No pre-merge here:
    # the per-day consolidate_overlapping below dedups overlapping downloads,
    # bridges sub-sample minute-jitter, and keeps real gaps. A global
    # merge(method=1) across a channel's fragmented minute-boundary traces would
    # be O(n^2) (the SGWU pathology).
    day_streams = defaultdict(Stream)   # (net,sta,loc,cha,year,doy) -> Stream
    for tr in kept:
        net, sta, loc, cha = tr.stats.network, tr.stats.station, tr.stats.location, tr.stats.channel
        for year, doy, piece in day_pieces(tr):
            day_streams[(net, sta, loc, cha, year, doy)] += piece

    written = 0
    for (net, sta, loc, cha, year, doy), st in sorted(day_streams.items()):
        sr = st[0].stats.sampling_rate
        bridge = int(round(args.bridge_gap_s * sr))
        # grid-consolidate: dedup overlapping downloads, bridge sub-sample
        # minute-boundary jitter (<= bridge samples), keep real gaps as splits.
        final, nbridged, nreal = consolidate_overlapping(st.traces, sr, bridge)
        if not final:
            continue
        for tr in final:
            if tr.data.dtype.kind == "f":
                tr.data = tr.data.astype("int32")
        npts = sum(tr.stats.npts for tr in final)
        span = f"{final[0].stats.starttime} -> {final[-1].stats.endtime}"
        out_path = sds_path(sds_out, net, sta, loc, cha, year, doy)
        tag = (f"{net}.{sta}.{loc}.{cha}.{year}.{doy:03d}: {len(final)} trace(s), {npts:,} samp, "
               f"bridged={nbridged} real_gaps={nreal}  {span}")
        if args.dry_run:
            print(f"  [dry] would write {tag}")
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".partial")
        if tmp.exists():
            tmp.unlink()
        final.write(str(tmp), format="MSEED", encoding="STEIM2", reclen=args.reclen)
        tmp.replace(out_path)
        print(f"  wrote {tag}")
        written += 1

    print(f"\n{'DRY-RUN: ' if args.dry_run else ''}done. "
          f"{'would write' if args.dry_run else 'wrote'} {written or len(day_streams)} day-file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
