# disk_to_sds — Architecture

_(Repo renamed from `sdcard_to_sds` on 2026-05-23.)_

## Goal

Ingest seismic data from field-deployed Gecko SD cards into our long-term
SeisComp SDS archive **without ever overwriting telemetered data that is
already complete**, and with a durable audit trail of every decision.

## The one rule

**SD card is ground truth, but only fills gaps.** A day already complete
via telemetry is *never* touched by SD card data. A day that's missing
or partial gets the SD card data merged in (records deduped).

## Hosts and mounts

```
┌──────────────────────────────┐        ┌────────────────────────────┐
│ Mac (DSAND laptop)           │        │ VM (uni-managed)           │
│                              │        │                            │
│  /Volumes/<sdcard>/data      │        │  SeisComp tools available  │
│       (READ ONLY)            │        │   (scmssort, scart, msi)   │
│                              │        │                            │
│  ~/projects/.../disk_to_sds  │  git   │  ~/projects/.../disk...    │
│   (this repo)                ├────────┤   (same repo, separate co) │
│                              │        │                            │
│  Mounts:                     │        │  Mounts:                   │
│   - SMB staging              │        │   - SMB staging            │
│     (mediaflux)              │        │   - SMB long-term archive  │
└──────────────────────────────┘        └────────────────────────────┘
```

Both hosts share the repo via GitHub. Code and manifests are in git;
SDS data is git-ignored. The Mac never touches the long-term archive;
the VM never reads the SD card directly.

## Four stages

Each stage is idempotent and runs as a separate script.

### 1. Pull (Mac)

`scripts/pull_sdcard.py <sdcard_data_dir> --card-id <ID>`

- Walks `YYYY/MM/DD/HH/*.ms` on the SD card.
- Streams 512-byte miniSEED records straight into per-channel SDS day
  files in a local scratch (`/tmp/sds_scratch`) using atomic
  `.partial → rename`.
- After each day, rsyncs the 3 channel files to the SMB staging mount
  and deletes the local scratch copy.
- Appends one line to `manifests/uploads.jsonl` per card pull.
- SD card is opened read-only; never modified.

### 2. Plan (VM, dry-run)

`scripts/plan_merge.py --card-id <ID>`

- For each (day, channel) in `staging/local_sds/<card-id-tree>`:
  - Count samples in staged file (via `msi -ts` or obspy).
  - If long-term file exists: count its samples too.
  - Decide: `MERGE`, `SKIP`, or `REVIEW` (see decision logic below).
- Writes the plan to `manifests/plans/<card-id>_<UTC>.jsonl` (ephemeral;
  gitignored).
- Prints the decision table to stdout for human review.

### 3. Apply (VM)

`scripts/apply_merge.py <plan_file> --commit`

- Re-reads the plan file (or recomputes if `--regenerate`).
- For each `MERGE` row: `cat staged_day | scmssort -u -E | scart -I -
  <long_term_root>` into a `.partial` then atomic-rename.
- For each `SKIP` row: do nothing.
- Appends one line per (day, channel) decision to
  `manifests/merges.jsonl`.
- Default is `--dry-run` (no `--commit` flag = no writes).

### 4. Cleanup (VM)

`scripts/cleanup_card.py --card-id <ID>`

- Verifies every day in this card's staged tree has a corresponding
  `merges.jsonl` line.
- If all accounted for: `rm -rf staging/local_sds/<card-id-tree>`.
- Appends one line to `manifests/cleanups.jsonl`.

## Decision logic (Stage 2)

Per (day, channel):

```
expected = sampling_rate × 86400          # e.g. 21.6M @ 250 Hz
LT       = samples in long-term file (0 if missing)
ST       = samples in staged file

if LT == 0:                            → MERGE   (new data)
if LT >= ST:                           → SKIP    (LT already as complete)
if (ST - LT) / expected > 0.001:       → MERGE   (>0.1% material gain)
otherwise:                             → SKIP    (within noise)
```

Tolerance is configurable. The `>= as complete` clause is the safety
belt: it's impossible to *reduce* the sample count of an LT file.

## Manifests (in git, append-only)

### `manifests/uploads.jsonl`
One line per SD card pull (Stage 1).
```json
{"ts":"2026-05-20T14:49Z","card_id":"MARD_2025Q3_0487","net":"VW","sta":"MARD",
 "gecko_serial":"02000487","date_min":"2025-07-04","date_max":"2026-05-19",
 "day_count":321,"bytes":40000000000,"host":"DSAND-mac",
 "scratch":"/tmp/sds_scratch","staging":"/Volumes/.../local_sds"}
```

### `manifests/merges.jsonl`
One line per (day, channel) decision actually applied in Stage 3.
```json
{"ts":"...","card_id":"...","day":"2025-08-14","net":"VW","sta":"MARD",
 "cha":"CHZ","loc":"00","lt_samples_before":18400000,"st_samples":21500000,
 "lt_samples_after":21580000,"action":"merged","reason":"telemetry gap"}
```
SKIP rows are also logged so we have a complete record.

### `manifests/cleanups.jsonl`
One line per card cleanup (Stage 4).

### `manifests/plans/` (gitignored)
Ephemeral Stage-2 output. Not part of the audit trail.

## Card-ID convention

`<STA>_<YYYY>Q<n>_<last4_of_serial>` — e.g. `MARD_2025Q3_0487`.

- Human-readable: tells you the station and quarter at a glance.
- Quarter-precision is enough — one SD card rarely spans more than a
  quarter (we have 12 cards/year per station).
- Serial last-4 disambiguates if multiple units swap into one station.

Generated automatically from `settings.ss` + first/last `YYYY/MM/DD`
on the card; user can override.

## Repo layout

```
disk_to_sds/
├── README.md                     workflow + processed-card table
├── docs/ARCHITECTURE.md          this file
├── scripts/
│   ├── pull_sdcard.py            Stage 1
│   ├── plan_merge.py             Stage 2
│   ├── apply_merge.py            Stage 3
│   ├── cleanup_card.py           Stage 4
│   ├── gecko_sdcard_to_sds.py    legacy alias of pull_sdcard.py
│   ├── merge_days_gecko.sh       legacy (old workflow)
│   └── lib/
│       ├── sds.py                SDS path helpers
│       ├── manifest.py           append/read JSONL manifests
│       ├── mseed.py              record streaming, sample counting
│       └── gecko.py              parse settings.ss
├── manifests/
│   ├── uploads.jsonl             durable audit
│   ├── merges.jsonl              durable audit
│   ├── cleanups.jsonl            durable audit
│   └── plans/                    gitignored, ephemeral
├── local_sds/                    gitignored (Mac scratch / SMB-mirrored)
├── inbox/                        legacy, gitignored
└── .gitignore
```

## Invariants

- **SD card read-only.** Every script that touches the SD card opens
  files with mode `"rb"` only. A startup guard refuses to run if any
  output path resolves inside the SD card mount.
- **Long-term writes are atomic.** Build to `<file>.partial`, fsync,
  `os.replace`. SeisComp never sees half-written archives.
- **No `rsync --delete` anywhere.** Never against staging, never
  against long-term.
- **Default to dry-run.** `apply_merge.py` requires explicit `--commit`.
- **Staging is the rollback point.** A card is not cleaned up until
  every day has a merges.jsonl entry.
- **One global lock per station.** `apply_merge.py` takes a flock on
  `long_term/<NET>/<STA>/.lock` so concurrent merges can't race.

## What's deliberately out of scope (V1)

- Multi-station SD cards (a Gecko writes one station per card).
- Re-ingestion / amendment of an already-merged day from a later card.
  When this comes up we'll add a `--force` path; for now skips are
  permanent until manually reset.
- Sidecar metadata inside the long-term archive (e.g. "this day's
  records came from card X"). Audit trail lives in `manifests/`.
- Automated cron-driven processing. Each stage is user-invoked.
