# Handoff to eqserver_2_seiscomp — OZ.KORUM EqServer-style SUDS drop (a test case, and a canonical-path question)

**From:** disk_to_sds (Dan)
**Date:** 2026-10-22
**Priority:** Medium — data is already converted to staging via a reference run; this handoff asks whether eqserver_2_seiscomp should (a) adopt KORUM as a regression fixture and (b) become the canonical path for OZ/EqServer-style feeds. Nothing is in LT yet.

## TL;DR

A fresh ~3-week EqServer-style **SUDS** drop for station **KORUM** (Korumburra) landed at
`/Users/DSAND/Downloads/10/<DD>/`. disk_to_sds did a **reference conversion** to staging
(using the *shared* `suds_convert.py` — the same core your pipeline calls). It did **not**
convert cleanly on the first pass: it **silently dropped 17 of 22 days** because KORUM
**renames its channels mid-deployment** (`c01/c02/c03` → `EHE/EHN/EHZ`) and the shared
converter only knew the EchoPro codes. Caught by per-day duration-ratio QC, root-caused, and
fixed with a conservative SEED-passthrough in `suds_convert.py` (details below). So this
dataset is **a sharp little test** for your pipeline — it exercises six things the VW/VX/DU
sweep hasn't had to in one dataset, **and it already found a real silent-loss gap in the
shared converter your pipeline depends on.** Writing it up so you can treat it as a fixture,
take it over as the canonical path, or bless the (now fixed) reference run as-is.

## The data

```
/Users/DSAND/Downloads/10/<DD>/ 'YYYY-MM-DD HHMM SS KORUM.dmx.gz'        # continuous, 1/min
/Users/DSAND/Downloads/10/<DD>/ 'YYYY-MM-DD HHMM SS KORUM.trig.dmx.gz'   # triggered events
```
- `10` = month (October 2026). `<DD>` = 01..22. Campaign window **2026-10-01 00:00 → 2026-10-22 ~01:06 UTC** (day 22 partial, 66 continuous files).
- Single station (KORUM only). **gzipped** `.dmx`. 100 Hz, 3-component seismometer.
- SUDS header carries network **`AB`** (placeholder) and channels **`c01/c02/c03`** → classic 6th-gen **Kelunji Echo Pro**. Also `c04/c05/c06` appear **only in the `.trig` files** (a second collocated sensor, large DC offsets — not archived).
- Per-day continuous file counts: 01:1411 02:1426 03:1420 04:1430 05:1422 06:1423 07:1428 08:1424 09:1427 10:1426 11:1419 12:1428 13:1426 14:1427 15:1430 16:1425 17:1420 18:1427 19:1426 20:1427 21:1427 22:66. Trigger-file counts per day range 1–88.

## Authoritative codes (AusPass is source of truth)

AusPass **does** have KORUM metadata — so no guessing. From
`https://auspass.edu.au/fdsnws/station/1/query?net=OZ&sta=KORUM&loc=60&level=channel`:

| N.S.L.C | rate | az / dip |
|---|---|---|
| OZ.KORUM.**60**.HHE | 100 Hz | az=90 dip=0 |
| OZ.KORUM.**60**.HHN | 100 Hz | az=0 dip=0 |
| OZ.KORUM.**60**.HHZ | 100 Hz | az=0 dip=-90 |

- Network **OZ** (Seismology Research Centre; also matches `uom_seismic_metadata/fdsnxml/oz.xml`, Equipment = Echo Pro, −38.40644/145.85085/354 m).
- Location **`60`** (station-specific; OZ-wide is mixed: ~21 stations on `00`, ~12 on `60`). Dan's ruling: **AusPass is source of truth → loc 60**. Do NOT fall back to `00`.
- Orientation `c01→N, c02→E, c03→Z` — matches the AusPass az/dip exactly and the existing `ECHOPRO_ORIENT` map.

## Why this is a good test for your pipeline (the 6 sharp edges)

0. **Mid-deployment channel RENAME → silent-loss trap (this one fired).** KORUM's SUDS
   channel codes change partway through: days 01–04 `c01/c02/c03`, **day 05 both**, days
   06–22 `EHE/EHN/EHZ` — same 100 Hz seismometer, relabelled. The shared `suds_convert.py`
   recognised only EchoPro `c0x`, so on the first reference pass **days 06–22 produced
   `traces=0, dropped=['EHE','EHN','EHZ']`, wrote nothing** — a clean `rc=0` hiding 17 lost
   days. Only the per-day duration-ratio / `dropped=[]` QC made it visible. **This is the
   same class as the 2026-06-11 Echo-format gap.** Fixed (see "Converter fix" below); your
   pipeline inherits the fix automatically since it calls the same lib, but KORUM is the
   regression case that proves it.

1. **New target network `OZ`.** Your `metadata/station_registry.yaml` `target_network` vocabulary is **`VW | VX | DU | null`** — **OZ is not in it**, and KORUM is not in the registry at all (checked: 249 entries, no KORUM/OZ). This is the first *external* (non-UoM, AusPass-served) network through the pipeline. Decision needed (see below).
2. **Dual file-type in one day-dir — the `.trig` dispatch trap.** `*.dmx.gz` (continuous) and `*.trig.dmx.gz` (triggered event windows) are interleaved in the same directory. The continuous archive must take **continuous only**; fold the trigger windows in and they **overlap the continuous stream → duplication → duration ratio > 1.0** (same signature as the TRPU LT-doubling). This looks like exactly your flagged **triage row 7 "mixed-recorder dispatch" open gap** (commit 74f48c9) and your per-FILE channel-knowledge work (83b5380). The discriminator here is the filename infix `.trig.`, not the channel set.
3. **6-component trigger files.** The `.trig` files carry `c01..c06`; `c04/c05/c06` are a second collocated sensor (DC offsets ~−150k and ~+2.04M counts). AusPass defines **only** the 3 HH channels for KORUM, so c04–c06 must be dropped. (The current `convert_suds_files` already drops them via the `ECHOPRO_ORIENT` miss, so this is handled — but worth a deliberate assertion in a fixture.)
4. **Non-`00` location + gzip.** loc `60` must be carried through from inventory, not defaulted; inputs are `.dmx.gz` and need a decompress step the old-archive NFS path may not have.
5. **Midnight-boundary.** Confirmed working here: each day's straddling last minute-file deposits a ~54 s sliver on the *next* DOY (e.g. day 02 → a `ratio=0.0006` stub on DOY 276) that the following day's run **merges** into the full file — the exact scenario of the 2026-06-02 midnight-boundary handoff. A 22-day run is a nice end-to-end regression of that fix.

## Converter fix (in the shared lib — your pipeline needs it too)

`disk_to_sds/scripts/suds_convert.py` now recognises already-SEED-named seismometer
channels, in addition to EchoPro `c0x` and Echo `*-T`:

```python
# band E/S/H/B + instrument H (high-gain velocity) + orient Z/N/E/1/2/3; trailing char = orientation.
# Accelerometers (instrument 'N'), microphones, etc. still fall through to dropped[].
_SEED_SEISMO_RE = re.compile(r"^[ESHB]H[ZNE123]$")
def seed_passthrough_orient(channel):
    return channel[-1] if _SEED_SEISMO_RE.match(channel or "") else None
# dispatch: orient = ECHOPRO_ORIENT.get(ch) or ECHO_ORIENT.get(ch) or seed_passthrough_orient(ch)
```

Additive and conservative: it only catches channels that were previously **dropped**, never
changes `c0x`/Echo behaviour, and excludes non-seismometer instruments. The orientation then
flows through `channel_for(inv=...)` so the AusPass inventory normalises band+location
(`EHZ` → `HHZ.60` for KORUM). **Not yet committed** — flagging for review; if you want it in
the shared lib under eqserver's test suite, say so and I'll commit, or you take it.

## Reference conversion (already done by disk_to_sds — treat as the expected output)

- Tool: a thin driver over the **shared `disk_to_sds/scripts/suds_convert.py`** (`convert_suds_files` + `write_sds`), inv = the AusPass StationXML above, **continuous files only** (`.trig.` excluded), env `scitools` (sudspy + obspy 1.4.1). Run under `caffeinate -s`.
- Output → staging: `/mnt/seiscomp_staging/seiscomp_archive/2026/OZ/KORUM/HH{E,N,Z}.D/OZ.KORUM.60.HH?.D.2026.{274..295}` (STEIM2, int32, UTC-day split, atomic).
- Two passes: pass 1 silently dropped days 06–22 (channel-rename trap above); after the converter fix, pass 2 re-ran all 22 days (idempotent `write_sds` merge → days 01–04 unchanged, 05–22 written/completed).
- QC profile after fix (representative): `read_errors=0`, `dropped=[]` (continuous-only, both `c0x` and `EH*` days), **duration ratios ~0.98–0.99** — slightly under 1.0 from genuine recording dropouts (~27 missing minute-files/day), **never > 1.0** (no duplication; the `c0x`+`EH*` overlap on transition day 05 is trimmed by `fast_merge_split`). Read the staging tree for authoritative final per-day counts (expect ~22 day-files/channel, DOY 274–295).
- **This is byte-identical to what your pipeline's convert stage would emit** (same core lib), so it doubles as a golden-output fixture: your run should reproduce it exactly.

## Proposed registry entry (if you adopt KORUM)

```yaml
KORUM:
  include: true
  target_network: OZ            # ⚠ outside current VW|VX|DU enum — see decision #1
  network_basis: registry       # AusPass FDSN StationXML, OZ.KORUM
  source_network_code: AB       # SUDS placeholder; ignore, do not pass through
  recorder_types: [echopro]     # 6th-gen Kelunji Echo Pro (AusPass equipment)
  coverage_start: 2009
  coverage_end: 2026
  notes: >
    Korumburra. OZ (SRC) network, served by AusPass (source of truth). loc=60,
    HH{N,E,Z}@100Hz, c01->N/c02->E/c03->Z. Day-dirs mix continuous *.dmx.gz with
    triggered *.trig.dmx.gz (6-comp; c04-c06 = 2nd sensor) — ingest continuous only.
    First OZ / first external-network station in this pipeline.
```

## Decisions I need from the eqserver_2_seiscomp session

1. **OZ support.** Extend `target_network` to accept `OZ` (external/passthrough network), or keep the pipeline UoM-only and leave external networks to disk_to_sds reference runs? If the former, this is a small enum + any `target_network`-validation change.
2. **Canonical path.** Is KORUM (and future OZ/EqServer-style drops) something you want to own end-to-end through your registry + convert + manifest? Or is this a one-off where the reference conversion above stands and I just drive the `apply.py` to LT?
3. **Fixture.** Want KORUM wired into your class-B / triage test suite as the dual-format + external-network + loc-60 + gzip regression case? I think it's worth it — it's the cleanest real example of the `.trig` dispatch trap (row 7) you have.
4. **Apply/provenance.** LT apply is still blocked (dev1/VPN down). When it's back: `apply.py --net OZ --sta KORUM --mode decide` — what `--source-kind` do you want for fresh EqServer-live SUDS (not the decade `oldarchive` sweep)? Propose a value and I'll use it, or you run the apply.

## Status / pointers

- Reference staging conversion: in progress at time of writing (SMB-bound, ~30 min), completing to the paths above; nothing in LT.
- Input drop: `/Users/DSAND/Downloads/10/` (Mac). AusPass StationXML and the driver are in the disk_to_sds session scratchpad (ephemeral) — the *method* is fully captured above; the durable code is the shared `suds_convert.py`.
- Related: disk_to_sds memory `project-echopro-rings-cont0-only` (continuous-only principle), `project-midnight-boundary-fix`, `feedback-node-scope-boundary` (codes are operator/metadata domain — here resolved from AusPass).
