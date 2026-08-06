# Client authentication and Agent event plan

## Goal

Keep model/core credentials out of anonymous browsers, make the public web entrypoint a server-side signed BFF, cap anonymous mechanical use by session/IP/global quotas, and expose meaningful sanitized Agent/SDK lifecycle, output, tool, and provider-supplied thinking events without disclosing secrets.

## Completion checklist

- [x] Add versioned HMAC request authentication with per-client keys, timestamp, nonce, body hash, and response acknowledgement signatures
- [x] Persist nonce claims and request/AI-task rate counters in SQLite
- [x] Protect context creation and all private V3 routes while retaining task-scoped SSE access
- [x] Add configurable per-client and global AI-task limits
- [x] Keep signed direct-core support in the reference client while the default browser uses server-side BFF signing
- [x] Stream and retain safe SDK system, text, tool, rate-limit, usage, result, and error events
- [x] Publish provider-supplied thinking while redacting credentials, paired identifiers, signatures, and absolute workspace paths
- [x] Add an anonymous same-origin BFF with HttpOnly session and session/IP rate limits
- [x] Keep the core API in-process only and strip all browser-supplied internal signature headers
- [x] Proxy task SSE directly so partial Agent events remain real-time
- [x] Correlate partial and final text by provider response/block identity, prevent stale client assets, and verify one-row rendering against the configured third-party model
- [x] Make uiflow2-coder-first advisory instead of a PreToolUse gate so hardware facts can be queried through m5stack-assistant without an artificial denial
- [x] Filter redundant input/signature fragments, retain every public text/thinking delta, and cap the raw-event DOM without dropping complete tool calls/results
- [x] Make `manage.sh` and the compatibility entrypoint start only the public gateway
- [x] Update API, client integration, deployment, and security documentation
- [x] Run unit, integration, syntax, and live HTTP verification
