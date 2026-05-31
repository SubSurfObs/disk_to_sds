# Cross-project handoffs

Asynchronous design conversations between this project and its peer projects,
conducted via committed markdown files (one Claude session writes, the peer's
Claude session reads, commits a reply, the cycle continues until convergence).

This is a **local convention** for the project group listed in
`../RELATED_PROJECTS.md`. It is NOT a Claude Code default — as of this writing
there is no established cross-session handoff pattern in the Claude Code or
MCP ecosystems. The convention here is bespoke and lightweight.

## Layout

```
handoffs/
└── <peer-project>/
    └── <YYYY-MM-DD>_<thread-slug>/
        ├── 01_<message-type>_from_<sender>.md
        ├── 02_<message-type>_from_<sender>.md
        └── ...
```

- **One subdirectory per peer project**.
- **One subdirectory per thread** (conversation), keyed by start date and a
  short topic slug.
- **Messages numbered sequentially** with the sender on the right of the
  filename so the conversation reads top-to-bottom in `ls` output.
- **The full thread is mirrored in each participating repo's `handoffs/`
  directory** (sender's outgoing AND incoming messages, both). A few KB of
  markdown duplication per repo; buys discoverability — any session reading
  any repo sees the complete exchange without cross-repo grep.

## Naming

- Thread dir: `<YYYY-MM-DD>_<topic-slug>` (date is the thread's start; slug
  is kebab-case, short, descriptive).
- Message file: `<NN>_<message-type>_from_<sender>.md` where:
  - `<NN>` is the sequential index (`01`, `02`, ...) for chronological order.
  - `<message-type>` is a short tag: `proposal`, `reply`, `ready_for_review`,
    `decision`, `closing`.
  - `<sender>` is the project name.

## When to start a new thread vs. extend one

- **New thread**: a different design topic, an earlier thread reached terminal
  state, or an existing thread is getting unwieldy (>10 messages without
  convergence — split it).
- **Extend**: iterating on the same proposal / schema / API / design decision.

## Closing a thread

When a thread reaches a terminal state (decision made, PR merged, schema
locked and implemented), add a final `NN_closing_from_<sender>.md` message
summarising the outcome and linking to the relevant commits / PRs. Then leave
the directory in place — future sessions may want the historical record.

## Active threads (this repo: `disk_to_sds`)

| Peer | Thread | Status |
|---|---|---|
| `eqserver_2_seiscomp` | `2026-05-30_ledger-integration` | open — eqserver shipped commits `cdb6439` and `b7bbe56` on branch `rewrite-suds2sds`; ledger branch `eqserver-integration` (commits `82d3d6f` / `af728fc`) awaiting review here |
| `sds_staging_ledger` | none | direct ledger work landed via `ledger_git` autocommit (no design discussion needed) |

## Future agents reading this

1. Read `RELATED_PROJECTS.md` to know which peers exist.
2. Browse the thread directory most relevant to your task — files are numbered
   chronologically so you can read top-to-bottom.
3. If you need to send a new handoff message, follow the naming convention
   above and write to **both** your repo's `handoffs/<peer>/<thread>/` AND
   the peer repo's matching directory (full-thread-in-both is the contract).
