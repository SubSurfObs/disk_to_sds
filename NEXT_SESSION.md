# Next session pickup (2026-05-27 → ?)

Mac is on home Wi-Fi; everything below needs Uni LAN (or VPN, with the caveat
that the VPN routes mediaflux SMB through `utun*` and cuts throughput ~100×).

## State

| Card / station | Ingest done? | Apply done? | Notes |
|---|---|---|---|
| VW.TRPU 0485 | ✓ | ✓ | cleanup uncommitted on staging VM |
| VW.MARD 0487 | ✓ | ✓ | cleanup uncommitted on staging VM |
| VW.OUTU 0487 | ✓ | partial | cleanup uncommitted on staging VM (older session) |
| VW.OUTU 026 (EchoPro USB) | **57 / 98 days** | partial (10 days were also applied in this same session) | resume — see Task 1 |
| VW.WLSH 0269 | re-pulled with `.00.` ✓ | **OLD empty-loc applied; .00. NOT yet applied** | see Task 2 |

## Tasks (in order)

### 1. Finish OUTU 026 EchoPro ingest (Mac, on Uni LAN)
Plug OUTU_026 USB back in, then re-run — resume picks up from day 58:
```bash
nohup /opt/anaconda3/envs/seisbench-env/bin/python -u scripts/echopro_usb_to_sds.py \
  /Volumes/OUTU_026/LocalArchive \
  /Volumes/proj-6700_seiscomp_staging-1128.4.1649/seiscomp_archive \
  >>/tmp/outu_full.log 2>&1 &
```
Mount staging first via `scripts/mount_staging.sh`. ~4 h on healthy SMB.

### 2. Apply VW.WLSH `.00.` files to LT + delete the empty-loc supersede targets (dev1)
The re-pull put the `.00.` files in staging but apply hasn't run. Then the
old empty-loc files have to be removed from LT (732 of them) — explicit
exception to memory `never-delete-lt-archive`, per the migration plan
we agreed on. Log the deletes in the ledger.
```bash
ssh seiscomp@seismology-dev1.its.unimelb.edu.au
cd ~/projects/SubSurfObs/sds_staging_ledger
git pull --rebase    # pull Mac's 0b0a63c (WLSH cards/)
# DRY-RUN first
python3 apply.py --staging-root /mnt/seiscomp_staging/seiscomp_archive \
  --lt-root /mnt/seiscomp_archive --ledger-root ./seiscomp_archive \
  --net VW --sta WLSH --source-card 20241103-20250704_0269
# Expect: would write=732 (the .00. paths are new on LT); 0 override/skip/fail
# COMMIT
... --commit
# Then delete the empty-loc files (the migration step we agreed on)
find /mnt/seiscomp_archive/2024/VW/WLSH /mnt/seiscomp_archive/2025/VW/WLSH \
  -type f -name 'VW.WLSH..*' | wc -l   # confirm = 732
find /mnt/seiscomp_archive/2024/VW/WLSH /mnt/seiscomp_archive/2025/VW/WLSH \
  -type f -name 'VW.WLSH..*' -delete
# Log a removed_supersede event into the manifests (next session: write
# a one-off helper script, or just append jsonl by hand WITH the same
# action shape as apply.py uses).
```

### 3. Ledger reconcile across 3 machines (Mac is already pushed to 0b0a63c)
Each machine has different uncommitted work. They don't touch the same files,
so the order is just "everyone commit + push + pull --rebase".

**On staging VM** (`ssh dsand@172.26.144.41`):
```bash
cd ~/projects/SubSurfObs/sds_staging_ledger
git pull --rebase    # gets Mac's WLSH cards/
git add cards/VW.{TRPU,MARD,OUTU}/*/card.json \
        seiscomp_archive/*/VW/*.cleanups.jsonl
git commit -m "TRPU/MARD/OUTU stage 4 cleanup records"
git push
```

**On dev1** (`ssh seiscomp@seismology-dev1.its.unimelb.edu.au`):
```bash
cd ~/projects/SubSurfObs/sds_staging_ledger
git pull --rebase    # gets Mac + staging-VM commits
git add seiscomp_archive/2024 seiscomp_archive/2025/VW/WLSH.events.jsonl
git commit -m "WLSH stage 3 manifests (empty-loc apply 2026-05-27; see also 0b0a63c)"
git push
# After task 2 above:
git add seiscomp_archive/{2024,2025}/VW/WLSH.events.jsonl
git commit -m "WLSH .00. supersede: write + removed_supersede events"
git push
```

**Back on Mac** + **staging VM**: `git pull --rebase` to converge.

### 4. Patch the scripts so this doesn't happen again
Add `git pull --rebase && git add ... && git commit ... && git push` at the
end of each stage's script (rename_card, gecko_sdcard_to_sds, apply, cleanup)
so the ledger stays converged automatically. Needs push-credentials set up on
all three hosts (Mac already has; staging VM + dev1 need to be checked).

## Heads-up: incoming changes from eqserver_2_seiscomp

eqserver_2_seiscomp work in this same session is going to push to
`sds_staging_ledger` too (parallel writer):
- `apply.py` patch: new `--source-kind` flag (currently hardcoded `"sdcard"`
  in the manifest event line; eqserver needs `"oldarchive"`). Backward-
  compatible default; SD-card stage 3 unchanged.
- `phase3_driver` patch (eqserver-side): prints the suggested `apply.py`
  command after successful staging conversion.

Implications for **our** reconcile:
- The dev1 `git pull --rebase` in Task 3 will likely pull eqserver's apply.py
  patch too. No conflict expected (we touch `cards/` + `seiscomp_archive/`,
  they touch `apply.py`).
- After pulling, the WLSH `apply.py` command in Task 2 may want
  `--source-kind sdcard` to be explicit (depends on whether they make it
  required or just optional with a sdcard default).
- Also: there may be an `oldarchive_2025` (or similar) tree appearing
  under `seiscomp_archive/<YYYY>/<NET>/` for whichever stations they're
  converting tonight. Don't be surprised by it.

## Things to remember from this session

- `--network VW --location 00` is mandatory for every Gecko SD card
  (memories `sdcard-network-override-vw` + `sdcard-location-override-00`).
  `gecko_sdcard_to_sds.py` refuses to proceed if records have empty loc and
  `--location` isn't given.
- VPN routes SMB through `utun*` and cuts throughput ~100×. Always check
  `route get default | grep interface` before debugging SMB perf — it should
  be `en*`, not `utun*`. Disconnect VPN client if it's there.
- `scripts/mount_staging.sh` mounts the staging share in ~3 s (idempotent).
  `diskutil eject /Volumes/<name>` for clean unmount (Finder UI sometimes
  refuses while a background indexer is touching the volume).
- `staging_buffer.py` decouples producer from SMB on EchoPro runs; tested
  in this session, rode out a multi-minute mount drop without losing data.
  Known sub-optimal: `promote_pending` is synchronous, so slow SMB still
  back-pressures the producer (just doesn't kill it).
- Known pipeline gap: nothing flips `applied_to_lt: false -> true` in
  card.json after apply. Hand-edit forbidden (memory). Needs a small
  patch in apply.py.
