# 第三方设备推送接口协议

## 1. 概述

本文档描述 VibeM5Stack Local 版本中用于向已绑定设备推送源代码和资源文件的 HTTP 接口。

- 默认服务地址：`https://uiflow2.m5stack.com/m5stack/`
- 鉴权：Local 版本默认不需要登录鉴权
- 前置条件：`deviceId` 必须已绑定到设备 MAC 地址
- 调用方初始化时必须同时保存客户端传入的 `deviceId` 和 `clientId`；禁止用内部项目 ID 替代 `clientId`

## 2. 推送代码

### 2.1 请求

```http
POST /api/v1/device/push-code/{deviceId}
Content-Type: text/plain; charset=UTF-8
```

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `deviceId` | Path | string | 是 | 已绑定设备的平台设备 ID。 |
| `codeBlock` | Body | string | 是 | MicroPython 源代码，不能为空或仅包含空白字符。 |

请求体直接传递源代码，不是 JSON：

代码接口只在路径中使用 `deviceId`；初始化时保存的 `clientId` 留给资源上传接口使用，不应擅自附加到代码接口。

```python
from m5stack import *
from m5ui import *

print("Hello VibeM5Stack")
```

### 2.2 cURL 示例

上传本地源代码文件：

```bash
curl -X POST \
  "https://uiflow2.m5stack.com/m5stack/api/v1/device/push-code/device-123" \
  -H "Content-Type: text/plain; charset=UTF-8" \
  --data-binary "@main.py"
```

直接发送源代码：

```bash
curl -X POST \
  "https://uiflow2.m5stack.com/m5stack/api/v1/device/push-code/device-123" \
  -H "Content-Type: text/plain; charset=UTF-8" \
  --data-binary 'print("hello")'
```

### 2.3 成功响应

HTTP 状态码：`200 OK`

```json
{
  "deviceId": "device-123",
  "chunkCount": 1
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `deviceId` | string | 目标设备 ID。 |
| `chunkCount` | integer | 代码拆分后的 MQTT 数据包数量。 |

### 2.4 处理规则

- 服务端根据 `deviceId` 查询绑定设备的 MAC 地址。
- 设备必须在线，数据库中的在线状态值为 `0`。
- 代码按 Unicode 码点边界拆包，不会截断多字节字符。
- 每个数据包解码后的代码最多为 2600 个 UTF-8 字节。
- 服务端逐包发布，每包最多等待设备 ACK 5 秒。
- 单包 ACK 超时只记录日志，不会使 HTTP 请求失败，也不会阻止后续数据包继续发送。
- `chunkCount` 仅代表已发布的数据包数量，不代表设备已成功执行代码。

### 2.5 常见失败

| 场景 | 服务端消息或状态 |
| --- | --- |
| `deviceId` 为空 | `deviceId must not be blank` |
| 请求体为空 | `codeBlock must not be blank` |
| 设备未绑定 | `deviceId is not bound to any device` |
| 绑定记录没有 MAC | `mac address for the given deviceId is blank` |
| 设备离线 | `Device is offline. Please connect the device and try again.` |
| Content-Type 不正确 | 通常返回 `415 Unsupported Media Type` |

## 3. 批量上传资源并推送

该接口将一个或多个资源文件上传到 Local 服务端，并向设备发布文件下载信息。接口只在 Spring `local` profile 下提供。

### 3.1 请求

```http
POST /api/v1/localFiles/upload-resource-batch-and-push
Content-Type: multipart/form-data
```

完整地址示例：

```text
https://uiflow2.m5stack.com/m5stack/api/v1/localFiles/upload-resource-batch-and-push?deviceId=device-123&clientId=client-123
```

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `deviceId` | Query 或 multipart 字段 | string | 是 | 已绑定设备的平台设备 ID。 |
| `clientId` | Query 或 multipart 字段 | string | 是 | 客户端初始化时传入的上传标识。 |
| `files` | multipart 字段 | file array | 是 | 一个或多个资源文件，重复使用字段名 `files`。 |
| `filePaths` | multipart 字段 | string array | 否 | 设备目标目录，按索引与 `files` 对应。 |

### 3.2 自动分配设备目录

未传递 `filePaths` 或某个路径值为空时，服务端按照扩展名自动分配目录：

| 文件类型 | 自动分配的 `devicePath` |
| --- | --- |
| `jpg`、`jpeg`、`png`、`bmp` | `res/img/` |
| `mp3`、`amr`、`wamr`、`wav` | `res/audio/` |
| 其他资源文件 | `res/` |

```bash
curl -X POST \
  "https://uiflow2.m5stack.com/m5stack/api/v1/localFiles/upload-resource-batch-and-push?deviceId=device-123&clientId=client-123" \
  -F "files=@logo.png" \
  -F "files=@startup.wav"
```

### 3.3 指定设备目录

传递 `filePaths` 时，其元素按照数组索引与 `files` 对应：

```text
files[0] -> filePaths[0]
files[1] -> filePaths[1]
```

```bash
curl -X POST \
  "https://uiflow2.m5stack.com/m5stack/api/v1/localFiles/upload-resource-batch-and-push?deviceId=device-123&clientId=client-123" \
  -F "files=@logo.png" \
  -F "filePaths=custom/images/" \
  -F "files=@startup.wav" \
  -F "filePaths=custom/audio/"
```

路径规则：

- 完全不传 `filePaths` 时，所有文件使用自动目录。
- 传递 `filePaths` 时，其数量必须与 `files` 数量完全相同。
- 某个路径值为空时，仅对应索引的文件使用自动目录。
- 反斜杠 `\` 会转换为正斜杠 `/`。
- 路径不能包含独立的 `.` 或 `..` 相对路径段。
- 资源文件路径表示目录，服务端最终会将其规范为以 `/` 结尾的目录。
- 对 UIFlow 项目内的常规 Flash 资源，接入方应传 Flash 根目录下的相对目录，例如运行时 `/flash/res/img/logo.png` 对应 `filePaths=res/img/`，`file://flash/res/audio/startup.wav` 对应 `filePaths=res/audio/`；不要把运行时 URI 或资源文件名直接作为目录发送。当前接口资料未确认 SD 写入语义，不能仅凭 HTTP `200` 声称文件已写入 SD。

### 3.4 文件限制

- 至少上传一个非空文件。
- 禁止上传 `main.py` 和 `main_ota_temp.py`。
- 图片扩展名支持 `jpg`、`jpeg`、`png`、`bmp`，每个图片文件不能超过 2 MB。
- 音频扩展名支持 `mp3`、`amr`、`wamr`、`wav`。
- 其他扩展名允许上传，并使用默认目录 `res/`。
- 默认 HTTP 单文件上传上限为 100 MB。
- 默认 HTTP 单次请求总大小上限为 500 MB。
- 建议同一批次使用不同文件名，避免设备端覆盖或结果歧义。

### 3.5 成功响应

HTTP 状态码：`200 OK`

```json
{
  "batchId": "a5703ac785f441f3a897d48d2dce9617",
  "urls": [
    "https://uiflow2.m5stack.com/m5stack/files/uploads/20260730/abc123-logo.png",
    "https://uiflow2.m5stack.com/m5stack/files/uploads/20260730/def456-startup.wav"
  ],
  "pushResult": {
    "batchId": "a5703ac785f441f3a897d48d2dce9617",
    "deviceId": "device-123",
    "mac": "dc5475c786cc",
    "topic": "$m5/uiflow/v1/down/dc5475c786cc/file",
    "fileCount": 2,
    "totalSize": 97824
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `batchId` | string | 32 位、不含连字符的上传批次 ID。 |
| `urls` | string array | 按上传顺序返回的文件访问地址。 |
| `pushResult.deviceId` | string | 目标设备 ID。 |
| `pushResult.mac` | string | 目标设备 MAC 地址。 |
| `pushResult.topic` | string | MQTT 文件下行主题。 |
| `pushResult.fileCount` | integer | 本批次文件数量。 |
| `pushResult.totalSize` | integer | 本批次所有文件的总字节数。 |

### 3.6 成功语义

HTTP 请求成功表示：

1. 文件已经保存到服务端。
2. 文件元数据已经持久化。
3. 文件列表消息已经发布到 MQTT。

该接口不会等待设备 ACK。因此，返回 `200 OK` 不代表设备已经下载或保存文件。

接口返回和推送的每个文件 URL 都必须能被设备直接访问。除非设备与服务端处于相同的本机网络环境，否则不要把公开文件地址配置为 `localhost`。

### 3.7 常见失败

| 场景 | 服务端消息或状态 |
| --- | --- |
| 缺少 `files` 字段 | 通常返回 `400 Bad Request` |
| 文件列表为空 | `The upload file list cannot be empty` |
| 文件内容为空 | `file content must not be empty` |
| 缺少 `deviceId` | 通常返回 `400 Bad Request` |
| 缺少 `clientId` | 通常返回 `400 Bad Request`，客户端不得用内部 `contextId` 代替 |
| 设备未绑定 | `device ID not bound` |
| 绑定记录没有 MAC | `device MAC for this device ID is blank` |
| `filePaths` 数量不匹配 | `filePaths count must match files count` |
| 上传 `main.py` 或 `main_ota_temp.py` | `The resource file endpoint does not allow uploading main.py or main_ota_temp.py` |
| 图片超过 2 MB | `image file must not exceed 2 MB` |
| 路径包含 `.` 或 `..` | `device file path must not contain relative path segments` |
| 超过 HTTP 上传大小限制 | 通常返回 `413 Payload Too Large` |

## 4. 错误响应兼容性

当前实现没有为所有业务异常定义稳定的第三方错误响应 DTO。第三方客户端必须将所有非 `2xx` HTTP 状态视为失败，不应依赖 Spring Boot 默认错误 JSON 的具体字段结构。

## 5. 接入注意事项

- 推送源代码时必须使用 `text/plain`，不能使用 JSON 或表单格式。
- 上传资源文件时必须使用 `multipart/form-data`，并重复使用准确的字段名 `files`。
- 使用 HTTP 客户端库发送 multipart 请求时，不要手动设置 boundary，应由客户端库自动生成。
- 客户端可控文件名必须作为 multipart `filename` 原样发送。使用 cURL 时应给本地路径和 `filename` 加双引号；名称包含双引号时需使用 cURL 7.81+ 的 `--form-escape`，避免文件名被改写。
- 使用 cURL 上传源代码文件时应使用 `--data-binary`，避免换行或内容被表单编码修改。
- HTTP 成功仅确认服务端已提交推送，设备执行和文件处理结果需要通过设备状态或 ACK 另行确认。

## 6. 只读网络诊断

推送出现 `curl: (28)` 或长时间无响应时，应在实际运行 `aiflow_push.py` 的服务器上执行诊断，
这样测到的是同一台机器的 DNS、出口网络和 TLS 路径：

```bash
python3 scripts/diagnose_uiflow_push.py \
  --role client \
  --repeat 5 \
  --interval 2 \
  --timeout 10
```

服务器没有 `python` 命令时使用 `python3`；本脚本只依赖 Python 3 标准库。

客户端和 UIFlow 运维应在各自的机器上针对同一个服务地址各运行一次。UIFlow 运维在网关主机上运行时，
`--base-url` 应改成该主机实际监听的地址，例如 `http://127.0.0.1:8080/m5stack/`：

```bash
# 客户端/20.38：从实际发起推送的机器运行
python3 scripts/diagnose_uiflow_push.py --role client --repeat 5 --interval 2 --timeout 10

# UIFlow 网关主机：使用网关本机实际地址，端口按运维环境替换
python3 scripts/diagnose_uiflow_push.py \
  --role uiflow \
  --base-url http://127.0.0.1:8080/m5stack/ \
  --repeat 5 \
  --interval 2 \
  --timeout 10
```

脚本默认直接输出中文结论和下一步建议；需要英文时加 `--language en`。`RESP` 表示 HTTP 服务确实返回了非 2xx
状态，或 HTTP `200` 正文中的应用层 `code` 已表示错误；它不是客户端连接超时。`timeout` 表示在限定时间内没有
收到完整 HTTP 响应。只读模式只发 DNS、TCP、TLS、HTTP `GET` 和 `OPTIONS`，不发送 `POST`，不会上传代码或改变
设备状态。`--json` 可输出 JSON Lines，
方便把双方结果保存后发给运维：

```bash
python3 scripts/diagnose_uiflow_push.py --role client --repeat 5 --json \
  > uiflow-client-diagnostic.jsonl
```

结论码的判断方式：

| 结论码 | 含义 |
| --- | --- |
| `CLIENT_NETWORK_FAILURE` | 运行脚本的一方 DNS、TCP 或 TLS 失败，优先查该机器的出口网络。 |
| `UIFLOW_BASE_GATEWAY_ERROR` | 只读探测中的基础路径返回 502/503/504，优先查 UIFlow 网关到该路径的上游；这还没有证明推送 POST 失败。 |
| `ROUTE_REACHABLE_POST_UNTESTED` | 只读路由可达，但没有证明真实推送 POST。 |
| `UIFLOW_POST_TIMEOUT` | 已发出 POST，但客户端在超时前没有收到完整响应；双方都超时则优先查 UIFlow 网关/上游。 |
| `CLIENT_POST_TRANSPORT_FAILURE` | POST 在客户端传输层失败，没有收到 HTTP 状态。 |
| `POST_ACCEPTED` | UIFlow 接受了 POST；不代表设备已经执行或 ACK。 |
| `DEVICE_OFFLINE_REPORTED` | UIFlow 已进入业务逻辑并明确返回设备离线，优先查设备在线状态。 |
| `UIFLOW_SERVER_ERROR` | UIFlow 对 POST 返回 5xx，提供输出和服务端日志给运维。 |
| `UIFLOW_REJECTED_REQUEST` | UIFlow 返回 4xx，优先检查设备 ID 或请求契约。 |

只读检查不能区分“POST 路由本身超时”和“设备 ACK 等待导致的服务端超时”。在明确授权、且只使用测试设备时，
双方再各做一次单 POST 验证；脚本会强制 `--repeat 1`，默认发送一小段诊断代码：

```bash
python3 scripts/diagnose_uiflow_push.py \
  --role client \
  --execute \
  --device-id 'replace-with-test-device-id' \
  --timeout 120
```

该命令会改变指定测试设备上的代码，禁止使用生产设备。真实推送若需带资源或使用项目代码，仍应使用已有的
`aiflow_push.py push-code ... --execute`，不要把诊断脚本的 `POST` 结果当作设备执行证明。
