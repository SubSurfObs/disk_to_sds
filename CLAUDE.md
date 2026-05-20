# CLAUDE.md — sdcard_to_sds

Session orientation for Claude Code. Canonical design in `docs/ARCHITECTURE.md`;
this file is the quick brief.

## What this repo is

Ingest pipeline: **field SD card → SMB staging → long-term SeisComp SDS archive**.
Today it handles SRC Gecko SD cards; eventually generalises to any local-disc
recorder (will be renamed `local_disc_to_sds`).

## Planned 3-repo architecture (V2)

| Repo | Scope | Hosts |
|---|---|---|
| `sdcard_to_sds` (this repo, → `local_disc_to_sds`) | SD/local disc → staging mount | Mac |
| `eq_server_to_sds` (future, one-off) | old EQ-server archive → staging mount | VM-only |
| `sds_staging_ledger` (future) | manifest + plan + apply + cleanup; only writer to LT | staging VM + SeisComp VM |

The **staging → LT copy lives in `sds_staging_ledger`**, NOT in this repo.
Ingest repos depend on the ledger only as a library for writing manifest entries.

## Pipeline stages

1. **Pull** (Mac, this repo) — `scripts/gecko_sdcard_to_sds.py`. SD card → `/tmp/scratch` → rsync per-day → staging mount. SD card opened read-only, atomic `.partial→rename` on outputs.
2. **Plan** (staging VM, future ledger repo) — examine staging vs LT; produce per-card plan.
3. **Apply** (SeisComp VM, future ledger repo) — execute plan; cp staging→LT; append manifest. **Only host with write access to `/mnt/seiscomp`.**
4. **Cleanup** (staging VM) — remove staged data after successful apply; mark card done in `cards/<card-id>.json`.

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
| Staging VM | `~/projects/SubSurfObs/sdcard_to_sds/` (git checkout, .venv with obspy) | scripts |
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
