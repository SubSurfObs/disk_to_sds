#!/usr/bin/env python3
"""Extract per-card Gecko settings epoch + config-change history.

A Gecko writes a `settings.ss` snapshot into every hourly data dir
(`data/YYYY/MM/DD/HH/<date> <HHMM> SS <STA>.ss`). Storing all 6000+ per
244-day card would be a telemetry log; what we actually want is the
configuration history -- the rare changes in non-volatile fields.

Output into the ledger card dir:
  cards/<NET>.<STA>/<card-id>/
    settings_epoch.ss         # first hourly snapshot, verbatim
    settings_changes.jsonl    # one line per config-field change
    settings_final.ss         # last hourly snapshot, verbatim

`settings_changes.jsonl` is the authoritative per-card config history. Lines
look like:
  {"at": "<settings_time of the new snapshot>",
   "source": "data/.../<file>.ss",
   "changes": {"current_gain": ["x1", "x2"], "sampling_rate": [250, 100]}}

Volatile fields (per-hour telemetry: GPS, supply, temperature, free space,
the auto-advancing settings_time) are excluded from the diff -- they would
generate one entry per file and tell us nothing about configuration. See
VOLATILE_FIELDS below.

Usage:
  extract_card_settings.py <card_root>
      [--cards-root PATH] [--commit] [--network VW]
      [--volatile-extra field1,field2,...]

`<card_root>` is the SD card mount (e.g. /Volumes/WLSH241103) OR a local
copy of its `data/` subtree (so we can run after the card is unplugged).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

# Fields that change every hour and tell us nothing about CONFIG. Excluded
# from the diff. Anything not in this set is considered config and triggers
# a change-event when it differs from the running snapshot.
VOLATILE_FIELDS = {
    "settings_time",      # auto-advances even when nothing else changes
    "long", "lat", "alt", "sats",   # GPS telemetry
    "v_supply", "temperature",      # environmental
    "card_free_pc",                 # storage usage drift
}

SS_LINE = re.compile(r'^"(?P<key>[^"]+)"\s*=\s*(?P<val>.*?)\s*$')


def parse_ss(text: str) -> dict[str, str]:
    """Parse a Gecko .ss file into {key: raw_value_string}. Preserves
    quotes/comments verbatim in the value so the diff matches the source."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = SS_LINE.match(line.strip())
        if m:
            out[m.group("key")] = m.group("val")
    return out


def find_hourly_ss_files(root: Path) -> list[Path]:
    """Find all hourly Gecko .ss files under <root>/data/YYYY/MM/DD/HH/.

    Sorts by *path* (chronological because the path encodes the time). Falls
    back to the parent <root> itself if <root>/data/ doesn't exist (e.g. a
    local copy that already starts at data/)."""
    base = root / "data" if (root / "data").is_dir() else root
    # Filter macOS AppleDouble sidecars ("._<name>") -- they're 4 KB binary
    # resource forks that would parse as garbage and pollute the diff.
    files = [p for p in base.rglob("*.ss")
             if p.is_file() and not p.name.startswith("._")]
    return sorted(files)


def settings_time_of(snap: dict[str, str]) -> str:
    """Return the snapshot's stamped settings_time (unquoted), or "" if
    absent. Used as the `at` timestamp on a change event."""
    raw = snap.get("settings_time", "")
    return raw.strip().strip('"')


def diff_config(old: dict[str, str], new: dict[str, str],
                volatile: set[str]) -> dict[str, list]:
    """Compare two snapshots, ignoring volatile fields. Returns {key:
    [old_value, new_value]} for every non-volatile field that differs (added,
    removed, or changed). Empty dict means no config change."""
    out: dict[str, list] = {}
    keys = (set(old) | set(new)) - volatile
    for k in keys:
        ov = old.get(k)
        nv = new.get(k)
        if ov != nv:
            out[k] = [ov, nv]
    return out


def extract(card_root: Path, cards_root: Path,
            volatile: set[str], commit: bool,
            network_override: str | None) -> int:
    files = find_hourly_ss_files(card_root)
    if not files:
        print(f"No hourly .ss files found under {card_root}")
        return 2

    # Use the root settings.ss for net/sta/serial (it's the canonical
    # identity file rename_card.py reads). Fall back to the first hourly
    # snapshot if the root isn't present (e.g. local copy of just data/).
    root_ss = card_root / "settings.ss"
    if root_ss.is_file():
        ident_snap = parse_ss(root_ss.read_text(errors="replace"))
    else:
        ident_snap = parse_ss(files[0].read_text(errors="replace"))
    sta = ident_snap.get("sitename", "").strip().strip('"')
    serial = ident_snap.get("serial", "").strip().strip('"')
    net_in_ss = ident_snap.get("network_code", "").strip().strip('"')
    net = network_override if network_override else net_in_ss
    if not (net and sta and serial):
        print(f"Could not resolve net/sta/serial from settings "
              f"(net={net!r} sta={sta!r} serial={serial!r}).")
        return 2
    serial4 = serial[-4:]

    # Card span: derive from filenames' parent dirs (data/YYYY/MM/DD/HH/).
    dates = []
    for p in files:
        m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/\d{2}/[^/]+\.ss$", str(p))
        if m:
            try:
                dates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                pass
    if not dates:
        print("Could not derive any data dates from .ss paths.")
        return 2
    start, end = min(dates), max(dates)
    derived_card_id = f"{start:%Y%m%d}-{end:%Y%m%d}_{serial4}"

    # Prefer an EXISTING ledger card-id for this station+serial -- the rename
    # script registered authoritatively with the true card span; our derived
    # span may be short if some hourly .ss files weren't captured (slow SD).
    # We match on the serial4 suffix, since station+serial is the unique key.
    card_id = derived_card_id
    sta_dir = cards_root / f"{net}.{sta}"
    if sta_dir.is_dir():
        existing = [d.name for d in sta_dir.iterdir()
                    if d.is_dir() and d.name.endswith(f"_{serial4}")]
        if len(existing) == 1 and existing[0] != derived_card_id:
            print(f"NOTE: aligning to existing ledger card-id {existing[0]} "
                  f"(derived from .ss span was {derived_card_id} -- likely "
                  f"some hourly snapshots weren't captured).")
            card_id = existing[0]

    out_dir = cards_root / f"{net}.{sta}" / card_id
    epoch_path = out_dir / "settings_epoch.ss"
    final_path = out_dir / "settings_final.ss"
    changes_path = out_dir / "settings_changes.jsonl"

    # Diff scan
    epoch_text = files[0].read_text(errors="replace")
    epoch_snap = parse_ss(epoch_text)
    final_text = files[-1].read_text(errors="replace")
    current = dict(epoch_snap)
    events: list[dict] = []
    for p in files[1:]:
        snap = parse_ss(p.read_text(errors="replace"))
        d = diff_config(current, snap, volatile)
        if d:
            try:
                rel = str(p.relative_to(card_root))
            except ValueError:
                rel = str(p)
            events.append({"at": settings_time_of(snap) or rel,
                           "source": rel,
                           "changes": d})
            current.update({k: snap.get(k) for k in d})
            # Remove keys that were dropped in the new snapshot.
            for k, (_, nv) in d.items():
                if nv is None:
                    current.pop(k, None)

    # Report
    print(f"Card           : {card_root}")
    print(f"net.sta/serial : {net}.{sta}  {serial}"
          + (f"  [network override; settings.ss said {net_in_ss!r}]"
             if (network_override and net != net_in_ss) else ""))
    print(f"Card id        : {card_id}")
    print(f"Span           : {start} -> {end}  ({len(files)} hourly .ss files)")
    print(f"Config events  : {len(events)}")
    if events:
        for e in events:
            keys = ", ".join(sorted(e["changes"].keys()))
            print(f"  {e['at']}  changed: {keys}")
    else:
        print("  (no non-volatile changes across the card -- config was stable)")
    print(f"Output dir     : {out_dir}")
    print(f"  -> settings_epoch.ss         ({len(epoch_text)} bytes)")
    print(f"  -> settings_changes.jsonl    ({len(events)} events)")
    print(f"  -> settings_final.ss         ({len(final_text)} bytes)")

    if not commit:
        print("\nDRY-RUN -- nothing written. Re-run with --commit.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    epoch_path.write_text(epoch_text)
    final_path.write_text(final_text)
    with changes_path.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
    print(f"\nWrote {epoch_path}")
    print(f"Wrote {changes_path}")
    print(f"Wrote {final_path}")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Extract Gecko per-card settings epoch + diff history")
    p.add_argument("card_root", help="SD card mount (e.g. /Volumes/WLSH241103) "
                                     "or a local copy that contains data/")
    default_cards = (Path(__file__).resolve().parents[2]
                     / "sds_staging_ledger" / "cards")
    p.add_argument("--cards-root", default=str(default_cards),
                   help=f"ledger cards/ dir (default: {default_cards})")
    p.add_argument("--commit", action="store_true",
                   help="actually write the files (default: dry-run summary)")
    p.add_argument("--network", default=None,
                   help="override the network code (see rename_card.py --network); "
                        "affects only the output dir path (not the .ss contents).")
    p.add_argument("--volatile-extra", default="",
                   help="comma-separated extra keys to treat as volatile "
                        "(silenced in the diff)")
    args = p.parse_args(argv[1:])

    volatile = set(VOLATILE_FIELDS)
    if args.volatile_extra:
        volatile |= {k.strip() for k in args.volatile_extra.split(",") if k.strip()}

    return extract(Path(args.card_root), Path(args.cards_root),
                   volatile, args.commit, args.network)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
