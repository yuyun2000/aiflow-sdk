---
name: aiflow-device-push
description: Push or deploy MicroPython/UIFlow2 source code and resource files to bound M5Stack devices through the AIFlow/VibeM5Stack Local third-party device API. Use when an agent needs to validate client-provided deviceId and clientId targets, upload images/audio/other resources, push finished code, run code on a device, or troubleshoot AIFlow device push failures.
---

# AIFlow Device Push

Use the bundled CLI so the agent does not rebuild HTTP requests or reload the full API contract for routine deployments.

## Workflow

1. Finish and locally validate the code. For UIFlow2 APIs, use `uiflow2-coder` first.
2. Reuse the client-provided `deviceId` and `clientId` from CLI arguments/environment. Do not ask again when `AIFLOW_DEVICE_ID` and `AIFLOW_CLIENT_ID` are already set.
3. Run `plan` once. It validates target resolution, UTF-8 code, resources, sizes, and device paths without network access.
4. Run a push command with `--execute` only when the current user request explicitly authorizes upload, deploy, push, burn, or another device-changing action. Finishing code alone is not authorization.
5. Report server submission separately from device execution. HTTP success does not prove that the device downloaded, saved, or ran anything.

Run the script from the project working directory so relative file paths resolve against the project:

```bash
python3 <skill-dir>/scripts/aiflow_push.py plan --code main.py
python3 <skill-dir>/scripts/aiflow_push.py push-code --code main.py --execute
python3 <skill-dir>/scripts/aiflow_push.py push-resources --resource logo.png --execute
python3 <skill-dir>/scripts/aiflow_push.py deploy --code main.py --resource 'logo.png::res/img/' --execute
```

Use `LOCAL_FILE::DEVICE_DIRECTORY` for an explicit device directory. Omit `::DEVICE_DIRECTORY` for server-side automatic placement. Repeat `--resource` for multiple files.

For normal project resources, use a directory relative to the device Flash root, such as `res/img/` or `res/audio/`; never append the resource filename. The CLI also accepts UIFlow runtime forms such as `/flash/res/img/` and `file://flash/res/audio/` and normalizes them to the upload API form. Automatic placement targets `res/...`; an explicit `/sd/...` directory is preserved, but do not claim SD delivery unless the downstream service and target device confirm it.

## Target Configuration

Resolve values in this order:

- Device ID: `--device-id`, `AIFLOW_DEVICE_ID`, config default.
- Client ID: `--client-id`, `AIFLOW_CLIENT_ID`, config `clientId`.
- Other settings: CLI, environment, config, built-in default.

Every plan and push requires both identifiers. The code endpoint uses `deviceId` in its path; the resource endpoint sends both `deviceId` and `clientId`. Inject them mechanically through environment variables; do not put them in the model prompt or build a MAC mapping layer.

Supported environment variables:

```text
AIFLOW_CONFIG
AIFLOW_BASE_URL
AIFLOW_DEVICE_ID
AIFLOW_CLIENT_ID
AIFLOW_TIMEOUT
```

Do not echo full device identifiers in the answer. The CLI masks them in plans and summaries.

## Operational Rules

- `deploy` pushes resources first, then code. It stops on the first failure.
- Do not upload `main.py` or `main_ota_temp.py` as resources; send code through `push-code` or `deploy --code`.
- Do not automatically retry a timeout or ambiguous HTTP failure. Code chunks or resources may already have been submitted.
- Do not claim firmware flashing. This API pushes MicroPython source and resource files; it does not install device firmware.
- Treat a successful code response as published chunks, not confirmed execution.
- Treat a successful resource response as server storage plus MQTT publication, not confirmed download.

Read [references/api-and-config.md](references/api-and-config.md) only when integrating a client, interpreting an error, or checking API limits. Use `python3 <skill-dir>/scripts/aiflow_push.py --help` for command details.
