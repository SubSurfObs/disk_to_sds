#!/usr/bin/env python3
"""Regression test for the midnight-boundary data-loss bug in write_sds.

THE BUG (discovered 2026-06-02, eqserver_2_seiscomp sweep): SRC minute-files
are named by their START time + a constant SS seconds offset, so the file
named `..._2359_12_...` actually starts at 23:59:12 and runs 60 s -> into the
NEXT day's 00:00:12. The old write_sds routed each whole trace by its
starttime's calendar day, so:
  - day N's job wrote the post-midnight sliver into day N's file (wrong day), or
  - day N+1's job clobbered the sliver day N had correctly placed in N+1.
Either way the first ~SS seconds of every day were lost. ~3.65 hr/station-year
at SS=12.

THE FIX: write_sds now (1) SPLITS every trace at UTC day boundaries so samples
land in the day they belong to, and (2) MERGES-ON-WRITE so an existing day-file
(from the prior day's straddle, or a resume) is read + combined + deduped
rather than overwritten.

This test is the tripwire that catches a future refactor silently
reintroducing clobber/misfile. Run:
    <python-with-obspy> scripts/test_boundary_write_sds.py
Exits 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import suds_convert as sc  # noqa: E402


def _make_trace(net, sta, loc, cha, start, npts, rate=250.0, val0=0):
    from obspy import Trace, UTCDateTime
    tr = Trace(data=np.arange(val0, val0 + npts, dtype="int32"))
    tr.stats.network, tr.stats.station = net, sta
    tr.stats.location, tr.stats.channel = loc, cha
    tr.stats.sampling_rate = rate
    tr.stats.starttime = UTCDateTime(start)
    return tr


def _read_day(root, net, sta, loc, cha, year, julday):
    from obspy import read as read_mseed
    p = sc._sds_day_path_for(root, net, sta, loc, cha, year, julday)
    if not p.exists():
        return None
    return read_mseed(str(p))


def _fail(msg):
    print(f"  FAIL: {msg}")
    return False


def test_straddle_split():
    """A trace starting 23:59:12 of day N spanning 60 s must put the post-
    midnight samples into day N+1, and day N+1's file must begin at 00:00:00."""
    from obspy import UTCDateTime
    N, S, L, C, R = "VW", "BRIG", "00", "CHZ", 250.0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # 23:59:12.000 for 60 s @ 250 Hz = 15000 samples, ending 00:00:11.996
        start = "2023-01-15T23:59:12.000000Z"
        npts = int(60 * R)
        sc.write_sds([_make_trace(N, S, L, C, start, npts, R)], root)

        d15 = _read_day(root, N, S, L, C, 2023, 15)
        d16 = _read_day(root, N, S, L, C, 2023, 16)
        if d15 is None:
            return _fail("day 015 file missing")
        if d16 is None:
            return _fail("day 016 file missing -- post-midnight sliver lost (THE BUG)")
        # day 16 must start at exactly 00:00:00.000
        t16 = d16[0].stats.starttime
        if not (t16.hour == 0 and t16.minute == 0 and t16.second == 0
                and t16.microsecond == 0):
            return _fail(f"day 016 starts at {t16}, expected 00:00:00.000000")
        # day 15's last sample must be < 00:00:00 of day 16
        if d15[-1].stats.endtime >= UTCDateTime("2023-01-16T00:00:00.000000Z"):
            return _fail(f"day 015 endtime {d15[-1].stats.endtime} leaks into day 016")
        # total samples conserved (no loss, no dup)
        tot = sum(tr.stats.npts for tr in d15) + sum(tr.stats.npts for tr in d16)
        if tot != npts:
            return _fail(f"sample count {tot} != input {npts} (loss or dup)")
        print("  ok: straddle split -- day016 starts 00:00:00, no loss/dup")
        return True


def test_merge_on_write_accumulates():
    """Simulate two adjacent day-jobs hitting day N+1's file: day N's straddle
    writes the opening sliver, then day N+1's own job must ADD to it, not
    clobber it."""
    N, S, L, C, R = "VW", "BRIG", "00", "CHZ", 250.0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # Job 1: day-15 straddle file -> deposits 00:00:00..00:00:11.996 into day16
        sc.write_sds([_make_trace(N, S, L, C, "2023-01-15T23:59:12.000000Z",
                                  int(60 * R), R, val0=0)], root)
        d16_after1 = _read_day(root, N, S, L, C, 2023, 16)
        sliver = sum(tr.stats.npts for tr in d16_after1)
        # Job 2: day-16's own 0000_12 file -> starts 00:00:12 for the rest of day
        sc.write_sds([_make_trace(N, S, L, C, "2023-01-16T00:00:12.000000Z",
                                  int(60 * R), R, val0=10000)], root)
        d16_after2 = _read_day(root, N, S, L, C, 2023, 16)
        if d16_after2 is None:
            return _fail("day 016 vanished after second write")
        total = sum(tr.stats.npts for tr in d16_after2)
        # Must contain BOTH the sliver AND the new minute (allowing for the
        # 4 ms inter-file gap 00:00:11.996->00:00:12.000 = no overlap).
        if total <= sliver:
            return _fail(f"merge-on-write clobbered: {total} samples <= sliver "
                         f"{sliver} (day-16 job overwrote day-15's sliver)")
        if d16_after2[0].stats.starttime.second != 0:
            return _fail(f"day016 no longer starts at 00:00:00 after merge")
        print(f"  ok: merge-on-write -- sliver {sliver} + new minute = "
              f"{total} samples preserved (no clobber)")
        return True


def test_idempotent_rerun():
    """Re-running the exact same input must not duplicate samples (resume
    safety: cleanup compares staged vs LT)."""
    N, S, L, C, R = "VW", "BRIG", "00", "CHZ", 250.0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tr_args = (N, S, L, C, "2023-01-16T00:00:12.000000Z", int(60 * R), R)
        sc.write_sds([_make_trace(*tr_args)], root)
        n1 = sum(tr.stats.npts for tr in _read_day(root, N, S, L, C, 2023, 16))
        sc.write_sds([_make_trace(*tr_args)], root)  # same data again
        n2 = sum(tr.stats.npts for tr in _read_day(root, N, S, L, C, 2023, 16))
        if n1 != n2:
            return _fail(f"re-run changed sample count {n1} -> {n2} (dedup failed)")
        print(f"  ok: idempotent re-run -- {n1} samples stable across re-run")
        return True


def main():
    try:
        import obspy  # noqa: F401
    except ImportError:
        print("SKIP: obspy not importable in this interpreter")
        return 0
    print("boundary write_sds regression tests:")
    results = [test_straddle_split(),
               test_merge_on_write_accumulates(),
               test_idempotent_rerun()]
    ok = all(results)
    print(f"\n{'PASS' if ok else 'FAIL'}: {sum(results)}/{len(results)} tests")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
