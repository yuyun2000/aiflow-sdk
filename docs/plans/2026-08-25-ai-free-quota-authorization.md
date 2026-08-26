# AI Free Quota Authorization

## Goal

Require the m5stack free-token quota service to authorize and settle every individual Claude/DeepSeek-compatible model HTTP request. Keep the final Coding-task usage as observability data only so it cannot be charged twice.

## Constraints

- Keep HMAC credentials server-side and environment-only.
- Authorize only after a task receives an execution slot and immediately before each model HTTP request.
- Keep `direct-run` outside the quota flow because it does not call the model.
- Fail closed when quota protection is enabled but authorization cannot be confirmed.
- Use deterministic task-plus-model-request IDs for quota idempotency and persist every actual usage report for restart reconciliation.
- Send no local usage estimate or reservation amount. Treat the quota service's `allowed` as the only forwarding decision.
- Never compare actual usage with a local balance, granted amount, reservation, or authorization expiry.
- Keep settlement failures as accounting warnings; they must not invalidate model output or block the next request's authorization.
- Expose only non-sensitive quota state to clients through capabilities, task errors, and task events.
- Verify with fake HTTP transports and fake Agent runners; do not call the production quota service or a real model.

## Work

- [x] Add validated quota settings and an HMAC-SHA256 client implementing authorize, settle, status, and legacy release cleanup.
- [x] Persist request-level authorization/accounting state and continuously reconcile unfinished settlements (including after restart).
- [x] Add a loopback-only model egress proxy so every `/messages` request is authorized before forwarding and settled from that response's trusted usage.
- [x] Migrate quota persistence from one row per task to ordered request-level rows without losing existing reservations.
- [x] Keep tool execution outside charging while naturally reauthorizing the model request triggered by each tool result.
- [x] Stop submitting the final SDK task usage to the quota service; retain it only in task results and telemetry.
- [x] Include `cacheCreationInputTokens` and `cacheReadInputTokens` in idempotent settlement, SQLite recovery, and downstream settlement events without double-counting them.
- [x] Preserve the quota service's daily and lifetime limit fields in downstream authorization events and denial errors.
- [x] Add downstream capability/event/error documentation and web-client event rendering.
- [x] Add focused protocol, configuration, storage migration, and task lifecycle tests.
- [x] Remove `requestedTokens`, local granted-token comparisons, authorization-expiry cutoffs, and quota-based post-response failures.
- [x] Run scoped and full offline regression checks plus secret/diff review.
