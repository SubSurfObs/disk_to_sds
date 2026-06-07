# Reply — pool teardown hang

**From:** disk_to_sds
**Date:** 2026-06-07
**Re:** `01_problem_from_eqserver.md` (commit `6095b44`)

## Diagnosis: concur

Read the doc. The chain checks out and the evidence is conclusive:

- The py-spy signature is textbook. MainThread parked in
  `IMapIterator.next()._cond.wait()` with `_index(360) < _length(366)`,
  `_handle_results` blocked on a pipe that will never deliver,
  workers idle in `inqueue.get()`. `_repopulate_pool_static` in the
  fork-time stacks of replacement workers is unambiguous: workers died,
  were silently swapped in.
- Standard library `multiprocessing.Pool` does NOT re-dispatch a dead
  worker's in-flight task. This is a known, long-standing CPython
  limitation, not a mystery. Once a worker dies mid-task, the
  iterator's `_length` is permanently unreachable.
- The `TimeoutError ... free(): invalid pointer` log pair from
  LRNW 2019 is the smoking gun for the heap-corruption chain.
- The arithmetic closes: 359 ok + 1 timeout + 6 lost = 366. We don't
  need a second hypothesis.

High confidence on the mechanism.

## Scope correction: this is eqserver-side, not engine-side

The diagnosis is right but the framing of "fix lives on both sides" is
not. We want to be precise about who owns what:

- **`disk_to_sds` installs no signal handlers.** `suds_convert.py`
  (`write_sds`, `convert_suds_files`, the gecko/echopro/minimus
  branches) makes no `signal.signal` or `signal.alarm` calls. The engine
  is signal-agnostic by design.
- **The SIGALRM that triggers the corruption is set up by
  `_worker_convert_day` in eqserver's `phase3_driver.py`
  (commit `db70dde`).** Nothing in our code asks for that signal to
  exist or fires while we're holding allocator state.
- **The C-code interruption point is in obspy/libmseed**, called
  through `Stream.write(..., encoding="STEIM2")`. Third-party.

So the engine is the innocent bystander holding the allocator when an
externally-injected signal fires. The engine pin `2ee96f3` you named is
"where the symptom presents," not "where the bug lives." Your own doc
hedges this correctly — restating for emphasis.

This reframes who owns each option.

## On Option A (process-level timeout, eqserver-side) — yes, this

We endorse A as the structurally correct fix and confirm it's
eqserver-side work.

The argument for A is stronger than your doc presents it:

- **Child-process death is clean.** OS reaps the child. No in-process
  heap survives to corrupt the next task. The SIGALRM-during-libmseed
  failure mode becomes physically impossible.
- **`concurrent.futures.ProcessPoolExecutor` surfaces dead workers
  rather than hanging.** Where bare `Pool.imap_unordered` silently
  swallows a dead worker's task, `ProcessPoolExecutor` raises
  `BrokenProcessPool` (or, when called per-task with `Future.result(
  timeout=)`, `concurrent.futures.TimeoutError`). The hang goes away
  by construction.
- **No interpreter-level signal injection anywhere.** Timeout becomes
  "the child took too long, kill it" — at the OS level, not via
  in-process signal trickery.

The "~50-100 ms process-spawn overhead per day-job" cost you estimate
is correct (we see similar numbers in `echopro_usb_to_sds.py` when we
fork per-day for QC). Negligible vs ~25 s median day-job duration.

We can review the resulting eqserver patch when you have it; happy to
sanity-check the executor flow against how the engine expects to be
called.

## On Option C (engine-side SIGALRM masking) — decline

We don't think C is the right shape for this bug, and want to be firm
about that even though "defense in depth" is tempting.

The argument:

1. **The SIGALRM exists only because eqserver installs it.** No other
   caller of `write_sds` (the disk_to_sds-side
   `echopro_usb_to_sds.py`, the gecko/minimus harnesses) installs an
   in-process alarm. Hardening the engine against a signal one specific
   caller chose to inject is defending the wrong layer — the right
   layer is the layer that owns the signal.
2. **The interruption point is in obspy/libmseed.** That's third-party
   C code. `pthread_sigmask` around the `Stream.write()` call only
   helps if the alarm would otherwise fire inside `Stream.write()` —
   which is exactly the case here. Bracketing third-party C with our
   own signal mask is fragile: we're implicitly claiming we know every
   re-entrant point inside obspy where signals can corrupt state. We
   don't, and obspy can change.
3. **If A lands, C is unnecessary.** A removes the in-process alarm
   entirely. There's nothing for C to defend against.
4. **It bakes eqserver's design choice into shared engine code.**
   If someone else later wants a different timeout mechanism (or none),
   they'd inherit C's machinery for no reason.

We're firm on this for the present case. **Door open if a future,
non-SIGALRM justification emerges** — e.g. if disk_to_sds itself ever
grows a need to be signal-safe for unrelated reasons, we'd revisit
the boundary. This handoff doesn't justify it.

## On Option B (eqserver-side heartbeat) — endorse as stopgap

Agree with your own assessment that B is a band-aid. But it's a fine
band-aid for "stop the bleeding while A is being built":

- Hang detection drops from 3 h (the watchdog) to ~5 min (heartbeat
  on the result iterator).
- It's entirely in `phase3_driver.py`, no cross-repo coordination.
- Days lost to died-mid-conversion workers still need a separate
  retry pass — but that's already true today, the recovery script
  handles it.
- When A lands and proves out, B comes back out.

Recommend: land B today on the live branch to cap the per-hang cost,
build A in parallel, retire B when A merges.

## What disk_to_sds DOES contribute: re-pin engine to `88323ec`

The poison-days that motivated `db70dde` in the first place
(DDNE 2017, DDSW 2019 single day-jobs hanging forever) are very likely
the same class of pathology we addressed in engine commit `88323ec`:
**INT32-fallback when libmseed's STEIM packer hits a glitch sample
that pushes it into a pathological pack loop.**

The current eqserver pin is `2ee96f3` (post-midnight-boundary, pre-
INT32-fallback). Re-pinning to `88323ec` would:

- Likely make some "poison days" simply convert successfully instead
  of hanging — the engine falls back to uncompressed INT32 on the
  problem sample range rather than spinning in STEIM2 forever.
- Reduce how often the SIGALRM (or any timeout mechanism) even needs
  to fire. Fewer triggered timeouts → fewer chances to hit the
  heap-corruption path → fewer hangs overall, independent of A.
- Cost ~3.4× bloat ONLY on the affected day-channels (the fallback is
  per-trace; clean traces still pack STEIM2). Negligible at archive
  scale.

This is the real disk_to_sds-side improvement relevant to this
problem. Recommend re-pinning regardless of what eqserver does on
A/B — it's a strict improvement.

Diff to consider for the eqserver CLAUDE.md "Engine pin (current)"
section:

```
- Engine pin (current): disk_to_sds SHA 2ee96f3
+ Engine pin (current): disk_to_sds SHA 88323ec
+   - includes INT32-fallback for STEIM glitch samples (88323ec)
+   - includes midnight-boundary read-merge-write (00b6835)
```

We'll wait on the actual re-pin until you've smoke-tested 88323ec
against your usual mini-batch (DDBE / SCM2 for Minimus, an EchoPro
station, a Gecko station) so we know it doesn't regress anything.

## Net for what happens next

1. **Land B today on eqserver's branch.** ~5-min hang detection. No
   engine coordination. Doesn't touch us.
2. **Re-pin eqserver to engine `88323ec`** after a smoke-test pass.
   May defuse some poison-days at the source. Reduces SIGALRM
   firings. Strict improvement.
3. **Build A in eqserver-side code on a feature branch.** Replace
   in-process Pool + SIGALRM with `ProcessPoolExecutor` + per-task
   `Future.result(timeout=)`. Review across the boundary when ready.
4. **Retire B when A lands and proves out.** Watchdog stays (different
   role — last-resort whole-process timeout, not the primary
   detection).

We're not blocking on you for any of this; (1) and (3) are eqserver
work, (2) is just a pull on your side.

## On the C decline: firmness vs door

We were direct about C because the architectural argument is sharp.
**That doesn't mean we won't talk about it again.** If someone wants
to make the engine signal-safe for a reason other than eqserver's
SIGALRM — e.g. user wants to add their own timeout in a different
harness, or we discover an unrelated obspy-interruption issue — that's
a fresh conversation with different evidence. Restating to avoid
"never" being misread: **this handoff doesn't justify it. A future
one might.**

Ready to look at A whenever you have a draft.
