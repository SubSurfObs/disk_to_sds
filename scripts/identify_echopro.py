#!/usr/bin/env python3
"""EchoPro USB Stage 0 — identify + register a card.json, the EchoPro analogue
of rename_card.py (which is Gecko-only, needing settings.ss).

EchoPro USBs have NO settings.ss. The ONLY reliable identifier from the data is
the STATION (in filenames + SUDS headers). The manufacturer serial is NOT in the
SUDS files (verified: absent from all block types), so it is NEVER derived or
required here -- card-ids are SPAN-ONLY. This avoids the OUTU failure, where a
card.json was hand-faked from a Gecko template and got the serial / recorder
type / format / span all wrong. See memory echopro-card-id-span-only.

What it records (none invented, none required):
  card_id        = <startdate>-<enddate>          (span only, NO serial suffix)
  net, sta       from filenames / --network override
  data_span      from cont0 day-dirs (date-windowed; pre-GPS-lock junk dropped)
  recorder_type  = "echopro"
  source_format  = "suds"
  digitizer_id   SUDS INSTRUMENT in_serial (UNSIGNED). A stable per-recorder
                 fingerprint, NOT the manufacturer serial. Auto-extracted.
  sensor         from SUDS COMMENT SensorA=, if present.
  serial         ONLY if --serial given (externally known, e.g. chassis
                 sticker); tagged serial_source="external". Optional.

Usage:
  identify_echopro.py <card_root> [--network VW] [--location 00]
      [--ring cont0] [--min-date 2015-01-01] [--max-date YYYY-MM-DD]
      [--serial 75006528] [--cards-root PATH] [--commit] [--no-autocommit]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import echopro_usb_to_sds as epu  # noqa: E402  (reuse discover_days, station_from_files)
import suds_convert as sc  # noqa: E402  (registry lookup)


def extract_digitizer_id_and_sensor(card_root, rings, min_date, max_date):
    """Read one good .dmx via sudspy to get the digitizer_id (unsigned SUDS
    in_serial) + sensor string. Returns (digitizer_id|None, sensor|None).
    Best-effort: any failure returns (None, None) -- identity is station-based,
    these are bonus fingerprints."""
    try:
        import sudspy
    except Exception:
        return None, None
    days, _ = epu.discover_days(card_root, min_date, max_date, rings=rings)
    import shutil
    tried = 0
    # Sample across the whole span (early files can have partial INSTRUMENT
    # blocks with null in_serial), not just the first days. Step through days.
    step = max(1, len(days) // 20)
    for day_dir, _dd in days[::step]:
        files, _ = epu._walk_dmx_tolerant(day_dir)
        nonzero = []
        for f in files:
            try:
                if f.stat().st_size > 0:
                    nonzero.append(f)
            except OSError:
                pass
        for f in nonzero[:5]:  # several files per sampled day
            tried += 1
            if tried > 60:
                return None, None
            try:
                with tempfile.TemporaryDirectory() as td:
                    dst = Path(td) / f.name
                    shutil.copyfile(f, dst)
                    dig = None
                    inst = sudspy.collect_instruments(str(dst))
                    if inst:
                        body = list(inst.values())[0]["struct_body"]
                        raw = body.get("in_serial")
                        if raw is None:
                            raw = body.get("sn_serial")
                        if raw is not None:
                            dig = raw & 0xFFFF  # read UNSIGNED (parser uses <h)
                    sensor = None
                    try:
                        for c in sudspy.collect_comments(str(dst)):
                            txt = c if isinstance(c, str) else str(c)
                            if "SensorA=" in txt:
                                sensor = txt.split("SensorA=", 1)[1].split("\n")[0].strip()
                                break
                    except Exception:
                        pass
                    if dig is not None:
                        return dig, sensor
            except Exception:
                continue
    return None, None


def main(argv):
    p = argparse.ArgumentParser(description="EchoPro USB Stage 0 (identify + register)")
    p.add_argument("card_root", help="USB card root containing LocalArchive/cont*/YYYY/MM/DD")
    p.add_argument("--network", default=None, help="network code (else registry lookup)")
    p.add_argument("--location", default="00", help="location code (default 00)")
    p.add_argument("--ring", default="cont0", help="ring(s) to scan for span (default cont0)")
    p.add_argument("--min-date", default="2015-01-01")
    p.add_argument("--max-date", default=None)
    p.add_argument("--serial", default=None,
                   help="manufacturer serial IF externally known (chassis sticker / "
                        "asset register). NEVER guessed -- it is not in the SUDS data. "
                        "Recorded with serial_source=external.")
    default_reg = str(Path(__file__).resolve().parents[2]
                      / "eqserver_2_seiscomp" / "metadata" / "station_registry.yaml")
    p.add_argument("--registry", default=default_reg)
    default_cards = str(Path(__file__).resolve().parents[2]
                        / "sds_staging_ledger" / "cards")
    p.add_argument("--cards-root", default=default_cards)
    p.add_argument("--commit", action="store_true", help="write card.json (default: dry-run)")
    p.add_argument("--no-autocommit", action="store_true")
    args = p.parse_args(argv[1:])

    card_root = Path(args.card_root)
    la = card_root / "LocalArchive"
    scan_root = la if la.is_dir() else card_root
    min_date = date.fromisoformat(args.min_date)
    max_date = date.fromisoformat(args.max_date) if args.max_date else date.today()
    rings = tuple(r.strip() for r in args.ring.split(",") if r.strip())

    days, skipped = epu.discover_days(scan_root, min_date, max_date, rings=rings)
    if not days:
        print(f"No in-window day-dirs found under {scan_root} (rings {rings}).")
        return 2
    dates = sorted(dd for _p, dd in days)
    start, end = dates[0], dates[-1]

    sta = epu.station_from_files(days[0][0])
    if not sta:
        print("Could not resolve station from filenames.")
        return 2
    net = args.network or sc.network_for_station(sta, args.registry) or "XX"
    card_id = f"{start:%Y%m%d}-{end:%Y%m%d}"  # SPAN ONLY -- no serial suffix

    digitizer_id, sensor = extract_digitizer_id_and_sensor(scan_root, rings, min_date, max_date)

    rec = {
        "card_id": card_id,
        "net": net, "sta": sta, "location": args.location,
        "sampling_rate": 250,
        "recorder_type": "echopro",
        "source_format": "suds",
        "data_span": {"start": start.isoformat(), "end": end.isoformat()},
        "day_count_dirs": len(days),
        "applied_to_lt": False,
        "notes": ("Registered by identify_echopro.py (Stage 0). EchoPro card-id is "
                  "span-only: manufacturer serial is NOT in SUDS data, only station "
                  "is reliably derivable. Pull/apply pending."),
    }
    if digitizer_id is not None:
        rec["digitizer_id"] = str(digitizer_id)
        rec["digitizer_id_note"] = ("SUDS INSTRUMENT in_serial (unsigned); stable "
                                    "per-recorder fingerprint, NOT the manufacturer serial")
    if sensor:
        rec["sensor"] = sensor
    if args.serial:
        rec["serial"] = str(args.serial)
        rec["serial_source"] = "external"
        rec["serial_note"] = ("manufacturer serial from chassis sticker / asset register; "
                              "NOT present in SUDS data")

    out_dir = Path(args.cards_root) / f"{net}.{sta}" / card_id
    card_path = out_dir / "card.json"

    print(f"Station        : {net}.{sta}")
    print(f"Data span      : {start} -> {end}  ({len(days)} day-dirs, {len(skipped)} skipped)")
    print(f"Card id        : {card_id}   (cards/{net}.{sta}/{card_id}/)  [SPAN-ONLY, no serial]")
    print(f"digitizer_id   : {digitizer_id}  (fingerprint, NOT the serial)")
    print(f"sensor         : {sensor or '(none in SUDS COMMENT)'}")
    print(f"serial (extern): {args.serial or '(not provided -- correct unless you have the sticker)'}")
    print()
    if not args.commit:
        print("DRY-RUN -- nothing written. Re-run with --commit.")
        print("Proposed card.json:")
        print(json.dumps(rec, indent=2, sort_keys=True))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    card_path.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {card_path}")

    if not args.no_autocommit:
        try:
            cards_root = Path(args.cards_root)
            sys.path.insert(0, str(cards_root.parent.resolve()))
            from ledger_git import commit_and_push
            commit_and_push(repo_dir=cards_root.parent, paths=[out_dir],
                            message=f"identify (echopro): {net}.{sta} card {card_id} (stage 0)")
        except Exception as e:
            print(f"ledger autocommit skipped ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
