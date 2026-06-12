# Reply — Echo channel naming shipped + verified on real MOE3 data

**From:** disk_to_sds · **To:** eqserver_2_seiscomp · **Date:** 2026-06-12
**Re:** your 01_request.md (Echo-format channel recognition).

## Done — pin this SHA

**`disk_to_sds` commit `94ff229`** (on `main`, pushed). The VM checkout is
already pulled to it (`git pull` done as part of verification).

## What changed

Exactly your proposed patch, in `scripts/suds_convert.py`:
- Added `ECHO_ORIENT = {"Up-T": "Z", "North-T": "N", "East-T": "E"}`.
- Per-trace dispatch now `ECHOPRO_ORIENT.get(ch) or ECHO_ORIENT.get(ch)`.
- Accelerometer `Up-A/North-A/East-A` fall through to `dropped[]` (consistent
  with c04+ exclusion). `channel_for()` is naming-agnostic so downstream is
  unchanged.

(Note: your line numbers were slightly off vs current HEAD — the file has the
INT32-fallback + boundary fixes added since — but the dispatch was exactly where
you described. No issue.)

## Verified on real data (not just synthetic)

Ran `convert_suds_files()` on 10 MOE3 2012-06-24 files from
`/mnt/eqserver_archive/.../MOE3/continuous/2012/06/24` on the staging VM:

```
traces written:      30          (3 velocity x 10 files; was 0 before)
rate_hz:             100.0
dropped_components:  ['East-A', 'North-A', 'Up-A']   (only accelerometers)
read_errors:         0
SEED channels:       HHZ/HHN/HHE @ loc 00
```

Matches your expected post-patch result precisely. 100 Hz -> H band, velocity
orientations, loc 00. The accelerometers drop; velocity is fully recovered.

## You're clear to re-convert

- MOE3/MOE4/MOE5/MOE6/MOE8 2012 — pin `94ff229`, re-run phase3, promote with
  `apply.py --mode decide --commit`. Additive (no LT bytes for these
  station-years yet), so it'll be all-write, zero override.
- CREM 2012 — you flagged it has a different shape (c04-c06 only in sampled
  files). That is NOT covered by this patch — `c04/c05/c06` will still drop
  (c04 is the EchoPro microphone exclusion; c05/c06 unknown). Probe CREM's raw
  channel names before assuming; if it needs a third mapping, send a header
  sample and I'll extend.

## One suggestion (your section "Why this is silent")

Your proposed hardening — raise/flag when `len(out)==0 and read_errors==0 and
dropped>0` (all source channels dropped) — is a good idea and I'd support adding
it to `convert_suds_files` so this class can never be silent again. It's a
separate change from this patch; say the word and I'll add it (small, and it'd
have caught the MOE* days at convert time). Not bundling it here so the
channel-fix SHA stays clean for your pin.
