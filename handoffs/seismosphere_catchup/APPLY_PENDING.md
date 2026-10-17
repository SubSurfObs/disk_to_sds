# PENDING APPLY → seiscomp_server_uom: DU.GLNLG + DU.HKRT (Seismosphere backfill)

*Written 2026-10-17 from disk_to_sds. Data is staged + verified; the apply to LT
is deferred because dev1 (the sole LT writer) is VPN-only and the VPN was down.
**Run this once VPN/dev1 is reachable.***

## Status

Three SA short-period `DU.<SITE>.00.EHZ @ 200 Hz` stations that SeedLink doesn't
republish were backfilled from Seismosphere (fetch + ingest ran entirely on the
VM `rs-l-0ezd3a`):

| Station | LT state | Staged (verified) |
|---|---|---|
| ARKL2 | ✅ already applied (2026-10-16) | — (current through 10-15) |
| **GLNLG** | ❌ not yet | 24 days, DOY 265–288 (2026-09-22 → 10-15) |
| **HKRT** | ❌ not yet | 18 days, DOY 271–288 (2026-09-28 → 10-15); gappy (real dropouts) |

Staged at:
```
/mnt/seiscomp_staging/seiscomp_archive/2026/DU/GLNLG/EHZ.D/DU.GLNLG.00.EHZ.D.2026.{265..288}
/mnt/seiscomp_staging/seiscomp_archive/2026/DU/HKRT/EHZ.D/DU.HKRT.00.EHZ.D.2026.{271..288}
```
UTC-day-aligned, int32/STEIM2, minute-jitter bridged, real gaps preserved, no
masked data. Both **absent from LT → purely additive**.

## Do this (on dev1, revive staging CIFS mount first if stale)

```bash
cd ~/projects/SubSurfObs/sds_staging_ledger
# GLNLG — expect write=24 override=0
python3 apply.py --staging-root /mnt/seiscomp_staging/seiscomp_archive \
  --lt-root /mnt/seiscomp_archive --ledger-root ./seiscomp_archive \
  --net DU --sta GLNLG --mode decide --source-kind seismosphere_backfill \
  --source-extra-json '{"src":"seismosphere/eqwaveextractor","duration_min":1440}'
#   dry-run first, then re-run with --commit
# HKRT — expect write=18 override=0   (same command, --sta HKRT)
```

## Verify (currently 204 → expect 200)

```
fdsnws/dataselect/1/query?net=DU&sta=GLNLG&loc=00&cha=EHZ&start=2026-10-05T00:00:00&end=2026-10-05T00:10:00
fdsnws/dataselect/1/query?net=DU&sta=HKRT&loc=00&cha=EHZ&start=2026-10-05T00:00:00&end=2026-10-05T00:10:00
```

## Notes

- **Metadata:** each download bundled `DU.<SITE>.xml`, but it's non-canonical
  (sensitivity-only, no PAZ, net code can be wrong) → route to
  `uom_seismic_metadata`; **do not** archive it as the response. FDSN is truth.
- **HKRT gappiness is real** (telemetry dropouts), preserved — not an ingest fault.
- **Cleanup after apply:** VM `~/seismosphere_incoming/{GLNLG,HKRT}/` and the
  staged days are disposable once in LT.
- **Next:** a 3-site (ARKL2/GLNLG/HKRT) daily trailing-window cron on the VM for
  ongoing catch-up — designed, not yet wired.
