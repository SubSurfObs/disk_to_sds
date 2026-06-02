#!/usr/bin/env python3
"""Reusable PC-SUDS -> miniSEED SDS conversion for EchoPro data.

Used by the disk_to_sds EchoPro USB adapter and (intended) by
eqserver_2_seiscomp for the historical-archive conversion. Depends on
sudspy + obspy. Pure functions so both pipelines share one mapping.

SUDS carries no network code and wrong channel codes, so we remap:
  network  <- eqserver_2_seiscomp station_registry.yaml (target_network), or override
  location <- "00"
  channel  <- FDSN inventory for that station + sample-rate if available,
              else SEED band-code-by-rate fallback (matches what the Gecko
              writes for the same rate). Orientation from the EchoPro component:
                c01 -> N (longitudinal), c02 -> E (transverse), c03 -> Z (vertical)
              c04 (microphone) and anything else are dropped.
"""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

import numpy as np
import sudspy

# EchoPro component -> SEED orientation (Kelunji EchoPro user manual)
ECHOPRO_ORIENT = {"c01": "N", "c02": "E", "c03": "Z"}  # c04 = microphone -> excluded
INSTRUMENT = "H"  # high-gain seismometer


def seed_band(rate_hz: float) -> str:
    """SEED band code for a sample rate (broadband side, matching the Gecko)."""
    r = float(rate_hz)
    if r >= 1000: return "F"
    if r >= 250:  return "C"   # 250 sps -> CHZ (Gecko convention)
    if r >= 80:   return "H"   # 100 sps -> HHZ
    if r >= 10:   return "B"
    if r >= 1:    return "L"
    if r >= 0.1:  return "V"
    return "U"


def network_for_station(station: str, registry_path) -> str | None:
    """target_network from station_registry.yaml, or None if absent/not-included."""
    import yaml
    reg = yaml.safe_load(Path(registry_path).read_text()) or {}
    rec = reg.get(station)
    if not rec or not rec.get("include", False):
        return None
    return rec.get("target_network")


def channel_for(orientation: str, rate_hz: float, inv=None,
                network=None, station=None) -> tuple[str, str]:
    """(channel, location): FDSN inventory match first, else band-code fallback."""
    if inv is not None and network and station:
        try:
            sel = inv.select(network=network, station=station)
            for net in sel.networks:
                for sta in net.stations:
                    for ch in sta.channels:
                        if ch.code[-1] == orientation and \
                           abs((ch.sample_rate or 0.0) - rate_hz) < 1e-6:
                            return ch.code, (ch.location_code or "")
        except Exception:
            pass
    return seed_band(rate_hz) + INSTRUMENT + orientation, "00"


def convert_suds_files(files, network, station, inv=None):
    """Read SUDS files -> obspy Stream remapped to SEED ids.

    Reads per file (current sudspy takes a single path), so a genuinely
    unreadable file is caught and recorded rather than aborting the batch. With
    copy-local-first the inputs are clean, so read_errors is normally empty.
    Returns (stream, qc); qc has read_errors, dropped_components, rate_hz,
    n_traces -- treat any non-empty read_errors as a day-level QC flag.
    """
    from obspy import Stream
    out = Stream()
    dropped: set = set()
    read_errors: list = []
    # Recovered-but-incomplete files: sudspy now reads tolerantly (strict=False),
    # recovering valid waveform channels and stopping at trailing junk / genuine
    # truncation. We capture the per-file stop diagnostics so the day-level QC can
    # tell "trailing junk, fully recovered" (bad_sync, small trailing) from
    # "truncated mid-data, partial recovery" (short_data) — the latter is real
    # data loss worth flagging.
    recovered: list = []
    rate = None
    for f in files:
        diag: dict = {}
        try:
            raw = sudspy.read_suds_stream(str(f), diag=diag)
        except Exception as e:
            read_errors.append((str(f), f"{type(e).__name__}: {e}"))
            continue
        if diag.get("stop_reason") not in (None, "clean_eof"):
            recovered.append((str(f), diag.get("stop_reason"),
                              diag.get("n_blocks"), diag.get("last_good_offset")))
        for tr in raw:
            orient = ECHOPRO_ORIENT.get(tr.stats.channel)
            if orient is None:
                dropped.add(tr.stats.channel)
                continue
            rate = tr.stats.sampling_rate
            cha, loc = channel_for(orient, rate, inv=inv, network=network, station=station)
            tr.stats.network = network
            tr.stats.station = station
            tr.stats.location = loc
            tr.stats.channel = cha
            out += tr
    qc = {"read_errors": read_errors, "dropped_components": sorted(dropped),
          "rate_hz": rate, "n_traces": len(out),
          "recovered_files": recovered,
          "n_recovered": len(recovered)}
    return out, qc


def _sds_day_path_for(sds_root, net, sta, loc, cha, year, julday) -> Path:
    return (Path(sds_root) / f"{year:04d}" / net / sta / f"{cha}.D" /
            f"{net}.{sta}.{loc}.{cha}.D.{year:04d}.{julday:03d}")


def _split_trace_by_utc_day(tr):
    """Yield (year, julday, sub-trace) for each UTC calendar day the trace
    spans. A minute-file that straddles midnight (recorder SS offset > 0)
    produces samples on BOTH days; this routes each sample to the day it
    actually belongs to, instead of filing the whole trace under its
    starttime's day. The post-midnight sliver therefore lands in the NEXT
    day's file -- the fix for the midnight-boundary data loss.

    Slicing is half-open [day_start, next_day_start): the sample at exactly
    00:00:00 of the next day belongs to the next day (matches scart /
    slarchive convention and eqserver's _trim_to_day epsilon)."""
    from obspy import UTCDateTime
    start, end = tr.stats.starttime, tr.stats.endtime
    day = UTCDateTime(start.year, start.month, start.day)
    while day <= end:
        nxt = day + 86400
        # half-open: end at nxt minus one sample so the 00:00:00 sample of the
        # next day is NOT included here (it's emitted on the next iteration).
        piece = tr.slice(starttime=max(start, day),
                         endtime=min(end, nxt - tr.stats.delta),
                         nearest_sample=False)
        if piece.stats.npts > 0:
            yield piece.stats.starttime.year, piece.stats.starttime.julday, piece
        day = nxt


def write_sds(stream, sds_root, encoding="STEIM2", reclen=512):
    """Write each (channel, UTC-day) SDS file with day-boundary splitting and
    merge-on-write, atomically (.partial -> os.replace).

    Two behaviours that together fix the midnight-boundary loss:
      1. SPLIT every trace at UTC calendar-day boundaries, so a minute-file
         straddling midnight contributes its post-midnight samples to the NEXT
         day's file (not the whole trace to the starttime's day).
      2. MERGE-ON-WRITE: if a target day-file already exists (because the
         previous day's straddling minute-file already deposited the opening
         sliver, or this is a resume/re-run), read it, combine, dedup
         overlapping samples, and rewrite -- never clobber. Idempotent:
         re-running with the same input yields the same samples.

    STEIM2 needs integer samples; SUDS data are digitiser counts, so cast to
    int32 if needed (lossless for counts).
    """
    from obspy import Stream, read as read_mseed

    def _to_int32(s):
        if encoding in ("STEIM1", "STEIM2"):
            for tr in s:
                if tr.data.dtype != np.int32:
                    tr.data = tr.data.astype("int32")
        return s

    # Group split sub-traces by their true (year, julday) target file.
    groups: dict = defaultdict(list)
    key_meta: dict = {}
    for tr in stream:
        s = tr.stats
        for year, julday, piece in _split_trace_by_utc_day(tr):
            path = _sds_day_path_for(sds_root, s.network, s.station, s.location,
                                     s.channel, year, julday)
            groups[path].append(piece)
            key_meta[path] = (year, julday)

    written = []
    for path, trs in groups.items():
        s = Stream(trs)
        # Merge-on-write: fold in whatever is already filed for this day.
        if path.exists() and path.stat().st_size > 0:
            try:
                s += read_mseed(str(path), format="MSEED")
            except Exception as e:
                # Don't lose new data because an existing file is unreadable;
                # preserve the suspect file for inspection rather than clobber.
                bad = path.with_suffix(path.suffix + f".corrupt")
                os.replace(path, bad)
                print(f"  write_sds: existing {path.name} unreadable ({e}); "
                      f"moved to {bad.name}, writing fresh from new data")
        _to_int32(s)
        # method=1: where samples overlap (the re-run / boundary case), keep one
        # copy -- deduplicates the straddle sliver instead of doubling it.
        s.merge(method=1)
        s = s.split()           # break any gaps back into discrete traces
        s.sort(["starttime"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".partial")
        _to_int32(s)
        s.write(str(tmp), format="MSEED", encoding=encoding, reclen=reclen)
        os.replace(tmp, path)
        written.append((path, path.stat().st_size))
    return written
