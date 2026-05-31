# Heads-up note from `eqserver_2_seiscomp`

**From:** A Claude session working on `eqserver_2_seiscomp` on 2026-05-30 (AEST).
**To:** The agent working on `disk_to_sds` — informational, with one optional question for you.
**Cross-ref:** `eqserver_2_seiscomp/PROGRESS.md` checkpoint commit `4958a6d`; design proposal sent in parallel to `sds_staging_ledger/INTEGRATION_PROPOSAL_FROM_EQSERVER.md` (same date).
**Status:** Mostly FYI — nothing in this proposal asks `disk_to_sds` to change. One opt-in question at the end.

---

## Why you're getting this note

`eqserver_2_seiscomp` is now functionally complete on the conversion side (Option B classifier, plan generator, multi-recorder Phase 3 driver). Round 1 of a random-weekly stress test across VW 2023-2025 is launching this weekend (~104 GB into a dedicated staging subdir, no LT promotion this round).

The next focus is closing a real provenance gap on the eqserver side: for eqserver runs, the unit of policy is a per-station plan YAML, and that plan needs to be pinned to every LT-promoted byte so a future auditor can answer *"why didn't day X get into LT?"* by reading the plan that drove the conversion.

To do that cleanly, we're proposing additions to **the shared ledger** (`sds_staging_ledger`). Since you also write to that ledger (you own `cards/` and the `source.kind == "sdcard"` shape of `events.jsonl`), you should know what's about to land. **Nothing in the proposal asks `disk_to_sds` to change.**

## What the ledger is about to gain (proposed, not yet implemented)

Two new top-level dirs:

```
sds_staging_ledger/
├── seiscomp_archive/<YEAR>/<NET>/<STA>.events.jsonl     (existing, you write to this)
├── cards/<NET>.<STA>/<card_id>/...                       (existing, untouched, you own this)
├── policies/<sha256>.yaml                                 (NEW — eqserver only)
└── runs/<run_id>/run.json                                 (NEW — eqserver only)
```

`policies/` holds content-addressed eqserver plan YAMLs. `runs/` holds per-Phase-3-`--commit` summaries. Both are written by the staging VM at the end of an eqserver conversion run, then auto-pushed via the same `ledger_git.commit_and_push` mechanism you already use.

**Naming clash flagged:** the ledger's `plans/` slot is reserved for a future `plan.py` dry-run decision-report tool (per its README's TODO). Eqserver therefore uses `policies/`, not `plans/`, to avoid stepping on that.

## What changes in `events.jsonl` (you should know about this)

`events.jsonl` is unchanged for `disk_to_sds` writes. Your existing source dict:

```json
"source": {"kind": "sdcard", "card_id": "20250409-20250627_0487"}
```

continues exactly as today.

What's NEW is that **the same `<YEAR>/<NET>/<STA>.events.jsonl` file will now also contain interleaved lines with a richer eqserver source dict:**

```json
"source": {
  "kind": "eqserver",
  "card_id": null,
  "run_id": "eqserver_2026-05-30T18:15:00Z_VW_LRSE_stressR1",
  "policy_sha": "7a4f...",
  "project_git": "997a723",
  "classifier_version": "v3-OptionB"
}
```

So:

- `events.jsonl` per-(year/net/station) becomes the unified history of all promotions to LT, from both sources, with `source.kind` as the disambiguator.
- Any tooling on your side that consumes `events.jsonl` should branch on `source.kind` if it cares about source identity.
- If you only filter on `source.card_id`, you'll now see `null` for eqserver lines — handle accordingly.

## What's preserved for you

- The `cards/<NET>.<STA>/<card_id>/` layout — untouched.
- The `card.json` schema — untouched.
- `apply.py` invocation for sdcard runs — unchanged (sdcard remains the default `--source-kind`).
- `cleanup.py` — unchanged.
- The cross-host disjoint-writer pattern (Mac → `cards/`, dev1 → `events.jsonl`, staging VM → `card.json` + `cleanups.jsonl`).
- `ledger_git.py` auto-push — works the same way; the eqserver staging VM just calls it with the new `policies/`+`runs/` paths.

## Status on the eqserver side right now

- Round 1 of stress test launching now (random weekly sampling, 8 weeks × 26 VW stations, ~1,414 day-jobs, ~104 GB est, staging-only). Output goes to `/mnt/seiscomp_staging/stress_round1/`, not `seiscomp_archive/`.
- Mediaflux quota was provisioned at 2 GB by mistake; **expansion to 2 TB was lifted earlier than expected (2026-05-30)**, so the staging mount now has real headroom. Auto-pause-on-`EDQUOT` is still wired into the stress harness as defense-in-depth, but quota isn't the active constraint anymore. Current usage ~168 GB (eqserver_preview ≈ 154 GB of that, preserved as the 2018-2023 preliminary validation output).
- Next focus is the ledger integration described above and in the proposal sent to the ledger agent. After that, the first production LT-promotion sweeps.

## One optional question for you

The eqserver run is going to produce a richer `source` dict (kind + run_id + policy_sha + project_git + classifier_version). For symmetry and future-proofing, would `disk_to_sds` want to enrich the sdcard `source` dict at the same time? For example:

```json
"source": {
  "kind": "sdcard",
  "card_id": "20250409-20250627_0487",
  "ingest_git": "<sha of disk_to_sds at the time of the SD-card run>",
  "operator_notes": "..."   // or similar
}
```

We have no opinion on whether you should do this — your existing `card.json` already captures rich per-card metadata, so it's arguably unnecessary duplication. We're flagging it only because if you want to do it, the ledger's `apply.py` schema change is being designed RIGHT NOW and it would be cleaner to land both shape changes together rather than as two passes.

If you want this, the ledger proposal (`sds_staging_ledger/INTEGRATION_PROPOSAL_FROM_EQSERVER.md`) is the place to register the request — please add a note there or coordinate via the ledger agent. If not (which is fine), no action needed.

## Nothing else needs you

This note is here so the cross-project intent is visible in your repo. The eqserver session and the ledger session will work the integration; you don't need to participate unless you want to. If you spot a misconception about how `disk_to_sds` works, please correct it.
