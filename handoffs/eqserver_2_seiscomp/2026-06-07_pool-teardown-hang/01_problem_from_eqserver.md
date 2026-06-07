# Pool teardown hang — handoff to disk_to_sds

**From:** eqserver_2_seiscomp (Dan Sandiford)
**Date:** 2026-06-07
**Status:** Mid-sweep blocker. Hits ~1 unit per 10. Each hit costs 3h wall-clock + manual recovery + ~6 lost days per unit.

## TL;DR

Phase3 (`eqserver_2_seiscomp/scan/phase3_driver.py`) runs day-jobs through a
`multiprocessing.Pool.imap_unordered`. On certain unit-years, workers die mid-
task and `imap_unordered` hangs forever waiting for results that won't come.
The fix touches code on both sides of the boundary — we want to coordinate
rather than patch one side blindly.

Confirmed cases this sweep:

| Unit          | Day-jobs queued | Day-lines emitted | Lost results | Outcome                       |
|---------------|------------------|--------------------|---------------|--------------------------------|
| DDWB 2022     | unknown          | all                | 0             | self-resolved at ~30 min       |
| LOYU 2016     | 365              | 365 (all done)     | unclear       | killed manually at 3.4h        |
| LRNW 2020     | 366              | 360                | 6             | watchdog killed at 3h, recovered |
| LRNW 2019     | 365              | 240                | 125           | watchdog killed, partial recovery |
| LRWS 2020     | 366              | 360                | 6             | watchdog killed at 3h, recovered |

The "recovered" cases are reconstructed via
`eqserver_2_seiscomp/scan/recover_manifest_from_log.py` — rebuild the
run_manifest from the phase3 log + staging file inventory after the watchdog
fires. Works, but costs 3h wall-clock per occurrence plus operator time.

## What we observe

When the hang occurs:

- All day-jobs marked "ok" in the phase3 log emit cleanly. Some units emit a
  `timeout` day-line (the SIGALRM path — see below). All workers' SDS writes
  to staging complete normally for those days.
- After the last day-line, phase3 sits at 0% CPU. Log static. Watchdog kills
  it at 3h.
- The log shows `366 day-jobs queued` in the header but only 360 day-lines
  emitted (per-unit numbers vary).

The 6 missing days never had their results delivered to MainThread. Their SDS
files are also missing from staging — the workers that were processing them
died before the SDS write completed.

LRNW 2019 had this signature pair in the log immediately before the hang
became visible:

```
TimeoutError: day-job exceeded 600s wall-clock
free(): invalid pointer
```

That's the eqserver-side SIGALRM TimeoutError firing followed by glibc
detecting heap corruption in libmseed/obspy C state.

## Diagnostic — py-spy on a live hung phase3

py-spy 0.4.2 dump against LRWS 2020 phase3 (PID 1356179) right before the
watchdog was due to fire. Parent process (5 threads, all idle):

```
Thread 1356179 (idle): "MainThread"
    wait (threading.py:320)
    next (multiprocessing/pool.py:861)
    main (phase3_driver.py:868)
    <module> (phase3_driver.py:983)
Thread 1356196 (idle): "phase3_watchdog"
    _bark (phase3_driver.py:210)
Thread 1356206 (idle): "Thread-1 (_handle_workers)"
    select (selectors.py:416)
    wait (multiprocessing/connection.py:931)
    _wait_for_updates (multiprocessing/pool.py:502)
    _handle_workers (multiprocessing/pool.py:522)
Thread 1356207 (idle): "Thread-2 (_handle_tasks)"
    _handle_tasks (multiprocessing/pool.py:531)
Thread 1356208 (idle): "Thread-3 (_handle_results)"
    _recv (multiprocessing/connection.py:379)
    _recv_bytes (multiprocessing/connection.py:414)
    recv (multiprocessing/connection.py:250)
    _handle_results (multiprocessing/pool.py:579)
```

All 8 workers (one example):

```
Thread 1356201 (idle): "MainThread"
    _recv (multiprocessing/connection.py:379)
    _recv_bytes (multiprocessing/connection.py:414)
    recv_bytes (multiprocessing/connection.py:216)
    get (multiprocessing/queues.py:365)
    worker (multiprocessing/pool.py:114)
```

What this tells us:

- MainThread is in `IMapIterator.next()._cond.wait()` because
  `_index < _length` (360 < 366). It expects 6 more results.
- `_handle_results` is in `_recv()` waiting for bytes on the result pipe.
- `_handle_workers` (via `_wait_for_updates`) and `_handle_tasks` are both
  parked normally.
- Workers are at `worker()` line 114 in `inqueue.get()`. **They are idle and
  ready for the next task** — they have nothing in flight.

`_repopulate_pool_static` appears in several workers' fork-time stacks. That
shows that **some workers died and were replaced by `_handle_workers`'
`_maintain_pool`**. Standard library `Pool` does **not** re-dispatch a dead
worker's in-flight task. The lost task just disappears, `_length` is never
adjusted, and `imap_unordered` hangs forever.

## Why workers die — proximate trigger

The eqserver SIGALRM patch
(`eqserver_2_seiscomp:scan/phase3_driver.py` commit `db70dde`, 2026-06-03):

```python
DAY_TIMEOUT_S = 600  # 10 minutes

def _timeout_handler(signum, frame):
    raise TimeoutError(f"day-job exceeded {DAY_TIMEOUT_S}s wall-clock")

_signal.signal(_signal.SIGALRM, _timeout_handler)
_signal.alarm(DAY_TIMEOUT_S)
...
try:
    if recorder_eff == "echopro":
        r = convert_echopro_day(...)   # <— libmseed / obspy inside
    elif recorder_eff == "gecko":
        r = convert_gecko_day(...)
    ...
except TimeoutError as e:
    r = {"status": "timeout", ...}
...
finally:
    _signal.alarm(0)
```

The 10-minute alarm was added to break poison-day deadlocks (DDNE 2017,
DDSW 2019) where a single day-job would hang phase3 indefinitely. It works
for those, but the side effect is what we're now seeing:

1. SIGALRM fires while the worker is inside libmseed C code (STEIM2 pack,
   record-write, etc.). Python's signal-handling defers the signal until
   the C call returns, but the bytes-level damage to libmseed's internal
   state happens here — Python interrupts mid-write.
2. The Python signal handler fires next time the interpreter loop runs.
   TimeoutError raises out of the convert function.
3. `_worker_convert_day` catches TimeoutError, returns
   `{"status": "timeout", ...}` cleanly. Pool sends this result back to
   MainThread, which emits the `timeout` day-line. From phase3's
   perspective this looks fine.
4. The same worker picks up its NEXT day-job. libmseed's internal allocator
   detects the earlier corruption: **`free(): invalid pointer`** → SIGABRT
   → worker dies.
5. Pool's `_handle_workers` thread sees the worker exit, spawns a
   replacement. **The day-job the worker was processing when it died has
   its result lost.** That day's SDS write is also incomplete or missing.
6. With ~6 dead workers per unit (one per SIGALRM occurrence, sometimes
   none, sometimes a handful), we lose ~6 results. `_length` (366) is
   never reached by `_index` (360). Hang.

LRWS 2020 specifically: 359 ok + 1 timeout + 6 lost = 366 dispatched.
Matches.

## Confidence in this chain

- Strong: the py-spy stack proves Pool is waiting for missing results, not
  busy. Workers are idle.
- Strong: LRNW 2019's log shows the literal
  `TimeoutError ... free(): invalid pointer` sequence.
- Strong: the counts match (366 dispatched - 1 timeout - 6 lost = 359 ok).
- Medium: that the SIGALRM is the trigger of the heap corruption (vs an
  unrelated obspy/libmseed bug). The DDWB 2022 case self-resolved at 30 min,
  which we don't yet have py-spy evidence for — could be the same mechanism
  with one fewer dead worker.

It's possible there's a second, SIGALRM-independent failure mode (data
shape that just confuses libmseed). The SIGALRM path is at least one
contributor, and the easiest to remove first.

## Fix options — your call

We see three approaches, ranked by how invasive they are.

### Option A — replace SIGALRM with a process-level timeout

Drop the `signal.alarm` mechanism in `_worker_convert_day`. Instead, run
each day-job in a child process (`subprocess.run` with a `timeout=` argument,
or a `concurrent.futures.ProcessPoolExecutor` per-task call). Process death
is clean: parent reaps, no in-process heap corruption.

Pros:
- Kills the SIGALRM-in-C-code root cause entirely.
- Worker death isolated per day-job; no carry-over corruption.
- `concurrent.futures` re-raises `TimeoutError` cleanly without leaving
  zombie state.

Cons:
- Bigger refactor of `phase3_driver.py`'s main loop.
- ~50-100 ms process-spawn overhead per day-job. Negligible vs day-job
  duration (median ~25 s); ~30 s total per unit-year.
- Changes the parallelism model — needs careful merge with the existing
  `imap_unordered` flow.

### Option B — keep SIGALRM, harden Pool against worker death

Wrap the `for r in pool.imap_unordered(...)` loop with a heartbeat: if no
new result arrives within N minutes after the last one AND all workers are
idle (via Pool internals), break out, write a partial run_manifest, exit
phase3 with a "partial" status. Convert.py advances; promote picks up the
partial year.

Pros:
- Single-file change in `phase3_driver.py`.
- Eliminates the 3h watchdog cost per hang — drops to ~5 min after last
  activity.
- No engine-side changes needed.

Cons:
- Doesn't fix the underlying corruption — workers still die silently,
  results still lost, we just notice faster.
- Requires probing Pool internals (`_pool._cache`, `_inqueue.empty()`) or
  doing it from outside — fragile across Python versions.
- Days lost to died-mid-conversion workers still need a separate per-day
  retry pass.

### Option C — engine-side: make `suds_convert`/`write_sds` SIGALRM-safe

Audit the libmseed/obspy paths in `disk_to_sds/scripts/suds_convert.py`
(`convert_suds_files`, `write_sds`) for points where a Python signal during
a C-allocation operation can corrupt heap state. Either:

- Mask SIGALRM during critical allocations (`signal.pthread_sigmask` around
  the obspy `Stream.write()` and the libmseed boundary), or
- Switch to a libmseed-native record buffer with explicit close before
  letting Python re-enter signal-checking, or
- Bracket each obspy `Stream.write()` with a context manager that pauses
  the alarm.

Pros:
- Fixes the actual root cause. SIGALRM remains usable as a deadlock
  breaker without corrupting workers.
- Benefits any future caller of these functions, not just phase3.

Cons:
- Requires you to dig into where exactly the libmseed call frame is
  interrupt-unsafe — we haven't pinned that to a specific function yet.
- May not be solvable cleanly if obspy's `Stream.write()` is the
  interruption point (third-party code).

## Our recommendation

**Combine A and C.** A solves the structural problem (Pool can't recover
from dead workers) in eqserver-side code we own; C makes the engine
robust to signals for any future user. Either alone leaves a known fragile
seam. B is a band-aid we'd take if A is too slow to land, but we'd rather
not bake a "watch for stuck Pool" loop into the orchestrator if we can fix
the underlying mechanism.

If you'd prefer to scope this differently (e.g. you take C, we take A;
or you'd rather we drop SIGALRM entirely and accept hangs on poison-days
again), say so and we'll align.

## What's blocking us

We're mid-sweep (40 of 258 station-years to go for VW). Each hang costs
~3 h wall-clock + ~6 days of lost data. We can:

- **Keep going as-is**: ride out the rest of VW with the
  watchdog + manual recovery. Cost: 3 h × ~4 more hangs × ~6 lost days
  per hang ≈ 12 h wall-clock and ~24 days of LT gaps to fill later.
- **Land Option B now**, on this branch only. Eqserver side; doesn't
  touch your code. Brings hang cost from 3 h → 5 min.
- **Wait for your fix** (A and/or C). We can pause the sweep at the next
  natural boundary (currently MARD), let you ship, then resume.

Your call. Either way, the recovery tool
(`recover_manifest_from_log.py`) covers what we've already lost.

## Reproducer

To reproduce in your environment without the production NFS mount:

1. Build a synthetic day-job that calls into libmseed with a tight loop
   long enough to trigger SIGALRM (e.g. `Stream.write` with a 1 GB
   in-memory array).
2. Wrap it in the `_worker_convert_day` `signal.alarm(600)` pattern with
   a 5-second alarm instead of 600.
3. Run through `multiprocessing.Pool.imap_unordered` with 4 workers and
   100 tasks. Expect ~1-5 workers to die with `free(): invalid pointer`
   and the iterator to hang.

The clearest live evidence is `py-spy dump --pid <phase3-pid>` while a
unit is hanging — the parent thread set above is the diagnostic signature.

## Affected commits

- `eqserver_2_seiscomp` `db70dde` — added the SIGALRM-based 10-min
  per-day-job timeout in `_worker_convert_day`. This is the trigger.
- `eqserver_2_seiscomp` `2724c15` — phase3 midnight-boundary fix; not
  related to this issue but it lives in the same file so context-mixing
  is possible.
- `disk_to_sds` engine at `2ee96f3` — current production pin for
  `suds_convert.write_sds`. This is the engine version showing the
  symptom; not asserting it's the buggy version, just naming the
  reference point.

## Pointers to recovery infrastructure already in place

- `eqserver_2_seiscomp/scan/recover_manifest_from_log.py` — reconstructs
  a run_manifest from a phase3 log + staging tree walk after a watchdog
  kill. Verified end-to-end for LRNW 2019, LRNW 2020, LRWS 2020.
- The watchdog itself: `_start_phase3_watchdog(deadline_s=10800)` in
  `phase3_driver.py` (commit `6a6d7f3` — the version that kills only
  `os.kill(os.getpid())`, NOT the pgid). Catches the hang at 3 h max.
- Recovery register: `docs/scan1_recovery_register.md` will list the
  affected units and the gap-day counts to re-run after this is resolved.

Ready to discuss whatever shape of fix lands.
