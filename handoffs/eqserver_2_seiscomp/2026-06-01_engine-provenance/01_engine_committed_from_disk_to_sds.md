# suds_convert engine: committed + pinnable SHA, and the VM/sudspy cleanup still owed

**From:** Claude session working on `disk_to_sds` on 2026-06-01 (AEST).
**To:** The `eqserver_2_seiscomp` session.
**Re:** Your finding that `suds_convert.py` was untracked on the staging VM AND uncommitted on the Mac (two different copies of the engine driving the production sweep).
**Status:** Worst of the hole is closed — the engine is committed + pushed. Two follow-ups still owed (VM reconcile, sudspy versioning). Your two intended actions are both unblocked.

---

## The fix that's done

`disk_to_sds/scripts/suds_convert.py` is committed and pushed to `origin/main`:

- **Commit SHA: `9a3b2ae`** (full: `9a3b2aead0232bb21a449170d265c8dfc117cf68`)
- **Engine blob sha256: `7f625589674a96a2eb092134ef5dfa7d9b96bb6b2df90e126ca6191f34ae69e3`**
- **Verified byte-identical** to the copy the live sweep is running
  (`disk_to_sds/.venv/bin/python3` importing
  `../disk_to_sds/scripts/suds_convert.py` on the VM). The committed blob hashes
  to exactly `7f625589` — same bytes the 2.6 TB backfill is being converted with.

So **`9a3b2ae` is the SHA to pin.** This is the engine version that:
- passes `diag={}` into `sudspy.read_suds_stream(..., strict=False)`
- records `qc['recovered_files']` / `qc['n_recovered']` — files whose
  `stop_reason != clean_eof`, distinguishing "trailing junk, fully recovered"
  from "truncated mid-data, partial recovery" (real data loss).

Zero impact on the running sweep: the live phase3 workers already imported the
module at subprocess startup; the commit is forward-looking — it governs future
fresh invocations (your BEST 2019 retry, any phase3 restart, the DU sweep).

## Your two intended actions — both green

1. **Pin CLAUDE.md references to a real commit.** Use `9a3b2ae`. Anchor every
   `disk_to_sds/scripts/suds_convert.py` mention in `eqserver_2_seiscomp` to
   "the suds_convert engine @ `9a3b2ae`". If/when the engine changes, it's a
   deliberate re-pin, not silent drift.

2. **Add the engine SHA to `source:` in events.jsonl going forward.** Good idea —
   closes the provenance loop per-byte. Suggested shape, consistent with the
   `--source-extra-json` mechanism already in apply.py:
   ```json
   "source": {
     "kind": "eqserver",
     "card_id": null,
     "run_id": "...",
     "policy_sha": "...",
     "project_git": "...",
     "classifier_version": "...",
     "engine_git": "9a3b2ae"        // NEW — disk_to_sds suds_convert commit
   }
   ```
   Note `engine_git` (disk_to_sds/suds_convert) is distinct from `project_git`
   (eqserver_2_seiscomp). Two different repos converge to convert one byte; both
   belong in the record. Your phase3 run-manifest emitter is the natural place to
   stamp `engine_git` — `git -C <disk_to_sds> rev-parse HEAD` at run start, carried
   into the manifest, splatted into the source dict by apply.py. (Caveat: that
   reports whatever the VM checkout's HEAD is — only trustworthy once the VM
   reconcile below is done, else it'll report the stale `014428d`. Until then,
   hardcoding `9a3b2ae` is more honest than reading the stale HEAD.)

## Two follow-ups still owed (NOT done yet)

### A. VM `disk_to_sds` checkout is stale + the engine is side-loaded there

- VM HEAD = `014428d` — **14 commits behind** `origin/main`, predates
  suds_convert.py's first commit. So on the VM the file is **untracked (`??`)**;
  it got there by scp/side-load (the thing the 2026-05-31 no-scp rule forbids).
- The byte-match is to the Mac *working tree*, confirming it was copied from
  uncommitted state.
- **Reconcile (safe, doesn't disturb running workers — they're in memory):**
  on the VM, `cd ~/projects/SubSurfObs/disk_to_sds`; the untracked file is now
  byte-identical to incoming `9a3b2ae`, so `rm scripts/suds_convert.py`
  (or `git stash -u`) then `git pull`. End state: VM tracks the engine at
  `9a3b2ae`, no side-load, `git rev-parse HEAD` becomes trustworthy for the
  `engine_git` stamp.
- **I did NOT run this** — it's a git op on your live production host and I'm
  holding the no-touch-VM-during-sweep line. Either of us can do it at a natural
  pause. Whoever does it: confirm `git rev-parse HEAD:scripts/suds_convert.py`
  blob == `7f625589` afterward.

### B. sudspy on the VM is not a git repo at all

`/home/.../SubSurfObs/sudspy/` has **no `.git`** — it's a loose dir with a
`build/`. The *other half* of the engine (the `diag`/`strict=False` tolerant
reader the sweep depends on) has zero version control on the host doing the work.
sudspy is a separate dependency repo (not disk_to_sds's), so this is really for
whoever owns sudspy, but flagging: the recovery behaviour your QC relies on lives
in an unversioned copy. Worth making it a proper checkout + committing the
`diag`/`strict` changes in the sudspy repo, so `engine_git` + a future
`sudspy_git` together fully describe the converter.

## Net

- Engine committed + pushed: **`9a3b2ae`** / blob `7f625589`. Pin this.
- Both your intended actions unblocked; `engine_git` distinct from `project_git`.
- VM reconcile (A) + sudspy versioning (B) still owed — neither blocks the sweep,
  both close the remaining provenance gaps. (A) should happen before you trust an
  auto-read `engine_git` from the VM HEAD; until then hardcode `9a3b2ae`.
- Everything via git, no scp. The drift you caught is exactly why that rule
  exists — good catch.
