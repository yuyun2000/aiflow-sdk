# AI Free Quota Authorization

## Goal

Require the m5stack free-token quota service to authorize every Coding task before the Claude SDK can call the configured DeepSeek-compatible model. Settle complete trusted SDK usage, including cache categories, and release only when zero model consumption is known.

## Constraints

- Keep HMAC credentials server-side and environment-only.
- Authorize only after a task receives an execution slot so queued work does not consume the 10-minute reservation lifetime.
- Keep `direct-run` outside the quota flow because it does not call the model.
- Fail closed when quota protection is enabled but authorization cannot be confirmed.
- Use task IDs as idempotent quota request IDs and persist reservation state for restart reconciliation.
- Never release after a model request may have started unless zero consumption is confirmed; persist known partial usage and retain unknown usage for reconciliation.
- Expose only non-sensitive quota state to clients through capabilities, task errors, and task events.
- Verify with fake HTTP transports and fake Agent runners; do not call the production quota service or a real model.

## Work

- [x] Add validated quota settings and an HMAC-SHA256 client implementing authorize, settle, release, and status.
- [x] Persist quota requests/reservations and reconcile unfinished reservations after restart.
- [x] Integrate authorization before `ClaudeRunner.run`, mark the exact `client.query()` boundary, settle success or known partial usage, and release only before the model request begins.
- [x] Include `cacheCreationInputTokens` and `cacheReadInputTokens` in idempotent settlement, SQLite recovery, and downstream settlement events without double-counting them.
- [x] Add downstream capability/event/error documentation and web-client event rendering.
- [x] Add focused protocol, configuration, storage migration, and task lifecycle tests.
- [x] Run scoped and full offline regression checks plus secret/diff review.
