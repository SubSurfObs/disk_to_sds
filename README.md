# disk_to_sds

_(formerly `sdcard_to_sds`; renamed 2026-05-23 as the scope generalises beyond Gecko SD cards to multiple recorder types and on-disk layouts. The Gecko SD-card path is one adapter.)_

**Architecture:** see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the
canonical pipeline (Pull on Mac → Plan/Apply on VM → Cleanup), the
gap-fill decision logic, and the durable manifests in `manifests/`.

# Seismic Upload Workflow (SD Cards → VM → Long-Term SDS)

This repository does **not** store seismic data.  
It only tracks:

- The **commands** used to ingest data from SD cards
- A **log** of uploads (what was ingested, when, from where)
- Simple **helper scripts** (in `scripts/`)

All actual waveform data live in:

- `inbox/` (temporary, shared mount; not tracked by git)
- A local SDS archive on the VM
- The long-term SDS repository (rsync target)

---

## Directory layout

- `inbox/`  
  Temporary location for raw data copied from SD cards.  
  This is on the shared/mounted disk, visible to both Mac and VM, and **ignored by git**.

- `scripts/`  
  Helper scripts, e.g. `process_inbox_example.sh`, showing how to:
  - run `scart` into a local SDS archive
  - `rsync` that SDS into the long-term repository

- `README.md`  
  This file. Contains:
  - Workflow notes
  - A log of each upload session

---

## Example usage

### Gecko SD card → SDS in one local step (recommended)

For Gecko-format SD cards (`data/YYYY/MM/DD/HH/*.ms`) the unit already
writes correct `NET.STA.LOC.CHA` codes in every MiniSEED record, so no
remapping is needed. `scripts/gecko_sdcard_to_sds.py` streams 512-byte
records straight from the SD card and routes each into its SDS day
file — no decoding, no intermediate concat, no VM round-trip.

```
./scripts/gecko_sdcard_to_sds.py "/Volumes/NO NAME/data" ./local_sds
```

Then rsync the resulting `local_sds/` into the long-term archive:

```
rsync -avh --progress ./local_sds/ /Volumes/proj-6700_uom_seismic_data-1128.4.1143/sdcard_to_sds/local_sds/
```

The script:
- walks `YYYY/MM/DD/HH/*.ms` on the SD card
- reads each record's SEED header (net/sta/loc/cha) and appends it to
  `local_sds/YYYY/NET/STA/CHA.D/NET.STA.LOC.CHA.D.YYYY.DOY`
- works with any Python that has no external deps (uses stdlib only)
- typical throughput: ~150 MB/s reading the SD card on a Mac

If the Gecko was misconfigured and the location code needs fixing, do
it once at the end with `scmssort` / `scart` on the VM, or just rename
files in the SDS tree.

### Legacy workflow (concat → rsync → scart on VM)

Kept for reference; only useful if the Gecko codes are wrong and you
need scart's `--rename` to fix them.

```
./scripts/merge_days_gecko.sh /Volumes/BGT2/data ./inbox/BGT2/
rsync -avh --progress inbox/BGT2 /Volumes/proj-6700_uom_seismic_data-1128.4.1143/sdcard_to_sds/inbox

# on VM:
for f in ~/mnt/sdcard_to_sds/inbox/BGT2/*.mseed; do
    cat "$f" | scmssort -u -E 2>/dev/null \
      | scart -I - --rename "Z1.BGT2.*.*:Z1.BGT2.00.-" \
            ~/mnt/sdcard_to_sds/local_sds
done
```

