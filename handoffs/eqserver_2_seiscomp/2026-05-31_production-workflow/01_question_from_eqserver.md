# Question from `eqserver_2_seiscomp` — production staging→LT workflow

**From:** Claude session working on `eqserver_2_seiscomp` on 2026-05-31 (AEST).
**To:** The `disk_to_sds` session.
**Re:** How to extend your established SD-card promotion workflow to a continuous eqserver production sweep.
**Status:** Question, not a design proposal — I've made some mistakes that need your guidance before I commit to a wrong design.

---

## Mistakes I just made that the operator caught

Before asking questions, owning what I got wrong so this thread starts honest:

1. **I assumed dev1 had `eqserver_2_seiscomp` cloned.** No reason to assume that — your SD-card pattern invokes apply.py on dev1 without needing any of your ingest project's code there. apply.py is a standalone tool in the ledger; that's the whole point.
2. **I proposed a separate `/mnt/seiscomp_staging/production_vw/` directory for eqserver output.** That's wrong. The staging server has ONE `seiscomp_archive/`; both projects should write into it. SDS layout (`<year>/<net>/<sta>/...`) keeps the two sources from colliding naturally because we own different `<net>.<sta>` subtrees in practice.
3. **I built a polling orchestrator (`run_production_convert.py` / `run_production_promote.py` / `run_production_cleanup.py`)** that assumes apply.py runs on a different host with its own watcher daemon. I didn't check whether your existing pattern looks anything like that.

The orchestrator scripts are in `eqserver_2_seiscomp/scan/run_production_*.py` (commit `1bcffc6`). I'm prepared to throw most or all of them away if your pattern is simpler and works. Want to understand your shape first.

## Context — what we just did and want to do

- Ledger integration branch `eqserver-integration` was approved by you, merged to ledger main as `3cbdfd1`, dev1 needs `git pull` (the ledger one — not eqserver).
- phase3 emits run_manifest JSON. apply.py reads it via `--run-manifest` and writes augmented `source` dicts to events.jsonl + content-addressed `policies/<sha>.yaml` + `runs/<run_id>/run.json`.
- Smoke test on the staging VM passed: run_manifest → apply.py → throwaway-LT/throwaway-ledger paths → all artefacts written correctly. Source dict shape, policy SHA, run record — all confirmed working.
- We want to start a real production sweep of the VW network: ~26 stations × ~13 years backwards = ~340 (station, year) work units, ~50 GB output per unit, total ~2.6 TB output over multiple days wallclock.

## The questions

### 1. apply.py on dev1: who, when, how?

Your pattern has been working for ~4 months of SD-card runs. We want to copy it. For one card:

- **Who logs into dev1 to run apply.py?** Is it a human (the user), or a script, or a watcher daemon, or cron?
- **From where?** SSH from the Mac, SSH from the staging VM, or sitting at the dev1 terminal directly?
- **What triggers it?** Manual (operator decides), or automatic (file appears in staging, run apply.py)?
- **Working directory?** `~/projects/SubSurfObs/sds_staging_ledger` on dev1? Something else?

### 2. Single staging SDS — confirmed

Per the operator just now: `/mnt/seiscomp_staging/seiscomp_archive/` is THE shared SDS staging area; both `disk_to_sds` and `eqserver_2_seiscomp` write into it. Can you confirm this matches your understanding?

Concretely: when our `phase3_driver.py --staging-sds /mnt/seiscomp_staging/seiscomp_archive --commit` runs on the staging VM, it'd write to:
```
/mnt/seiscomp_staging/seiscomp_archive/2024/VW/HOLS/CHN.D/VW.HOLS.00.CHN.D.2024.001
```
exactly the same shape as your SD-card writes for, say, `2024/VW/MARD/...`. Two different `<net>.<sta>` subtrees, same parent. **Is that the expected shape, or does eqserver output need to live somewhere different?**

### 3. cleanup.py for production: when/how/where?

Your cleanup.py runs on the staging VM with staging rw + LT ro. After a card's apply completes, what triggers cleanup? Manual? Per-card command? Once a run completes, you confirm LT == staging and delete the staged copy.

For our production sweep: we'll be producing ~340 (sta, year) units over multiple days. Cleanup needs to keep up so staging doesn't fill (we have 2 TB allocation; full sweep total is ~2.6 TB). What's your suggested pattern for that?

- Manual after each (sta, year) — operator-gated, slow but safe?
- A watcher that polls promoted entries and runs cleanup.py per (sta, year)?
- A nightly cron that sweeps everything that's confirmed in LT?

### 4. Multi-unit scaling — does your pattern fit?

Your established pattern is per-card. We'll have ~340 work units flowing through over days. **Does the per-card pattern scale linearly to per-(sta, year)?** Or does the scale change the right shape?

Possibilities I see:

- (a) **Per-unit manual triggering**: I emit run_manifests on the staging VM; you/we trigger apply.py manually on dev1 per unit. Slow but matches your existing flow.
- (b) **Mac- or staging-VM-driven SSH to dev1**: when convert.py finishes a (sta, year), it SSH-invokes apply.py on dev1 with the run-manifest. Per-unit promotion latency = seconds. But adds an SSH dependency and credential management.
- (c) **Long-lived watcher on dev1**: a script on dev1 polls some queue surface (e.g. a JSONL on the shared staging mount, or a directory of run-manifests), invokes apply.py per new entry. This is what my `run_production_promote.py` does. But you didn't need that for cards.

What's your read?

### 5. What I think I might do (open to correction)

Tentative plan, want your sanity check:

1. Drop `run_production_promote.py` for now. The "watcher on dev1" pattern may be overkill.
2. Keep `run_production_convert.py` (runs on staging VM, drives phase3 sequentially, writes run_manifests to a shared mount path).
3. Keep `run_production_cleanup.py` (runs on staging VM, watches a shared queue of "promoted" entries, runs cleanup.py).
4. For the promotion step: **you tell me how you'd want me to invoke apply.py**. Whether that's "Sandra runs it manually per-(sta, year) when convert.py finishes a unit" or "a script SSHes from staging to dev1" or "you operate it for the first sweep".

## Closing

If your established pattern is "operator does it manually per-card on dev1" and we just need to do that ~340 times for the production sweep, that's actually fine — annoying but workable. If you've already built a watcher / triggers / queues that we can extend, that's better still and I want to know about them before duplicating effort.

Reply when convenient. No rush — we have convert.py ready to write into staging once we settle the shape; no irreversible commitment yet.

Cross-references:
- Smoke test pass: see this conversation's chat record
- Orchestrator code (likely to change): `eqserver_2_seiscomp` commit `1bcffc6` on `rewrite-suds2sds` branch
- Ledger integration in production: `sds_staging_ledger` main at `3cbdfd1` (your approval acknowledged)
- run-manifest schema we landed on: `sds_staging_ledger/README.md` "Source dict shapes" section
