#!/usr/bin/env python3
"""QC-VM resample transform: decimate native high-rate SDS -> lower-rate SDS.

SAFETY CONTRACT
---------------
STRICTLY READ-ONLY against --sds-in (the long-term archive). Writes only under
--sds-out (the staging tree). Output files are built as `<file>.partial` and
atomically renamed. apply.py then places them in LT with provenance.

WHAT IT DOES
------------
Reads native day-files from --sds-in, decimates each trace by an integer factor
(stepwise factor-2 passes, ObsPy's default anti-alias FIR) to --target-sr,
rewrites the band code (e.g. FH? -> CH?), and writes STEIM2 SDS day-files to
--sds-out. It is a QC transform in the staging contract: it never merges records
inside LT -- instead the *script* builds the complete output day-file and
apply.py places it atomically.

Blip preservation + idempotency (merge-in-script): for each output day it folds
in any existing output-band data already in --sds-in (LT). That keeps native
segments that live at the output channel -- e.g. VW.SGWU.00.CHZ.D.2026.277 holds
~8 s of native 500 Hz before the station flipped to 2000 Hz FH at 00:00:08 --
and makes a re-run rebuild the same day-file deterministically. Different sample
rates stay as separate traces (a 500 Hz blip + a 250 Hz body in one day-file).

Two writer requirements handled: ObsPy decimate returns float64 -> cast to int32
before STEIM2; merge(method=0) masks internal gaps -> split() afterwards so no
masked array reaches the writer.

Built for VW.SGWU FH*(2000/1000 Hz) -> CH*(250 Hz) in the 2026-10 Korumburra
aftershock, but parametrised to generalise.

Usage (run on the QC VM, obspy venv):
  ~/venvs/obspy/bin/python resample_to_sds.py --sds-out <staging>/seiscomp_archive \
      --day 2026-10-04 [--dry-run]
  ... --start 2026-10-04 --end 2026-10-08     # inclusive range
  ... (no dates)                              # previous UTC day
Then apply.py --net VW --sta SGWU --source-kind qc_resample ... places it in LT.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from obspy import Stream, Trace, UTCDateTime, read


def day_file(root: Path, net, sta, loc, chan, day: UTCDateTime) -> Path:
    return (root / f"{day.year}" / net / sta / f"{chan}.D"
            / f"{net}.{sta}.{loc}.{chan}.D.{day.year}.{day.julday:03d}")


def stepwise_decimate(tr, target_sr: float):
    """Decimate a Trace to target_sr via successive factor-2 passes (ObsPy
    best practice for large factors), each with the default anti-alias FIR.
    Pure pass-through if already at target. Raises on a non-integer factor."""
    factor = tr.stats.sampling_rate / target_sr
    if abs(factor - 1.0) < 1e-6:
        return tr.copy()
    f_int = int(round(factor))
    if abs(f_int - factor) > 1e-6:
        raise ValueError(f"non-integer decimation {tr.stats.sampling_rate}->{target_sr} on {tr.id}")
    out = tr.copy()
    passes = []
    while f_int > 1:
        if f_int % 2 == 0:
            passes.append(2); f_int //= 2
        else:
            passes.append(f_int); f_int = 1
    for p in passes:
        out.decimate(p, no_filter=False, strict_length=False)
    return out


def consolidate_overlapping(traces, sr, bridge_samples):
    """Grid-place same-rate records (timing-correct) and stitch the sub-sample
    boundary jitter into continuous traces.

    Native high-rate day-files come back from read() as thousands of ~8 s
    records whose stated boundaries carry ~1 ms (±2-sample) GPS-timestamp
    jitter -> thousands of micro over/under-laps. Each record is placed at its
    TRUE sample index (round((start-t0)*sr)); overlaps are overwritten
    (later record wins). Positive-gap holes are then classified: a hole
    <= bridge_samples is the jitter artifact and is filled by linear
    interpolation across its two neighbours; a larger hole is a REAL data gap
    and is left unfilled so the trace splits there. Returns
    (Stream of contiguous traces, n_bridged, n_real_gaps).

    O(total samples); avoids ObsPy merge(method=1), which is pathologically slow
    on ~11k overlapping traces. bridge_samples=0 preserves every hole (faithful).
    """
    traces = sorted((t for t in traces if t.stats.npts), key=lambda t: t.stats.starttime)
    if not traces:
        return Stream(), 0, 0
    dt = 1.0 / sr
    t0 = traces[0].stats.starttime
    proto = traces[0].stats
    dtype = traces[0].data.dtype
    placements, end_idx = [], 0
    for tr in traces:
        i = max(0, int(round((tr.stats.starttime - t0) / dt)))
        placements.append((i, tr.data))
        end_idx = max(end_idx, i + len(tr.data))
    buf = np.zeros(end_idx, dtype=dtype)
    filled = np.zeros(end_idx, dtype=bool)
    for i, data in placements:
        buf[i:i + len(data)] = data
        filled[i:i + len(data)] = True

    def _runs(mask):
        d = np.diff(mask.astype(np.int8))
        starts = list(np.where(d == 1)[0] + 1)
        ends = list(np.where(d == -1)[0] + 1)
        if mask[0]:
            starts = [0] + starts
        if mask[-1]:
            ends = ends + [len(mask)]
        return list(zip(starts, ends))

    n_bridged = n_real = 0
    if not filled.all():
        for s, e in _runs(~filled):
            if 0 < s and e < end_idx and (e - s) <= bridge_samples:   # jitter hole
                buf[s:e] = np.round(np.interp(np.arange(s, e), [s - 1, e],
                                              [float(buf[s - 1]), float(buf[e])])).astype(dtype)
                filled[s:e] = True
                n_bridged += 1
            else:
                n_real += 1

    out = Stream()
    for s, e in _runs(filled):
        tr = Trace(data=buf[s:e].copy())
        tr.stats.network, tr.stats.station = proto.network, proto.station
        tr.stats.location, tr.stats.channel = proto.location, proto.channel
        tr.stats.sampling_rate = sr
        tr.stats.starttime = t0 + s * dt
        out.append(tr)
    return out, n_bridged, n_real


def build_day_channel(day, comp, args) -> tuple[Stream, Path, dict]:
    """Return (final_stream, out_path, info) for one (day, component), or
    (None, out_path, info) if there's no native input for it."""
    net, sta, loc = args.net, args.sta, args.loc
    src_chan = args.src_band + comp
    out_chan = args.out_band + comp
    sds_in, sds_out = Path(args.sds_in), Path(args.sds_out)
    out_path = day_file(sds_out, net, sta, loc, out_chan, day)
    info = {"src": src_chan, "out": out_chan}

    src = day_file(sds_in, net, sta, loc, src_chan, day)
    if not src.exists():
        info["status"] = "no native input"
        return None, out_path, info

    # Consolidate native FIRST (fast numpy stitch of the ~1 ms sub-sample
    # record-boundary overlaps -> continuous segments, split only at real
    # gaps), THEN decimate each continuous segment. The 2000->1000 split day
    # carries both native rates, so group by native sampling rate and decimate
    # each group (factor 8 and 4 respectively) down to the target.
    native = read(str(src))
    by_native_sr = defaultdict(list)
    for tr in native:
        by_native_sr[tr.stats.sampling_rate].append(tr)
    decimated = Stream()
    n_bridged = n_real = 0
    for nsr, trs in by_native_sr.items():
        bridge = int(round(args.bridge_gap_s * nsr))
        segs, nb, nr = consolidate_overlapping(trs, nsr, bridge)
        n_bridged += nb
        n_real += nr
        for seg in segs:
            decimated += stepwise_decimate(seg, args.target_sr)
    for tr in decimated:
        tr.stats.network, tr.stats.station = net, sta
        tr.stats.location, tr.stats.channel = loc, out_chan
        if tr.data.dtype.kind == "f":
            tr.data = tr.data.astype("int32")   # STEIM2 needs int32; decimate gives float64

    # Preserve any native data already at the output channel that is NOT at the
    # target rate (e.g. the 500 Hz day-277 blip) as its own trace. Same-rate
    # existing content is regenerated deterministically from native, so we don't
    # re-read it -- that keeps re-runs idempotent and avoids a slow cross-run merge.
    final = decimated
    folded = False
    existing_lt = day_file(sds_in, net, sta, loc, out_chan, day)
    if existing_lt.exists():
        for tr in read(str(existing_lt)):
            if abs(tr.stats.sampling_rate - args.target_sr) > 1e-6:
                if tr.data.dtype.kind == "f":
                    tr.data = tr.data.astype("int32")
                final += tr
                folded = True

    rates = sorted({tr.stats.sampling_rate for tr in final})
    info.update(status="ok", folded_existing_lt=folded, traces=len(final),
                npts=sum(tr.stats.npts for tr in final), rates=rates,
                bridged=n_bridged, real_gaps=n_real)
    return final, out_path, info


def process_day(day, args) -> int:
    print(f"\n--- {day.date} (doy {day.julday:03d}) ---")
    if day + 86400 <= args.window_start:
        print(f"  skip: before window-start {args.window_start}")
        return 0
    written = 0
    for comp in args.comps:
        try:
            final, out_path, info = build_day_channel(day, comp, args)
        except Exception as exc:
            print(f"  ERROR {args.src_band}{comp}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if final is None:
            print(f"  {info['src']} -> {info['out']}: {info['status']}")
            continue
        tag = (f"{info['out']}: {info['traces']} trace(s), {info['npts']:,} samp, "
               f"rates={info['rates']}, bridged={info['bridged']} real_gaps={info['real_gaps']}"
               + ("  (+folded LT blip)" if info["folded_existing_lt"] else ""))
        if args.dry_run:
            print(f"  [dry] would write {out_path.name}  {tag}")
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".partial")
        if tmp.exists():
            tmp.unlink()
        final.write(str(tmp), format="MSEED", encoding="STEIM2", reclen=args.reclen)
        tmp.replace(out_path)
        print(f"  wrote {out_path.name}  {tag}")
        written += 1
    return written


def main(argv):
    p = argparse.ArgumentParser(description="Decimate native SDS -> lower-rate SDS (QC transform)")
    p.add_argument("--sds-in", default="/mnt/seiscomp_archive", help="source SDS (read-only; LT)")
    p.add_argument("--sds-out", required=True, help="output SDS root (staging tree)")
    p.add_argument("--net", default="VW"); p.add_argument("--sta", default="SGWU")
    p.add_argument("--loc", default="00")
    p.add_argument("--src-band", default="FH", help="source 2-char band code (e.g. FH)")
    p.add_argument("--out-band", default="CH", help="output 2-char band code (e.g. CH)")
    p.add_argument("--comps", default="ZNE", help="components to process (default ZNE)")
    p.add_argument("--target-sr", type=float, default=250.0)
    p.add_argument("--bridge-gap-s", type=float, default=0.002,
                   help="fill positive-gap holes shorter than this (seconds) -- the "
                        "sub-sample GPS-timestamp jitter at native record boundaries; "
                        "larger holes stay as real gaps. 0 = faithful (bridge nothing). "
                        "Default 0.002 (2 ms; native jitter is ~1.1 ms).")
    p.add_argument("--window-start", default="2026-10-04T00:00:00",
                   help="do not resample native data before this UTC time")
    p.add_argument("--reclen", type=int, default=512)
    p.add_argument("--start", help="UTC date YYYY-MM-DD")
    p.add_argument("--end", help="UTC date YYYY-MM-DD (inclusive)")
    p.add_argument("--day", help="single UTC date YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv[1:])
    args.comps = list(args.comps)
    args.window_start = UTCDateTime(args.window_start)

    if Path(args.sds_out).resolve() == Path(args.sds_in).resolve():
        print("ERROR: --sds-out must differ from --sds-in"); return 2

    if args.day:
        start = end = UTCDateTime(args.day)
    elif args.start:
        start = UTCDateTime(args.start)
        end = UTCDateTime(args.end) if args.end else start
    else:
        y = UTCDateTime() - 86400
        start = end = UTCDateTime(y.year, y.month, y.day)

    print(f"resample {args.net}.{args.sta}.{args.loc} {args.src_band}* -> {args.out_band}* @ {args.target_sr} Hz")
    print(f"in : {args.sds_in}  (read-only)\nout: {args.sds_out}")
    print(f"window: {start.date} -> {end.date}   window-start={args.window_start}   dry_run={args.dry_run}")
    total = 0
    d = UTCDateTime(start.year, start.month, start.day)
    end_floor = UTCDateTime(end.year, end.month, end.day)
    while d <= end_floor:
        total += process_day(d, args)
        d += 86400
    print(f"\ndone. wrote {total} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
