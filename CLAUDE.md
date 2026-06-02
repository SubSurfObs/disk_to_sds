# CLAUDE.md — disk_to_sds

> Repo renamed `sdcard_to_sds` → `disk_to_sds` on 2026-05-23 (generic intent: many recorder types + layouts; Gecko SD is one adapter). GitHub redirects the old URL. Local Mac working dir, git remote, and Claude memory all migrated to `disk_to_sds`.

Session orientation for Claude Code. Canonical design in `docs/ARCHITECTURE.md`;
this file is the quick brief.

## What this repo is

Ingest pipeline: **field SD card → SMB staging → long-term SeisComp SDS archive**.
Today it handles SRC Gecko SD cards; generalising to any local-disk recorder
and on-disk layout (the repo is now `disk_to_sds`; Gecko SD is the first adapter).

## 3-repo architecture

| Repo | Scope | Status |
|---|---|---|
| `disk_to_sds` (this repo) | SD/local disk → staging mount | active |
| `eqserver_2_seiscomp` (`/Users/DSAND/projects/SubSurfObs/eqserver_2_seiscomp`) | old EqServer archive (PC-SUDS/EchoPro + miniSEED) → staging mount; one-off | active, skeleton |
| `sds_staging_ledger` (`/Users/DSAND/projects/SubSurfObs/sds_staging_ledger`) | manifest + apply + per-card plotting; only writer to LT | **exists** (cloned on Mac, staging VM, dev1) |

The **staging → LT copy lives in `sds_staging_ledger`** (`apply.py`), NOT in this repo.
Ingest repos depend on the ledger for manifest entries + the apply model.

## Sibling project: `eqserver_2_seiscomp`

Separate but **intertwined** — converts the decade-scale old EqServer archive
into the *same* long-term SeisComP SDS, sharing all the infrastructure we built:

- **Shared staging VM** `rs-l-0ezd3a` / `172.26.144.41` (user `dsand`), with three
  mounts: `/mnt/eqserver_archive` (NFS ro, the old archive — eqserver's input),
  `/mnt/seiscomp_staging` (CIFS rw, **shared** staging SDS), `/mnt/seiscomp_archive`
  (CIFS ro, long-term).
- **Shared staging SDS skeleton** — both pipelines write
  `/mnt/seiscomp_staging/seiscomp_archive/<YYYY>/<NET>/<STA>/...`.
- **Shared ledger** — `eqserver_2_seiscomp` appends to the same `sds_staging_ledger`
  so one provenance record covers both old-archive conversion and SD-card uploads.
- **Shared station→network truth** — `eqserver_2_seiscomp/metadata/station_registry.yaml`
  is the authoritative VW/VX/DU assignment per station. EchoPro/PC-SUDS files carry
  **no network code**, so eqserver must patch it from the registry; Gecko (this repo)
  already writes correct codes but the registry is the common reference. Several
  stations (MARD, TRPU, …) appear in both pipelines.
- **Shared diagnostic** — the duration-ratio metric (in `plot_card.py` and
  `plotting/`) is used by both.

## Pipeline stages

0. **Identify/rename** (Mac, optional) — `scripts/rename_card.py`. On field return, reads `settings.ss` + scans the date range, then safely renames the macOS volume to a ledger-aligned label (FAT32: `STA+YYMMDD` ≤11 char e.g. `MARD250704`; exFAT: full `STA_<card-id>`) and registers `card.json` with `volume_label`. Rename is metadata-only/reversible; refuses anything without the `settings.ss`+`data/` Gecko signature. Purpose: unambiguously identify a card before the eventual wipe-and-reuse.
1. **Pull** (Mac, this repo) — `scripts/gecko_sdcard_to_sds.py`. SD card → `/tmp/scratch` → rsync per-day → staging mount. SD card opened read-only, atomic `.partial→rename` on outputs.
2. **Plan** (staging VM, future ledger repo) — examine staging vs LT; produce per-card plan.
3. **Apply** (SeisComp VM, future ledger repo) — execute plan; cp staging→LT; append manifest. **Only host with write access to `/mnt/seiscomp`.**
4. **Cleanup** (staging VM) — remove staged data after successful apply; mark card done in `cards/<card-id>.json`.

## Card upload runbook — trigger: "upload" / "process this card"

When the user says **"upload"** (or "process this card"), act as an agent: don't
just run the pull. Drive the card through the stages below, report after each,
and stop only at the two irreversible **GATES** (LT commit, wipe). Commands
verified 2026-05-25 on VW.OUTU.

1. **Identify** (Mac) — `scripts/rename_card.py "/Volumes/<vol>" --network VW --location 00`
   (dry-run → `--commit`). Run FIRST: labels the volume (`STA+YYMMDD`), creates
   `cards/<NET>.<STA>/<card-id>/card.json`, AND walks the ~6k hourly `.ss`
   snapshots to write `settings_epoch.ss` + `settings_changes.jsonl` (config
   diffs only — volatile fields filtered) + `settings_final.ss` into the same
   card dir. Skip the .ss extract with `--no-settings-history`. **Always pass
   `--network VW --location 00`** — current SD tranche predates the FDSN VW
   assignment, and VW policy is loc='00' on every primary sensor (memories
   `sdcard-network-override-vw` + `sdcard-location-override-00`). Out-of-order
   leaves the note stale.
2. **Pull → staging** (Mac) — `scripts/gecko_sdcard_to_sds.py "/Volumes/<vol>/data" /tmp/sds_scratch_<sta> --fast --skip-existing --network VW --location 00 --rsync-to /Volumes/proj-6700_seiscomp_staging-1128.4.1649/seiscomp_archive`.
   `--network VW` rewrites bytes 18:20 of every MSEED record + SDS path;
   `--location 00` does the same for bytes 13:15. Pull refuses to proceed if
   records have empty location and `--location` is not given (catches
   misconfigured cards like WLSH 2024-11). Check the printed `SKIP` list is
   pre-GPS-lock junk only (date-window filter).
3. **Apply → LT** — `ssh seiscomp@seismology-dev1.its.unimelb.edu.au`, then
   `cd ~/projects/SubSurfObs/sds_staging_ledger && python3 apply.py --staging-root /mnt/seiscomp_staging/seiscomp_archive --lt-root /mnt/seiscomp_archive --ledger-root ./seiscomp_archive --net <NET> --sta <STA> --source-card <card-id>`.
   DRY-RUN first. **GATE: show the dry-run, get an explicit go before adding `--commit`.**
   A new station is all `write` (additive); `--mode overwrite --fast` is fine then.
4. **Cleanup** — `ssh dsand@172.26.144.41`, `cleanup.py ... --net <NET> --sta <STA> --source-card <card-id>` (dry-run → `--commit`): verifies staged==LT,
   deletes staging, stamps `card.json.cleanup.complete`.
5. **Wipe** (Mac) — ONLY once data is verified in LT (apply done + staged-vs-LT
   match). The **user does this manually in the Disk Utility GUI** (visual volume
   selection) — do NOT run it. Identify + confirm the volume (name/size), then
   hand off: reformat to **MS-DOS (FAT32)** for ≤32 GB, **ExFAT** for larger.
   (CLI equivalent if ever needed: `diskutil eraseDisk FAT32 <LABEL> MBRFormat /dev/diskN`.)

Notes:
- Apply LT root on dev1 is `/mnt/seiscomp_archive` (CIFS **rw**), not `/mnt/seiscomp`.
- Known gap: pull + apply do NOT auto-stamp card.json (`pull_date`/`day_count`/
  `channels`/`staging_path`/`applied_to_lt`). Flag the missing fields — do not
  hand-edit the record (see memory `no-manual-provenance-edits`).

## Invariants (do not violate)

- **SD card is read-only during ingest.** Every input opened `"rb"` only. Startup guard refuses to run if output is inside SD card mount.
- **Mac never writes to LT archive.** Mac → staging only.
- **Staging VM never writes to LT archive.** Plan + cleanup only.
- **Apply writes are atomic.** Build `<file>.partial`, fsync, `os.replace`. SeisComp never sees half-written archives.
- **No `rsync --delete` anywhere.**
- **Default to dry-run on apply.** `--commit` required to write.
- **Single global lock per LT station during apply.**
- **Data files never in git** — only manifests.

## V1 decision logic (binary, no merge)

Per (day, channel):
- `write` — no existing LT file → put SD/old-archive data there
- `skip` — LT exists, considered complete enough → don't touch
- `override` — LT exists AND we deliberately supplant it (rare, user-flagged for upstream-seedlink-mess cases)

**No `scmssort -u | scart` merge path in V1.** Tried before, dedup unreliable for interleaved-stream cases. Override is the escape hatch.

## Telemetry handling: implicit default

The manifest records **non-telemetry operations only**. A (day, channel) with no manifest entry = telemetry-only data. "What's in the archive" → ask FDSN Availability / scardac. "How did it get there" → ask the manifest. Two separate questions, two separate tools.

## Manifest layout (in git, append-only)

```
seiscomp_archive/                       ← mirrors /mnt/seiscomp layout to STA depth
  └── 2026/VW/MARD.events.jsonl         ← all channels, all days for this station-year
cards/
  └── MARD_2025Q3_0487.json             ← per-card metadata (pull + cleanup events)
```

Event line shape:
```json
{"ts":"...","day":"2026-05-14","cha":"CHZ","loc":"00","samp_rate":250,
 "action":"override","source":{"kind":"sdcard","card_id":"MARD_2025Q3_0487"},
 "lt_samples_before":18400000,"st_samples":21500000,"lt_samples_after":21500000,
 "reason":"telemetry gap"}
```

`source.kind ∈ {sdcard, oldarchive}` (telemetry never appears — it's the default).
Skip decisions are NOT logged per-day-channel (would bloat the file); per-card summary in `cards/<card-id>.json`.

## Hosts & mounts

| Host | Mount | Role |
|---|---|---|
| Mac (DSAND laptop) | SD card via `/Volumes/<sdcard>/` (Finder, READ ONLY) | Pull stage |
| Mac | SMB staging via `/Volumes/proj-6700_seiscomp_staging-1128.4.1649/` (Finder) | rsync destination |
| Staging VM (`172.26.144.41`, dsand@) | `/mnt/seiscomp_staging` (CIFS+Kerberos, noauto, in fstab) | Plan, cleanup |
| Staging VM | `~/projects/SubSurfObs/disk_to_sds/` (git checkout) | scripts |
| SeisComp VM (existing, separate) | `/mnt/seiscomp` (local) | Apply stage |

Mount names matter:
- **`/mnt/seiscomp_staging`** = the mount = staging area (data in transit).
- **`seiscomp_archive/`** = repo subdir = manifests mirroring the destination layout.

## Performance baseline (from 30 GB VW.MARD ingest)

- Full card SD → staging via `--fast --skip-existing --rsync-to`: **43 min, 11.77 MB/s effective.**
- Throughput floors:
  - SD card per-file overhead dominates write phase (~62% of total).
  - SMB single-stream throughput from Mac on Uni LAN: ~30 MB/s.
  - SMB throughput from staging VM (colocated): **~476 MB/s.**
- **Parallel rsync to mediaflux is a loss** — single-stream bandwidth cap; 3 streams ran 10× slower than 1.

## Performance tuning that's in the script

- `read_bytes()` per minute file (one syscall vs ~10 with `read(reclen)` loops).
- 16 MB write buffer per channel (`--fast`).
- Pipelined worker: while day N+1 reads from SD, day N rsyncs to staging.
- Parallel rsync is opt-in only (off by default).

## Duplication diagnostics — duration ratio

The key QC metric is the **duration ratio** = (sum of in-record sample-time) /
86400 — clean day ≈ 1.0, doubled day ≈ 2.0, partial < 1.0. File size is too
noisy for completeness (±30–50% from compression), so use the ratio.

This now lives in **`sds_staging_ledger/plot_card.py`** (built on `lib/sds.py`),
which produces per-card `duration_ratio.png`, `daily_file_size.png`, and the
SD-vs-LT comparison. It's the same metric `apply.py` uses as its "density" check
to detect duplication-corrupted LT days. (An earlier prototype `plotting/` dir
of parallel-session scripts was removed once `plot_card.py` superseded it.)

**Key finding it established:** SD-card data caps at ratio 1.0 (physics — a
recorder can't write >1 day/day), so it's ground truth. The same station via the
SeisComp pipeline shows ratios ~2.0 in discrete intervals (TRPU CHZ: 53 days at
~1.99 across 2026-02-26→03-18 and 2026-04-10 on) — that SD-vs-pipeline contrast
is the case made to the upstream operator.

## Midnight-boundary fix + a naming wart to clean up

SRC minute-files straddle midnight (named by start-time + constant `SS`
offset), so the first ~`SS` s of each day live only in the prior day's last
file. The old `write_sds` clobbered/misfiled that sliver — ~3.65 hr/station-yr
lost at SS=12, invisible to duration-ratio QC. Fixed in
`scripts/suds_convert.py:write_sds` (`@00b6835`): split-by-UTC-day +
merge-on-write, idempotent; regression test `scripts/test_boundary_write_sds.py`.
Discovered by the eqserver sweep 2026-06-02. **Still owed:**
`gecko_sdcard_to_sds.py:process_day` has the same bug via its raw-record/
directory-routing path (fix = route records by SEED header start-time); plus the
VM `disk_to_sds` git reconcile so the sweep's checkout gets `@00b6835`.

**Naming wart (deferred, tracked):** `write_sds` (+ `_sds_day_path_for`,
`_split_trace_by_utc_day`, atomic plumbing) lives in `suds_convert.py` but has
**nothing to do with SUDS** — it's the generic SDS writer every eqserver phase3
branch (gecko/minimus/mseed/echopro) calls. Gecko data is read by ObsPy and
merely *written* by it, not SUDS-parsed. Plan: split into a new `sds_writer.py`
(generic writer) vs. `suds_convert.py` (SUDS reader only). Deferred to a
dedicated pure-rename PR **after the eqserver sweep stabilises** — it changes
import paths in both repos; don't conflate with the data-loss fix. See memory
`split-sds-writer-from-suds-convert`.

## Future work: generic pre-downloaded-MiniSEED adapter (`miniseed_to_sds.py`)

A planned third "disk source" (after Gecko SD + EchoPro USB): an
**ingest-pre-downloaded-files** adapter. Point it at a `.mseed` file or a
dir/glob; it routes each record to its SDS day-file by SEED header + record
start-time, dedups records, writes atomically to staging. **No conversion**
(input is already MiniSEED) and **no fetch** (files arrive out-of-band). The
record-routing core is the same logic as `gecko_sdcard_to_sds.py:process_day`;
worth extracting into a shared module both call.

Primary motivation is **gap-patching** where slarchive failed, so
`apply.py --mode decide` (gap-fill: write missing / override partial / skip
complete) is the natural promotion mode -- the first source where decide-mode is
the main path, not the exception.

Design decisions already settled:
- **First/only consumer: seismosphere.net** -- manual web download (NO scriptable
  API, confirmed), occasional pathway. Used when slarchive->LT dropped data but
  Seismosphere's longer direct-download buffer (vs its seedlink server) still has
  it. Optimise for correctness + clear per-day reporting over a few files, not
  throughput; no buffering/pipelining/overnight runs.
- **Provenance is run-shaped, not card-shaped** -- no serial / settings.ss /
  volume label, so no `card.json`. `source.kind = "seismosphere"` (or generic),
  interval-based fields (`request` net/sta/loc/cha + start/end, `downloaded_at`,
  `service`), carried via the ledger's `--source-extra-json` mechanism.
- **Day-boundary routing** by record start-time (scart/slarchive convention).
- **Dedup is mandatory** -- overlapping downloads of the same day would otherwise
  duplicate records and inflate the duration ratio (same signature as the TRPU
  LT-doubling). Hash each record; skip dupes when assembling a day-file.
- **Overrides default OFF** -- Seismosphere returns real FDSN data with correct
  net/loc codes (unlike the pre-FDSN Gecko tranche); flags stay available.
- **Reuse the duration-ratio QC** -- a patched day showing ratio >1.0 means dedup
  failed; catch before apply.
- **Generic by design** -- Seismosphere is just one source of "MiniSEED on disk";
  the same tool ingests FDSN dataselect dumps or scattered `.mseed`.

Blocked only on a sample of what seismosphere.net's download button produces
(one file vs many, reclen, code correctness, day alignment, filename convention)
to pin discovery + dedup keys.

## User collaboration preferences

- Terse responses. No long preambles.
- Don't ask permission before read-only operations (Read, ls, find, head, tail of project files, mounted volumes, /tmp). See memory `feedback-no-permission-prompts`.
- Don't ask before reading. Just read.

## Pending TODOs (as of 2026-05-20 evening)

- Commit + push today's Mac-side changes (script optimisations, ARCHITECTURE.md, manifests/ skeleton, .gitignore).
- VM has older code, needs `git pull` after Mac push.
- Tomorrow on Uni LAN: re-ingest VW.MARD into the NEW staging mount with optimised script. Bench expected per-day time vs today's 8s/day average.
- Update script to write to `seiscomp_archive/` path on staging (currently uses `local_sds/`).
- Create the ledger repo when ready to wire up plan/apply.
