# AI 额度服务接入契约

本文档定义 AIFlow `3.7` 与 m5stack AI 额度服务之间的运行时语义。HMAC 请求头、Canonical Path、nonce 和签名算法继续沿用 `/internal/v1/aiQuota` 协议；共享密钥只通过部署环境传递，不写入仓库、请求体或日志。

## 放行规则

AIFlow 在每个真实 `POST .../messages` 请求前调用一次 `POST /authorize`：

```json
{
  "requestId": "task_example:model:2",
  "mac": "aabbccddeeff",
  "model": "deepseek-pro"
}
```

AIFlow 不发送 `requestedTokens`，也不计算请求体、`max_tokens`、上下文或输出的预估用量。额度服务的 `allowed` 是唯一放行依据：

- 当前有效余额大于 `0` 时返回 `allowed=true`，AIFlow 立即转发该次模型请求。
- 当前有效余额小于等于 `0` 或设备被停用时返回 `allowed=false`，AIFlow 不发送模型请求。
- 额度服务不能要求当前余额覆盖本次未知用量，不能用默认申请量或预占量拒绝仍有正余额的设备。
- 已放行请求的实际结算可以把余额扣成负数；额度服务应在下一次 `/authorize` 时拒绝。

成功响应至少需要明确的布尔值 `allowed`。`requestId`、`authorizationId`、`grantedTokens`、`reservedTokens` 和 `expiresAt` 可为兼容旧服务保留，但 AIFlow 不使用批准量或过期时间做本地放行判断。若返回 `requestId`，必须与请求一致。额度服务若在实际结算后返回负的 `*AvailableTokens`，AIFlow 会原样保留并透传给下游；下一次放行仍只看新的 `allowed` 决定。

## 实际用量结算

模型响应包含可信 usage 后，AIFlow 立即调用 `POST /settle`：

```json
{
  "authorizationId": "qa_optional_compatibility_id",
  "requestId": "task_example:model:2",
  "model": "deepseek-pro",
  "inputTokens": 120000,
  "outputTokens": 80000,
  "cacheCreationInputTokens": 30000,
  "cacheReadInputTokens": 70000
}
```

`authorizationId` 只在 `/authorize` 返回时携带。`inputTokens` 已包含未缓存输入、缓存创建和缓存读取 Token；两个缓存字段是分类明细，不能再次加入 `actualTokens`。本次扣费固定为：

```text
actualTokens = inputTokens + outputTokens
```

额度服务不得再校验 `actualTokens <= grantedTokens/reservedTokens`。相同 `requestId` 和相同 usage 的重复结算必须幂等，并返回与提交值一致的 `inputTokens`、`outputTokens`、两项缓存明细及 `actualTokens`。

结算超时或结果未知时，AIFlow 会保存真实 usage 并使用相同业务请求重试；失败记录由后台每 30 秒扫描，并在重启后继续补偿，确保短暂的额度服务故障不会永久漏记。每次 HTTP 重试使用新的 HMAC nonce。结算未确认属于记账告警，不会撤销已经返回的模型结果，也不会阻止下一个模型请求重新执行 `/authorize`。

## 无用量与旧接口

模型上游在产生 usage 前明确拒绝请求时，AIFlow 只记录 `NO_USAGE`，不调用 `/release`。新流程没有预占，因此 `/release` 不参与正常运行；它只保留用于清理旧版本已经落库的历史预占记录。若服务在 `allowed=true` 后、转发模型前重启，恢复程序会查询服务端状态：仍为 `AUTHORIZED/ALLOWED` 的记录标记为 `NO_USAGE`，只有明确返回旧式 `RESERVED` 才执行兼容释放。

任务末尾 Claude SDK 的汇总 usage 仅用于任务结果与可观测性，不能再次提交额度服务，否则会重复扣费。
