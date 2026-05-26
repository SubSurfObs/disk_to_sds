#!/usr/bin/env python3
"""
rename_card.py — identify a Gecko SD card and (safely) rename its macOS volume
to a label that maps to the ledger card-id, then register it in the ledger.

This is the optional FIRST step when a card comes back from the field: it turns
an anonymous "NO NAME" volume into something you can recognise later — crucially,
before the destructive "wipe and reuse" step, so you can confirm against the
ledger which card you're about to erase.

SAFETY
  - A volume rename is metadata-only (it rewrites the volume label, not the data)
    and is reversible. This script NEVER writes to or deletes card data.
  - It refuses to rename anything that isn't a confirmed Gecko card: the volume
    MUST contain `settings.ss` and a `data/<YYYY>/...` tree. A system/boot volume
    can't match, so it can't be touched.
  - Default DRY-RUN. `--commit` performs the `diskutil rename` (+ card.json write).
  - After renaming it re-verifies the data tree is still present.

LABEL (filesystem-aware)
  - FAT32 labels are capped at 11 chars, uppercase, alphanumeric — too short for
    the full card-id. There we use STATION + start-date: e.g. `MARD250704`
    (unique across card swaps from the same recorder; resolves to the ledger).
  - exFAT allows long labels, so there we use the full `STA_<card-id>`, e.g.
    `MARD_20250704-20260519_0487`.
  The full ledger card-id is always `<startYYYYMMDD>-<endYYYYMMDD>_<serial4>`
  (station is the ledger parent dir); the volume label is recorded in card.json
  so the physical-card <-> ledger mapping is explicit.

Usage:
    rename_card.py [VOLUME] [--cards-root PATH] [--commit]
        VOLUME       /Volumes/<name> (auto-detected if exactly one Gecko card)
        --cards-root ledger cards/ dir to register card.json into
                     (default: ../../sds_staging_ledger/cards relative to this repo)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

SETTING_RE = re.compile(r'"(\w+)"\s*=\s*"?([^"\n]*?)"?\s*$')
DAY_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})$")


def find_gecko_volumes() -> list[Path]:
    vols = []
    for v in Path("/Volumes").iterdir():
        if (v / "settings.ss").is_file() and (v / "data").is_dir():
            # require at least one YYYY year dir under data/ to be sure
            if any(d.is_dir() and d.name.isdigit() and len(d.name) == 4
                   for d in (v / "data").iterdir()):
                vols.append(v)
    return vols


def read_settings(vol: Path) -> dict:
    out = {}
    for line in (vol / "settings.ss").read_text(errors="replace").splitlines():
        m = SETTING_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def scan_date_range(vol: Path, min_date: date, max_date: date) -> tuple[date, date]:
    days = []
    for d in (vol / "data").glob("*/*/*"):
        m = DAY_RE.search(str(d))
        if not (m and d.is_dir()):
            continue
        try:
            dd = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        # Ignore pre-GPS-lock bootup dates (recorder default clock, e.g. 2012)
        # and clock-glitch future dates so the card-id/label use the real span.
        if min_date <= dd <= max_date:
            days.append(dd)
    if not days:
        raise ValueError(
            f"no data/YYYY/MM/DD day directories within [{min_date} .. {max_date}] "
            "(all filtered as pre-GPS-lock / clock-glitch dates?)")
    return min(days), max(days)


def get_fs(vol: Path) -> str:
    """Return 'fat32' | 'exfat' | 'unknown' from diskutil."""
    try:
        out = subprocess.run(["diskutil", "info", str(vol)],
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    for line in out.splitlines():
        if "File System Personality" in line:
            v = line.split(":", 1)[1].strip().lower()
            if "fat32" in v or "fat16" in v or "ms-dos" in v:
                return "fat32"
            if "exfat" in v:
                return "exfat"
    return "unknown"


def fat32_label(sta: str, start: date) -> str:
    raw = f"{sta}{start:%y%m%d}".upper()
    raw = re.sub(r"[^A-Z0-9]", "", raw)
    return raw[:11]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Identify + safely rename a Gecko SD card volume")
    p.add_argument("volume", nargs="?", help="/Volumes/<name> (auto-detect if omitted)")
    default_cards = (Path(__file__).resolve().parents[2]
                     / "sds_staging_ledger" / "cards")
    p.add_argument("--cards-root", default=str(default_cards),
                   help=f"ledger cards/ dir for card.json (default: {default_cards})")
    p.add_argument("--commit", action="store_true",
                   help="actually rename the volume + write card.json (default: dry-run)")
    p.add_argument("--min-date", default="2015-01-01",
                   help="ignore day-dirs before this when computing the data span "
                        "(drops pre-GPS-lock bootup dates like 2012-01-01). "
                        "Default 2015-01-01.")
    p.add_argument("--max-date", default=None,
                   help="ignore day-dirs after this. Default: today.")
    p.add_argument("--network", default=None,
                   help="override the network code from settings.ss (e.g. VW). "
                        "Use for cards that predate the FDSN VW assignment — "
                        "the settings.ss network is then obsolete.")
    p.add_argument("--no-settings-history", action="store_true",
                   help="don't extract the per-card settings epoch + change "
                        "history (default: extract; reads ~6k hourly .ss files "
                        "off the card, can take a few minutes on slow media).")
    args = p.parse_args(argv[1:])

    # Resolve volume.
    if args.volume:
        vol = Path(args.volume)
    else:
        found = find_gecko_volumes()
        if len(found) == 0:
            print("No Gecko card found under /Volumes (need settings.ss + data/YYYY).")
            return 2
        if len(found) > 1:
            print("Multiple Gecko cards mounted; specify which:")
            for f in found:
                print(f"  {f}")
            return 2
        vol = found[0]

    # GUARD: must be a confirmed Gecko card under /Volumes, not the boot disk.
    if vol.resolve() == Path("/") or "Macintosh HD" in str(vol):
        print(f"REFUSING: {vol} looks like a system volume.")
        return 2
    if not ((vol / "settings.ss").is_file() and (vol / "data").is_dir()):
        print(f"REFUSING: {vol} is not a Gecko card (no settings.ss + data/).")
        return 2

    s = read_settings(vol)
    sta = s.get("sitename", "").strip()
    net_in_ss = s.get("network_code", "").strip()
    net = args.network.strip() if args.network else net_in_ss
    loc = s.get("location_id", "").strip()
    serial = s.get("serial", "").strip()
    rate = s.get("sampling_rate", "").strip()
    if not (sta and serial):
        print(f"REFUSING: settings.ss missing sitename/serial (sta={sta!r} serial={serial!r}).")
        return 2
    serial4 = serial[-4:]
    min_date = date.fromisoformat(args.min_date)
    max_date = date.fromisoformat(args.max_date) if args.max_date else date.today()
    start, end = scan_date_range(vol, min_date, max_date)
    fs = get_fs(vol)

    card_id = f"{start:%Y%m%d}-{end:%Y%m%d}_{serial4}"
    if fs == "exfat":
        label, label_kind = f"{sta}_{card_id}", "exfat (full)"
    else:  # fat32 or unknown -> use the safe short form
        label, label_kind = fat32_label(sta, start), f"{fs} (short)"

    cur_name = vol.name
    print(f"Volume        : {vol}  (current label: {cur_name!r})")
    print(f"Filesystem    : {fs}")
    net_note = "" if (not args.network or net == net_in_ss) else f"  [override; settings.ss said {net_in_ss!r}]"
    print(f"Station/serial : {net}.{sta}  serial {serial} ({serial4}), {rate} Hz, loc {loc}{net_note}")
    print(f"Data span     : {start} -> {end}")
    print(f"Ledger card-id : {card_id}   (cards/{net}.{sta}/{card_id}/)")
    print(f"New volume label: {label!r}   [{label_kind}]")
    print()

    if not args.commit:
        print("DRY-RUN — nothing changed. Re-run with --commit to rename + register.")
        return 0

    # Rename (metadata only).
    if cur_name == label:
        print("Volume already has this label; skipping rename.")
    else:
        r = subprocess.run(["diskutil", "rename", str(vol), label],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"RENAME FAILED: {r.stderr.strip() or r.stdout.strip()}")
            return 1
        print(f"Renamed: {cur_name!r} -> {label!r}")
        # macOS remounts under the new name.
        vol = Path("/Volumes") / label

    # Re-verify data intact (read-only check).
    if (vol / "settings.ss").is_file() and (vol / "data").is_dir():
        print("Verified: settings.ss + data/ still present after rename.")
    else:
        print("WARNING: could not re-verify data tree at new mount path — check manually.")

    # Register / update card.json in the ledger.
    cards_root = Path(args.cards_root)
    if cards_root.is_dir() or cards_root.parent.is_dir():
        card_dir = cards_root / f"{net}.{sta}" / card_id
        card_path = card_dir / "card.json"
        if card_path.exists():
            rec = json.loads(card_path.read_text())
            rec["volume_label"] = label
            print(f"Updated volume_label in existing {card_path}")
        else:
            rec = {
                "card_id": card_id,
                "net": net, "sta": sta, "location": loc,
                "sampling_rate": int(rate) if rate.isdigit() else rate,
                "recorder_type": "gecko",
                "source_format": "miniseed",
                "serial": serial,
                "data_span": {"start": start.isoformat(), "end": end.isoformat()},
                "volume_label": label,
                "applied_to_lt": False,
                "notes": "Registered by rename_card.py at field-return; pull/apply pending.",
            }
            if args.network and net != net_in_ss:
                rec["net_override"] = {"from_settings_ss": net_in_ss, "applied": net}
            print(f"Created {card_path}")
        card_dir.mkdir(parents=True, exist_ok=True)
        card_path.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    else:
        print(f"(--cards-root {cards_root} not found; skipped card.json registration)")

    # Settings-history extraction: epoch + non-volatile config diffs + final
    # snapshot, into the same card dir. Reads ~6k hourly .ss files; the cost
    # is unavoidable but happens only once at field-return. Failure here must
    # not unwind the rename/card.json work above.
    if not args.no_settings_history:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import extract_card_settings as ecs
            print()
            ecs.extract(vol, Path(args.cards_root), set(ecs.VOLATILE_FIELDS),
                        commit=True, network_override=args.network)
        except Exception as e:
            print(f"settings-history: extraction skipped ({type(e).__name__}: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
