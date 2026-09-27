---
name: costbook-initialize
description: Initialize or update a local agent-costbook price and capability catalog from a reviewed batch. Use when an agent has gathered public sources or manual candidates and needs the same plan, diff, approval, backup, apply, and verify steps. Field shapes for a contribution or capability row stay in costbook-contribute.
---

# Initialize or update agent-costbook

Use this with [costbook-contribute](../costbook-contribute/SKILL.md). That skill owns the contribution and capability fields. This skill owns the batch workflow. It does not depend on a coordinator product, a hosted agent brand, or a scheduler.

Initialization means the first reviewed coverage of a scope. It does not create the database, start the server, or delete rows. An update uses the same commands and compares the server's current rows with the batch. An agent may run the commands when that server, scope, and those categories were already authorized. A wider server, a new category, or a private-data read needs a new authorization. The same authorized scope may be repeated without asking again.

The server must be the loopback process in the scope file. `ac data` refuses any other host, a URL that contains a user or password, and a redirect. Read `ACB_ADMIN_TOKEN` from the environment. Do not put the token in the scope, the batch, a command argument, a log, or a report.

## Commands

```sh
ac data plan --mode init --scope scope.json --out batch-dir
ac data plan --mode update --scope scope.json --out batch-dir
ac data validate --batch batch-dir
ac data diff --batch batch-dir --server http://127.0.0.1:8080
ac data apply --batch batch-dir --approved-diff-sha256 PLAN_SHA256 --server http://127.0.0.1:8080 \
  --backup-source "$ACB_DB" --backup-destination batch-dir/backup.sqlite3
ac data verify --receipt batch-dir/receipt.json --server http://127.0.0.1:8080
```

`examples/data-scope.json` is a scope. `examples/data-batch/` is the batch `plan` writes from it. Money and scores are decimal strings.

## What you write, and what the command writes

A scope item is `manual`, `agent`, or `openrouter-json`.

- `manual` and `agent` items are candidates only after you supply the value. Until then `plan` marks `needs_research`. Do not report those rows as collected.
- `openrouter-json` accepts a local fixture, `parser` of `catalog` or `endpoints`, and `retrieved_at`. The parser reads only the pinned `openai/gpt-4o-mini` record. `plan` does not fetch the network. A missing fixture, a layout change, or disagreeing endpoint prices stays `needs_research`.
- `coding_agent`, benchmark combinations, private quota, and subscription ledgers are `unsupported`. Omit `candidate` on a gap. A candidate for an unsupported category fails validation and is not written.
- Leave unknown prices and scores out. Do not fill them with `0` or `1`.

`diff` writes `plan.json` and a reading copy `plan.md`. The approval hash is the sha256 of the canonical `plan.json`. It binds the server URL, the publisher id, the batch bytes, the record identity, the baseline version, and the full target value. `plan.md` is not part of the hash. Pass that hash to `apply`. An authorized caller may apply the hash for the scope it was given. A new hash is a different plan and needs its own authorization.

`apply` sends a price row through `POST /v1/contributions` and publish, using `base_snapshot_id` from `record_snapshot_id` on `POST /v1/estimates`. Capability rows use the current GET body plus `expected_version`. Fields omitted from the candidate keep the stored value. A missing source does not delete a row. The same business content is not written again.

## Backup, failure, and a second run

A plan that would write requires either:

- `--backup-source` and `--backup-destination`. The copy uses the local backup helper, then the new file is mode `0600`. The destination directory must already be private (`0700`) or is created that way. This does not change the mode used by `agent-costbook-backup`. The copied database must show this publisher, the current price snapshot, and the current capability rows.
- `--backup-receipt` for a backup the operator already made. The receipt must name this server and publisher, match the current catalog version and content hash, and point at a private file whose bytes and database contents match. A world-readable file or a copy of a different database does not authorize a write.

If the approved hash, server, or publisher does not match, `apply` writes nothing. If a row's version changed and its content differs, that row is `conflict`; re-run `diff` instead of editing the approved plan. Re-running `apply` with the same hash does not create another price snapshot or capability version when the content is unchanged.

There is no contribution-status request. After a lost price response, run `apply` again with the same hash. The journal's saved contribution id is checked with `GET /v1/evidence`. When that evidence is already public, the command records success and does not publish again. When it is not public, publish is repeated for that same contribution id; a contribution that is already published returns its snapshot and does not create a second one. A contribution id that is not in the journal is not invented or replayed. A lost capability response is success only when the stored row matches the approved body and its version is the expected next version. Any other version or body is `conflict`.

`verify` reads the server again. Command success does not mean the scope is complete: deferred and refused rows remain visible in the receipt coverage.
