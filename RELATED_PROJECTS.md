# Associated projects

Projects that `disk_to_sds` collaborates with, directly or via shared
infrastructure. Each peer with a documented cross-project relationship has
a corresponding `handoffs/<peer>/` directory for design conversations — see
`handoffs/README.md` for the convention.

| Project | Role | Repo | Local path (Mac) | Handoff history |
|---|---|---|---|---|
| `eqserver_2_seiscomp` | Sibling ingest project — converts legacy EqServer archive into the same shared staging SDS skeleton. Writes to the same `events.jsonl` in the ledger as this project. | `github.com/SubSurfObs/eqserver_2_seiscomp` | `~/projects/SubSurfObs/eqserver_2_seiscomp` | `handoffs/eqserver_2_seiscomp/` |
| `sds_staging_ledger` | The system of record for long-term archive promotions. Owns `apply.py` (the single LT writer), `cleanup.py`, and the events/cleanups/cards/policies/runs manifest tree. This repo's `apply.py` invocations + autocommits via `ledger_git.commit_and_push` flow through it. | `github.com/SubSurfObs/sds_staging_ledger` | `~/projects/SubSurfObs/sds_staging_ledger` | `handoffs/sds_staging_ledger/` |

## How a new peer joins the group

1. **Add a row** to the table above describing the relationship — what role
   the new project plays, what data/state it shares with this one, where to
   find its repo and local checkout.
2. **Mirror the addition in the new peer's `RELATED_PROJECTS.md`** so the
   relationship is documented bidirectionally.
3. **Create `handoffs/<new-peer>/` directory** here (just `.gitkeep` if no
   thread is active yet; first message starts a thread).
4. **If the new peer affects this project's architecture** (storage paths,
   shared mounts, contracts, schemas), document those touchpoints in
   `CLAUDE.md`. Don't leave the relationship buried in this file alone.
5. **First handoff thread**: when starting the first conversation with a new
   peer, follow `handoffs/README.md` — create
   `handoffs/<new-peer>/<YYYY-MM-DD>_<topic-slug>/` and write
   `01_<message-type>_from_<sender>.md`. Mirror the file in the peer's repo.

## Why this isn't in CLAUDE.md

`CLAUDE.md` captures stable design intent for this project. Associated-project
relationships and handoff conventions are at a different abstraction layer
(project group rather than project internals) and change more frequently than
CLAUDE.md should. Splitting them keeps both files focused.
