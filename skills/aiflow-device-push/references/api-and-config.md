# AIFlow Push Reference

Read this reference only for client integration, configuration, limits, or failure diagnosis.

## Client injection

The cheapest per-call integration is an environment variable supplied by the client process:

```bash
AIFLOW_DEVICE_ID='device-123' AIFLOW_CLIENT_ID='client-123' \
  python3 <skill-dir>/scripts/aiflow_push.py push-code --code main.py --execute
```

Do not place per-device identifiers in prompts when the client can set environment variables directly.

## Config schema

If `AIFLOW_CONFIG` and `--config` are absent, the CLI reads `.aiflow/config.json` from the project working directory when it exists.

```json
{
  "baseUrl": "https://ai-flow.m5stack.com/",
  "clientId": "client-123",
  "timeout": 120,
  "defaultDeviceId": "device-123"
}
```

`baseUrl` and `timeout` are optional. Every plan or push requires both a device ID and a client ID, supplied through CLI options, environment variables, or `defaultDeviceId` plus `clientId` in config.

Recommended client behavior:

1. Pass `AIFLOW_DEVICE_ID` and `AIFLOW_CLIENT_ID` from the paired client process.
2. Write config updates atomically when using `defaultDeviceId`.
3. Use a separate config file per environment if base URLs differ.
4. Never store credentials in this file. The documented Local API has no authentication field.

## Commands

```text
plan             Validate target and optional files; never sends HTTP.
push-code        POST UTF-8 source as text/plain.
push-resources   POST one or more files as multipart/form-data.
deploy           Push resources first and code second.
```

Push commands without `--execute` return a validated, non-network plan. This makes an accidental command safe while keeping the real operation a one-flag change.

## HTTP contract

Code:

```http
POST /api/v1/device/push-code/{deviceId}
Content-Type: text/plain; charset=UTF-8
```

Resources:

```http
POST /api/v1/localFiles/upload-resource-batch-and-push?deviceId=...&clientId=...
Content-Type: multipart/form-data
```

Repeat multipart field `files`. If any resource has an explicit device directory, send one `filePaths` field per file, using an empty value for automatic placement.

The CLI quotes the local path and explicitly sends the original basename so commas, semicolons, spaces, and UTF-8 client filenames are not reinterpreted by cURL. A local path or filename containing a double quote requires cURL 7.81 or newer because the CLI enables `--form-escape` for that case.

Normal UIFlow project resources use directories relative to the device Flash root. UIFlow code and the upload API express the same location differently:

| UIFlow runtime path | Upload `filePaths` value |
| --- | --- |
| `res/img/logo.png` or `/flash/res/img/logo.png` | `res/img/` |
| `file://flash/res/audio/startup.wav` | `res/audio/` |

Pass only the directory to `filePaths`, never the resource filename. The CLI accepts `/flash/...`, `flash/...`, `file://flash/...`, and `file:///flash/...` directory forms and converts them to the Flash-relative API value. It converts `file://sd/...` to `/sd/...` without redirecting it to Flash, but the documented third-party upload contract does not confirm SD delivery; only report SD success after downstream/device verification.

## Local validation limits

- Code must be non-empty UTF-8.
- Resource files must be non-empty and have unique basenames after Unicode/case normalization.
- Resource `main.py` and `main_ota_temp.py` are forbidden.
- Images (`jpg`, `jpeg`, `png`, `bmp`) must not exceed 2 MiB each.
- Any single resource must not exceed 100 MiB.
- One resource request must not exceed 500 MiB total.
- Device directories must not contain unsupported URI schemes or standalone `.` or `..` segments.

Automatic server directories are `res/img/` for images, `res/audio/` for `mp3`, `amr`, `wamr`, and `wav`, and `res/` for other extensions.

## Success and failure semantics

- Code `200`: chunks were published; ACK waits can time out without failing the HTTP response. `chunkCount` is not proof of execution.
- Resource `200`: files were stored, metadata persisted, and an MQTT file-list message published. The API does not wait for device ACK.
- Any non-2xx is failure. Do not depend on a stable error JSON schema.
- An HTTP timeout is ambiguous. Do not retry automatically because the server may already have submitted some or all work.
- If a device must access returned file URLs across a network, the public file address must not resolve to `localhost` from the device.
