# Handoff: SmartSolo node adapter (Korumburra RDK campaign)

*Written 2026-10-07 from the quake-fetch session that processed the first
node harvest (P7). Goal: a `smartsolo_to_sds.py` adapter in this project's
Pull → Plan/Apply → Cleanup pattern. A node plugged in presents as an
external drive; one harvest = one mounted directory.*

## Context

15 SmartSolo nodes (sites P2–P16) are deployed around the Korumburra M4.7
(2026-10-02) epicentre, plus two redundancy 1C units (at P3 +80 m, and
adjacent to P7). Harvests will arrive repeatedly over the coming weeks.
One harvest (P7) has been fully processed by hand; everything below is
measured from it, not assumed.

- Deployment/instrument project (naming, serials, StationXML):
  `~/projects/SubSurfObs/korum_02102026/HANDOFF_DEPLOYMENT.md`
- Working prototype converter (single station, also does the 100 Hz
  derivative): `~/projects/SubSurfObs/quake-fetch/inspection/k180_reloc/build_p7_sds.py`
- Reference harvest to test against: `~/Desktop/ss_test/p7/`

## On-disk format (SmartSolo IGU-16HR 3C, 5 Hz; firmware V1.0.8.1be)

A harvest directory contains:

| File | Content |
|---|---|
| `device.ini` | `[deviceInfo] sn=<serial>` — **the identity key** |
| `DigiSolo.LOG` | serial + device type again, and periodic GPS fixes (`Latitude = …` / `Longitude = …` / `Altitude = …`) |
| `seis<SEG><C>.MiniSeed` | waveforms; SEG = 000,001,… ; C = X,Y,Z |
| `SCT_INT.XML`, `sct_par*.xml` | acquisition parameter sets (gain etc.) |
| `PULSE_X/Y/Z.WAV` | pulse-test records (response validation, not ingest) |

Waveform facts (all verified on P7):

1. **One file per component per rollover segment — NOT per day.** Segment
   boundaries are arbitrary times (P7: 20.3 h, 21.6 h, 19.2 h, 11.4 h),
   set by power-on + a size/sample-count rollover. NOT UTC-aligned.
2. X/Y/Z roll at identical instants; adjacent segments are
   sample-continuous (next starts exactly one sample after previous ends).
3. **Embedded trace IDs are junk** (`XX.TEST..LHZ` regardless of
   component). The component is carried ONLY by the filename letter.
   Mapping: X→E, Y→N, Z→vertical. Horizontal orientation assumes the
   node's north arrow was field-aligned — per-site metadata, flag it.
4. 2000 sps, 32-bit, continuous within segments, GPS-disciplined timing
   (P7 showed ~0.03 s residuals against the permanent network — clean).
5. GPS fixes: P7 gave 540 fixes over 3 days with <3 m scatter. Median of
   all fixes = the position recipe. Parse regex:
   `Latitude\s*=\s*(-?\d+\.\d+)` (same for Longitude/Altitude).

## Adapter pipeline (proposed, matching this repo's pattern)

1. **Identify** (cf. `identify_echopro.py`): read serial from
   `device.ini`, GPS median from `DigiSolo.LOG`, device type, record
   window. **Reconcile serial → station** by nearest-site match against
   the registry (`korum_02102026/rdk/sites.csv`; field pins good to tens
   of metres, so matching is unambiguous — this also resolves the sites
   where the serial wasn't written down). Maintain the serial↔site table
   as a durable manifest.
2. **Validate**: segment continuity (no gaps at joins), GPS-lock
   continuity in the LOG, sane sample rate, position scatter.
3. **Convert**: relabel from filename (net/sta/loc/chan per the naming
   scheme the deployment project settles — interim precedent is
   `VW.P7SS.00.EP?` at 100 Hz), merge all segments per component, cut at
   UTC midnights, write SDS day files (STEIM-compressed).
4. **Manifest**: per-harvest record — serial, station, window, gaps,
   position, file list — in `manifests/` like the other adapters.

## Policy decisions to make (not mine to pre-empt)

- **Archive rate.** Native 2000 sps ≈ 2 GB/day/node (3C, raw). Suggestion
  from the quake-fetch side: keep the raw segment files as the archive of
  record; produce a **100 sps SDS derivative** (45 Hz zero-phase lowpass →
  resample, as the prototype does) as the standard product — that is what
  the quake-fetch scan pipeline consumes. Build native-rate SDS only for
  windows where HF work is planned (sonification, spectral/EGF studies).
- **Channel codes**: `EP?` was the interim choice at 100 Hz output; a
  native-2000-sps product needs a different band code. Belongs to the
  deployment project's naming task.
- **Repeat harvests of the same node**: dedupe/overlap policy when a node
  is re-harvested without wiping (segments re-appear).
- **1C units**: file layout unverified — presumably `seis<SEG>Z` only.
  Confirm at first 1C harvest (P11/P14/P15/P16, or the P3/P7 redundancy
  units).

## Known constraints from the campaign

- The production VM is unreachable (since 2026-10-04), so Plan/Apply
  may need a local-Mac variant until it returns; the Korumburra sequence
  archive currently lives locally at
  `quake-fetch/quake-archive/korumburra_seq` (its `_p7_sds/` tree is the
  P7 100 Hz SDS already in use).
- `korum_02102026` repo is read-only for Claude sessions unless Dan
  explicitly asks (its registry is an *input* here, never a write target).
- Node responses are mock/uncalibrated until the deployment project
  delivers real poles & zeros — amplitudes from node SDS are not usable
  for magnitudes yet; timing and picks are fine.
