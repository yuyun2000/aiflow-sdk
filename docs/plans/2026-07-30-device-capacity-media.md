# Device-keyed capacity and media plan

## Goal

Require the paired client to provide `deviceId + clientId`, use `deviceId` as the stable frontend/backend project key and `clientId` as the resource-upload identifier, remove the unnecessary MAC layer, bound session/task capacity, support Base64 image/audio messages, and keep the repository structure understandable.

## Completion checklist

- [x] Move current docs, examples, and V2 artifacts out of the repository root
- [x] Make deviceId and clientId required at initialization and remove MAC mapping from service and push Skill
- [x] Pass deviceId to code pushes and deviceId + clientId to resource uploads without exposing either value in Agent prompts
- [x] Reconnect the same deviceId to the same workspace/history while rotating its token
- [x] Enforce configurable total session capacity
- [x] Enforce configurable global task concurrency and waiting queue capacity
- [x] Return live aggregate capacity at connect time and through a status endpoint
- [x] Decode image/audio Base64 into the device project without persisting payload text
- [x] Return device_id on project, task, and conversation management responses
- [x] Add focused capacity, concurrency, media, and device push tests
- [x] Run lifecycle HTTP smoke and final requirement audit
