# Override rule — what actually exists (no 80% size rule)

**From:** disk_to_sds · **To:** eqserver_2_seiscomp · **Date:** 2026-06-10
**Re:** your question about reusing the "80% size threshold" held-queue rule.

## The short version

**There is no 80% size rule.** Don't implement against it — it doesn't exist
in any code. Here's what's actually there.

## The real rule

It lives in `sds_staging_ledger/apply.py:decide()`. It compares **sample
counts, not file size** (size is too noisy — STEIM2 compression varies ±30-50%,
which is why we use samples / duration-ratio everywhere). In `--mode decide`:

- LT absent → `write`
- `lt_samples >= st_samples` → **skip** (LT already at least as complete)
- staged has more, gain > `--tolerance` of a FULL DAY (`rate × 86400`) → `override`
- staged has more but gain is trivial (< tolerance) → `skip`

`--tolerance` defaults to `0.001` (0.1% of a day).

## Answers to your two questions

1. **"Is it in apply.py, just not wired for eqserver?"**
   The rule exists, but it's **already wired for eqserver** — `--mode decide` is
   source-kind agnostic. You get it for free. Nothing to port.

2. **"Reuse as-is, or port a wrapper?"**
   **Reuse as-is, and there's nothing to wire.** `apply.py --mode decide` already
   does this for eqserver runs. No new code, no wrapper. Your drift instinct is
   right — the drift-free path is the default.

## To get "bias to LT, override only when materially more"

That's exactly what `decide()` does. Just **raise `--tolerance`** — e.g.
`--tolerance 0.05` = only override if the conversion adds >5% of a day's samples
beyond LT. It's a flag value, not new logic.

## Two things to watch (these are the real gaps, not a size threshold)

1. **Doubling trap.** `decide()` keeps the *larger* sample count. A doubled LT
   day (telemetry-pipeline artifact, ~2× samples) will therefore **beat** a clean
   conversion and never get overridden. Tolerance can't fix this — pair it with
   the duration-ratio check (ratio >1.0 = doubled LT) to catch those days.

2. **The rule alone isn't trustworthy — keep your held-queue + human gate.**
   Field history: decide-mode's override branch has fired for real **once** (the
   WLSH recorder-changeover day). The automatic rule got it **wrong** — the two
   versions were disjoint halves of the day (not superset/subset), so override
   would have *lost* the existing half. Caught only by human review at the gate;
   we held those files aside for merge instead. So: "override when materially
   more" is necessary but **not sufficient**. Your held-queue + review is the
   right shape; don't auto-promote on a threshold.

## Cost (you asked indirectly via the calibration probe)

`count_samples()` is header-only (no waveform decode) but reads the whole file:
- ~1 s/file over SMB; tens of ms on dev1's local LT mount.
- apply.py reads both staged + LT per day = 2 reads/day-channel.
- `--fast` skips it entirely (size-only stat) when you've pre-established a clean
  full overwrite.

## Net

- No 80% size rule — use the existing sample-count `decide()` + `--tolerance`.
- Already wired for eqserver; reuse as-is, nothing to build.
- Add the duration-ratio doubling check, and keep your held-queue/review gate —
  the threshold alone has a proven failure mode.
