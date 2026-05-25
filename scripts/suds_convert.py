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
    rate = None
    for f in files:
        try:
            raw = sudspy.read_suds_stream(str(f))
        except Exception as e:
            read_errors.append((str(f), f"{type(e).__name__}: {e}"))
            continue
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
          "rate_hz": rate, "n_traces": len(out)}
    return out, qc


def _sds_day_path(sds_root, tr) -> Path:
    s, t = tr.stats, tr.stats.starttime
    return (Path(sds_root) / f"{t.year:04d}" / s.network / s.station /
            f"{s.channel}.D" /
            f"{s.network}.{s.station}.{s.location}.{s.channel}.D.{t.year:04d}.{t.julday:03d}")


def write_sds(stream, sds_root, encoding="STEIM2", reclen=512):
    """Write each (channel, day) file atomically (.partial -> os.replace).

    STEIM2 needs integer samples; SUDS data are digitiser counts, so cast to
    int32 if needed (lossless for counts).
    """
    from obspy import Stream
    groups: dict = defaultdict(list)
    for tr in stream:
        groups[_sds_day_path(sds_root, tr)].append(tr)

    written = []
    for path, trs in groups.items():
        s = Stream(trs)
        s.sort(["starttime"])
        if encoding in ("STEIM1", "STEIM2"):
            for tr in s:
                if not np.issubdtype(tr.data.dtype, np.integer):
                    tr.data = tr.data.astype("int32")
                elif tr.data.dtype != np.int32:
                    tr.data = tr.data.astype("int32")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".partial")
        s.write(str(tmp), format="MSEED", encoding=encoding, reclen=reclen)
        os.replace(tmp, path)
        written.append((path, path.stat().st_size))
    return written
