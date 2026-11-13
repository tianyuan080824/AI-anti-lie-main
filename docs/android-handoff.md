# Android 联调说明（给 Android 端）

这份说明写给没看过后端代码的 Android 端。接口以 `docs/api-contract.md` 为准，这里只讲怎么把服务跑起来、怎么联调，以及当前实现和契约的出入。

## 1. 现在能用什么

| 接口 | 状态 |
| --- | --- |
| `POST /v1/checks` 发起鉴定 | 已实现 |
| `GET /v1/checks/{check_id}` 查询进度与结果 | 已实现 |
| `GET /v1/health` | 已实现 |
| `POST /v1/checks/{check_id}/feedback` 用户反馈（P1） | 已实现，见第 5 节 |
| `GET /v1/rules` 规则集下发（P1） | 已实现，见第 5 节和第 9 节 |
| `POST /v1/media` 上传媒体（P1） | 已实现，见第 5 节「媒体上传」。服务端没装媒体依赖组时返回 503 `UNAVAILABLE` |

服务有两种跑法：

- **模拟模式**（建议先用这个联调界面）：不调用任何外部服务。五个分析器按约 0.6 秒的间隔依次点亮，最后返回的报告固定是契约示例 `shared/contract-examples/report.json`（`high_risk`、带证据、链接、媒体和相似案例，字段最全）。健康检查返回 `"mock": true`，App 必须显示「模拟数据」标记。
- **真实模式**（默认）：请求走完服务端流水线：服务端二次脱敏 → 规则 + 大模型话术分析（主备两个模型）→ 链接体检 → 相似案例 → 汇总打分，报告按契约第 6 节的规则产出（证据逐字回查、固定模板文案）。
  - **没有配置大模型密钥时**（现在就是这样）只用规则：结论最高 `suspicious`、`risk_score` ≤ 59（唯一例外：有链接命中黑名单时可以是 `high_risk`），`confidence: low`，`degraded` 带 `llm_unavailable_rules_only`，证据来源是 `rule`，`summary` 末尾说明大模型暂时不可用。这正好用来测降级状态的展示。
  - 内容太少（不到 10 个字且没有链接和媒体）时是 `inconclusive`、`risk_score` 0。
  - 媒体条目：图片做文字识别，语音做转写和合成语音检测，视频只分析音轨。识别出的文字作为派生行（`derived_lines`）和聊天文字一起分析。分析不了时（服务端没装依赖、模型还在加载、超时）媒体条目是 `not_analyzed`、报告带 `deepfake_unavailable`，其余照常出报告。详见第 5 节「媒体上传」。

链接体检和相似案例由另外两个后端会话实现，进度见 `docs/server-progress.md`。

## 2. 在笔记本上启动服务

需要 Python 3.12 和 uv（团队的 Mac 上已装好）。第一次运行先装依赖：

```bash
cd "AI anti lie/server"
```

```bash
uv sync
```

模拟模式：

```bash
uv run python -m app --mock
```

真实模式：

```bash
uv run python -m app
```

启动后控制台会打印一段状态，第一行下面能看到监听地址（默认 `http://127.0.0.1:8000`）和「模拟模式 开启/关闭」。按 Ctrl+C 停止。详细说明见 `server/README.md`。

联调真实模式前可以先跑一次启动前自检，逐项看规则、数据库、主备模型、向量接口是否可用（没填密钥的项显示「未配置」）：

```bash
uv run python -m app.selfcheck
```

路演当天的顺序、常见故障（手机连不上、大模型调不通、报告一直降级）见 `server/README.md` 第 4、5 节。

## 3. 手机连上服务

**数据线 + adb reverse（默认方式）**：手机用数据线连笔记本、打开 USB 调试，在笔记本上执行：

```bash
adb reverse tcp:8000 tcp:8000
```

之后手机上的 `http://127.0.0.1:8000` 就是笔记本上的服务，App 默认地址不用改。拔插数据线后转发是否还在**没有验证过**（后端的 Mac 上没有 adb），在验证之前每次重新插线都再执行一次（重复执行没有副作用），用 `adb reverse --list` 确认。

**热点（备用方式）**：手机开热点、笔记本连上，服务改为监听所有网卡：

```bash
uv run python -m app --mock --host 0.0.0.0
```

启动输出里会列出笔记本的局域网地址（形如 `http://192.168.x.x:8000`），App 里把服务地址改成它（只有调试构建允许这种明文地址），笔记本防火墙需要放行 8000 端口。

## 4. 请求头

| 请求头 | 要求 |
| --- | --- |
| `Authorization: Bearer <令牌>` | 所有 `/v1/*` 接口都要，**包括 `/v1/health`**。默认令牌 `dev-token`；笔记本上改了 `server/.env` 的 `APP_TOKEN` 时用新的值 |
| `X-Device-Id` | `POST /v1/checks` **必填**：首次启动生成的随机 UUID。只允许字母、数字、`-`、`_`，1–64 个字符。缺失或格式不对返回 400。反馈接口可以不带，带了就按同样的格式检查 |
| `If-None-Match: "<版本>"` | `GET /v1/rules` 可选：带上本地规则集的版本（含双引号，也就是上次响应的 `ETag`），没更新时返回 304 |
| `Content-Type: application/json` | POST 时 |

## 5. curl 示例

在笔记本上直接测（手机上 `adb reverse` 后地址相同）。以下命令都在 `AI anti lie/server/` 目录下执行。

**健康检查**

```bash
curl -s http://127.0.0.1:8000/v1/health -H "Authorization: Bearer dev-token"
```

```json
{"status":"ok","version":"0.1.0","rules_version":"2026.11.2","mock":true,"llm":"unknown","db":"ok"}
```

**发起鉴定**（用契约示例请求）

```bash
curl -s -X POST http://127.0.0.1:8000/v1/checks -H "Authorization: Bearer dev-token" -H "X-Device-Id: 0f8fad5b-d9cb-469f-a165-70867728950e" -H "Content-Type: application/json" --data @../shared/contract-examples/check-request.json
```

HTTP 202：

```json
{"check_id":"c_01M75E0ZEX93H50YJ76N6RPCWR","status":"queued","poll_after_ms":800}
```

**轮询**（`check_id` 换成上一步返回的值，按 `poll_after_ms` 间隔重复，直到 `status` 为 `succeeded` 或 `failed`）

```bash
curl -s http://127.0.0.1:8000/v1/checks/c_01M75E0ZEX93H50YJ76N6RPCWR -H "Authorization: Bearer dev-token"
```

进行中（模拟模式下能看到逐项点亮）：

```json
{"check_id":"c_...","status":"running","poll_after_ms":800,"analyzers":[{"name":"script","status":"done","elapsed_ms":2100},{"name":"link","status":"running"},{"name":"cases","status":"pending"},{"name":"deepfake","status":"pending"},{"name":"summary","status":"pending"}],"derived_lines":[],"report":null,"error":null}
```

完成：`status` 为 `succeeded`，`report` 是完整的报告对象，结构见契约第 5 节和 `shared/contract-examples/report.json`。

**错误示例**

不带令牌，HTTP 401：

```bash
curl -s http://127.0.0.1:8000/v1/health
```

```json
{"error":{"code":"UNAUTHORIZED","message":"访问令牌缺失或错误，请检查 App 里的访问令牌设置","retryable":false,"trace_id":"t_..."}}
```

查不存在或已过期的鉴定，HTTP 404：

```bash
curl -s http://127.0.0.1:8000/v1/checks/c_NOPE -H "Authorization: Bearer dev-token"
```

```json
{"error":{"code":"NOT_FOUND","message":"鉴定记录不存在或已过期","retryable":false,"trace_id":"t_..."}}
```

空的 `items`，HTTP 400：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/checks -H "Authorization: Bearer dev-token" -H "X-Device-Id: 0f8fad5b-d9cb-469f-a165-70867728950e" -H "Content-Type: application/json" -d '{"source":"in_app","items":[]}'
```

```json
{"error":{"code":"INVALID_INPUT","message":"至少需要提交一条内容","retryable":false,"trace_id":"t_..."}}
```

所有错误都是这个结构；`message` 是可以直接展示给用户的中文；`retryable` 为 `true` 时可以提供「重试」按钮；`trace_id` 也在响应头 `X-Trace-Id` 里，反馈问题时附上它，后端能在日志里找到这次请求。

**用户反馈**（契约 4.4，P1）：`label` 是 `scam` / `not_scam` / `unsure`，`note` 可省略，最多 200 字。

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/v1/checks/c_.../feedback -H "Authorization: Bearer dev-token" -H "X-Device-Id: 0f8fad5b-d9cb-469f-a165-70867728950e" -H "Content-Type: application/json" --data @../shared/contract-examples/feedback-request.json
```

- 成功返回 **204**，没有响应体。
- `check_id` 不存在或已过期（7 天）返回 404 `NOT_FOUND`；`label` 不对、`note` 超过 200 字返回 400 `INVALID_INPUT`。
- 服务端会先对 `note` 做二次脱敏再保存（和聊天内容同一套规则），App 端照样先脱敏。
- 同一次鉴定再次反馈时**覆盖**上一条（只留最新的）。反馈和鉴定一起在 7 天后删除。
- 模拟模式下也能用（存进数据库，不影响模拟报告）。

**规则集下发**（契约 4.5，P1）：

```bash
curl -s -i http://127.0.0.1:8000/v1/rules -H "Authorization: Bearer dev-token" -H 'If-None-Match: "2026.11.2"'
```

- 返回服务端的 `shared/rules.json`（格式同契约 4.5），响应头 `ETag: "2026.11.2"`（版本号加双引号）、`Cache-Control: no-cache`。
- 请求带 `If-None-Match` 且和当前版本相同时返回 **304**，没有响应体，`ETag` 照样带上。支持 `W/"…"`、逗号分隔的多个值和 `*`；不带引号的裸版本号（`2026.11.2`）**不算命中**，会返回完整规则集。
- 服务端的规则文件缺失或损坏时返回 503 `UNAVAILABLE`（`retryable: true`），这时继续用 App 内置的规则。
- 规则文件改动后不用重启服务，下一次请求就是新版本。

**媒体上传**（契约 4.1，P1）：先上传拿 `media_id`，再在 `POST /v1/checks` 的 `items` 里引用它。

```bash
curl -s -X POST http://127.0.0.1:8000/v1/media -H "Authorization: Bearer dev-token" -F kind=image -F file=@screenshot.png
```

HTTP 201：

```json
{"media_id":"m_01M78F8B3E0AT1ZEVMTY49NS27","kind":"image","size_bytes":37184,"expires_at":"2026-11-12T19:08:25Z"}
```

然后提交鉴定（可以和聊天文字放在同一个请求里）：

```json
{"source":"in_app","items":[{"type":"image","media_id":"m_01M78F8B3E0AT1ZEVMTY49NS27"}]}
```

- **上传前单独征得用户同意**：录音、截图可能含第三方的声音和隐私，和提交文字分开问，用户不同意就不上传，只提交文字。
- **字段**：`multipart/form-data`，`kind` 是 `image` / `audio` / `video`，`file` 是文件。服务端按内容认格式，不看扩展名和 `Content-Type`。请求要带 `Content-Length`（OkHttp 的 `MultipartBody` 配文件或字节数组的 `RequestBody` 会自动带上；不要用长度未知的流）。
- **限制**：图片 5 MB；音频 10 MB、60 秒；视频 50 MB、30 秒。超出返回 413 `PAYLOAD_TOO_LARGE`。
- **错误码**：400 `INVALID_INPUT`（缺字段、`kind` 不对、空文件、没有 `Content-Length`）；413 `PAYLOAD_TOO_LARGE`；415 `UNSUPPORTED_MEDIA`（认不出的格式，或视频里没有音轨）；503 `UNAVAILABLE`（服务端没装媒体依赖组或关了媒体分析，`retryable: true`，这时只提交文字）；500 `INTERNAL`。
- **有效期和删除**：上传后 1 小时内没被鉴定引用就删除（`expires_at`）；被引用的文件在分析读进内存后立即删除。一个 `media_id` 只能用一次，重新鉴定要重新上传。
- **结果怎么看**：
  - `report.media[]` 每个媒体一项：语音和视频的 `label` 是 `likely_synthetic` / `likely_genuine` / `uncertain`，加 `spoof_score`；图片只做文字识别、不做合成检测，`label` 固定 `not_analyzed`。`note` 是可以直接展示的说明，都带「仅供参考」。
  - 视频只分析音轨，画面是否换脸**没有检测**，`note` 里会写明。
  - 识别出的文字在轮询响应顶层的 `derived_lines` 里：`line_id` 形如 `d<items 序号>-<行号>`（`d0-0` 是 `items[0]` 识别出的第 1 行），`speaker` 固定 `unknown`，`text` 已经服务端脱敏。这些行和聊天文字一起参与话术、链接、案例分析，`report.evidence[].line_id` 可能指向它们。**高亮证据时，`line_id` 以 `d` 开头的到 `derived_lines` 里找那一行**，把它显示成「从截图 / 语音里识别出的文字」。
  - 识别失败、超时或服务端缺依赖时，媒体条目是 `not_analyzed`，报告带 `deepfake_unavailable`，`derived_lines` 为空，其余各路照常出报告。
- **服务刚启动时**：语音转写模型在后台加载，本机实测约 17 秒。这期间提交的语音只做合成检测，不转写（没有派生行，报告带 `partial_timeout`）。图片识别模型约 2 秒就绪。
- **实测用时**（Mac 本机 CPU，合成素材，模型已加载，没有大模型密钥）：上传 0.1–0.25 秒；图片从提交到出报告 0.7–1.0 秒；4.5 秒的语音 1.8–3.2 秒。

## 6. 契约示例 JSON

契约里的每段示例都拆成了单独的文件，放在 `AI anti lie/shared/contract-examples/`，Android 端可以直接拿来做测试夹具：

| 文件 | 内容 |
| --- | --- |
| `check-request.json` | `POST /v1/checks` 请求体 |
| `check-created.json` | 202 响应 |
| `check-status-running.json` | 进行中的轮询响应 |
| `report.json` | 完整报告对象（模拟模式返回的就是它） |
| `error.json` | 错误响应 |
| `health.json` | 健康检查响应 |
| `feedback-request.json` | 反馈请求体（已实现） |
| `rules-response.json` | 规则集下发响应的格式（已实现；示例里的规则是节选，服务端返回完整的 `shared/rules.json`） |
| `media-upload-response.json` | 媒体上传的 201 响应（已实现；`media_id` 实际是 `m_` 加 26 位字母数字） |

后端测试会逐个解析这些文件并检查序列化结果和原文一致，所以它们和服务端实现保持同步。

## 7. 已知出入（实现 vs 契约）

这些是契约没写清楚、由后端自行决定的地方，或者有意的差异。Android 端照这里处理即可。

1. **`/v1/health` 也要鉴权**：契约没单独说，后端对所有 `/v1/*` 一视同仁。
2. **`X-Device-Id` 在 `POST /v1/checks` 上是必填的**，缺失或格式不对返回 400 `INVALID_INPUT`（契约只说了要带）。GET 接口不检查它，带上也无妨。
3. **`POST /v1/checks` 的请求体超过 1 MB 返回 400 `INVALID_INPUT`**，不是 413（契约里 413 专指媒体文件过大）。`POST /v1/media` 不受这个 1 MB 限制，按文件类型各自的上限，超限返回 413，见第 5 节「媒体上传」。
4. **超出数量/长度限制**（`items` > 8、行数合计 > 200、单行 > 500 字、URL > 5 个、单个 URL 超过 2048 字符）返回 400 `INVALID_INPUT`，`message` 说明是哪一项超限，不回显内容。URL 长度上限 2048 是后端额外加的。
5. **同一请求里 `line_id` 重复**返回 400。
6. **不认识的 `items[].type`** 被忽略、不报错也不保存（契约的兼容规则）。不认识的 `speaker` 按 `unknown` 处理。
7. **限流**：每台设备每分钟 20 次、可以突发 10 次，超出返回 429 `RATE_LIMITED`，响应头带 `Retry-After`（秒）。
8. **报告里从正文提取出的链接**（以后由链接体检产生）`origin` 为 `null`；用户以 `url` 条目提交的链接保留请求里的 `origin`。
9. **`elapsed_ms`** 只在分析器 `done` 或 `failed` 时出现；`pending`、`running`、`skipped` 不带这个字段。
10. **降级标记**：契约没有通用的「某分析器失败」标记，后端的映射是话术失败 → `llm_unavailable_rules_only`、链接失败 → `intel_unavailable`、案例失败 → `cases_unavailable`、媒体失败 → `deepfake_unavailable`；超时再加 `partial_timeout`。
11. **服务重启**时，正在进行的鉴定被标为 `failed`，`error.code` 为 `INTERNAL`、`retryable: true`，`message` 是「服务重启，这次鉴定没有完成，请重新提交」。HTTP 仍是 200（契约规定的任务级失败）。
12. **`rules_version`** 是 `shared/rules.json` 的 `version`，现在是 `"2026.11.2"`（69 条规则；`2026.11.1` 的 5 条规则在 `2026.11.2` 收紧了，见第 9 节第 7 条）。文件缺失或损坏时是 `"none"`。规则集怎么在 App 里用见下面第 9 节。
13. **`llm`** 反映**最近一次真正调用了大模型的鉴定，或启动前自检的结果**（契约 4.6）：
    - 服务启动时，如果 12 小时内跑过启动前自检（`uv run python -m app.selfcheck`），先用自检的结果：主备任一调通是 `"ok"`，都不通是 `"down"`；没跑过自检、自检太旧或自检时没配置密钥时是 `"unknown"`。
    - 之后每次真正调用了大模型的鉴定都会更新它：主备任一成功是 `"ok"`，都失败是 `"down"`。只有链接、没有聊天文字的鉴定，以及剩余时间不够、没去调用大模型的鉴定，不改变这个值。
    - 没有配置密钥时请求不会去调大模型，自检也只显示「未配置」，所以一直是 `"unknown"`。
14. **`check_id`** 格式是 `c_` 加 26 位大写字母数字（例如 `c_01M75E0ZEX93H50YJ76N6RPCWR`），比契约示例 `c_01JCXYZ` 长，App 不要假设长度。
15. **模拟模式的报告**里有媒体结果，所以 `deepfake` 这一项是 `done` 而不是 `skipped`；真实模式只有请求里带了媒体条目时 `deepfake` 才会运行。模拟模式下 `POST /v1/media` 照样真实校验并保存文件、返回 201，但鉴定结果仍是固定的模拟报告，`derived_lines` 为空，上传的文件不会被分析。模拟模式不跑清理任务，文件要等切回真实模式、1 小时过期后才会被删掉。
16. **媒体上传成功返回 201**（契约 4.1 没规定状态码）。**上传必须带 `Content-Length`**（分块传输会被拒，400）。**同一个 `media_id` 在一次鉴定里只能出现一次**，重复返回 400；一个媒体只能被一次鉴定使用，第二次引用时媒体条目是 `not_analyzed`（「这份媒体已经分析过」）。
17. **`line_id` 不能是 `d<数字>-<数字>` 的形式**（例如 `d0-1`），这类编号留给服务端的派生行，提交了返回 400。`d-note`、`dx1` 这类不受影响。
18. **服务端二次脱敏已实现**（契约第 2 节的同一套规则，可以重复执行，已经是占位符的内容不会被再改）。App 端仍然要先脱敏：服务端只是兜底。报告里的 `evidence[].quote` 是**服务端脱敏后**那一行的逐字子串，所以引文里可能出现 `<AMOUNT:200>`、`<PHONE>` 这类占位符，和 App 端本地显示的原文不完全一样。高亮原文时请按 `line_id` 找到那一行，再在 App 端自己脱敏后的文本里找引文；找不到时只显示引文、不高亮。
19. **数字后面带单位的写法**（比如「第8块」）也会被当成金额脱敏成 `第<AMOUNT:8>`。这是契约第 2 节金额规则的字面结果，宁可多脱也不漏。
20. **`suspicious` 的 `risk_score`** 保证在 6–59 之间，`high_risk` ≥ 60，`low_risk` ≤ 5（`risk_score == round(100·p)`，只在阈值附近为了和结论同档做了钳制）。
21. **只有规则可用时**（大模型不可用），报告里的证据来源是 `rule`；大模型可用时只列大模型给出、且回查通过的证据（来源 `llm`），规则命中只参与打分，不重复列出。
22. **服务端脱敏比契约第 2 节的字面多做了几件事**，App 端的脱敏请照着做，否则同一行两端脱出来不一样，引文高亮会对不上（`server/app/redact.py`，测试在 `server/tests/test_redact.py`）：
    - 匹配前把全角数字 `０`–`９` 换成 ASCII、全角加号 `＋` 换成 `+`；夹在两个数字之间的全角小数点 `．` 换成 `.`；紧跟 17 位数字的全角 `Ｘ`/`ｘ` 换成 `X`/`x`（身份证校验位）。全角逗号 `，` **不换**：「3000，5000元」是两个数，合成一个会把金额改错。
    - 手机号前面的国家码 `+86`、`0086`、`+0086`（中间可以有一个空格、`-`、全角 `－` 或全角空格）连同号码一起换成 `<PHONE>`，不留下 `+86<PHONE>`。前面没有 `+` 或 `00` 的「86」不算国家码（「我86年生的…」）。
    - 不带协议的「域名:端口」里端口最多 5 位，后面不能再紧跟数字（`a.cn:13800138000` 是 `a.cn:<PHONE>`）。带协议（`https://…`）或 `www.` 开头的链接整体保护，不受这条限制。
    - Kotlin 里用 `[0-9]`，不要用 `\d`（Java 的 `\d` 默认只认 ASCII，但带 `UNICODE_CHARACTER_CLASS` 时会认全角数字），先做上面的全角转换。这些写法**没有在 Kotlin 上跑过**。

## 8. 没有验证过的

- 后端这边的 Mac 上没有 adb，`adb reverse` 和手机实际访问**没有在真机上验证**；热点方式也没用手机测过。第一次联调如果连不上，先在手机浏览器里打开 `http://127.0.0.1:8000/v1/health`（会返回 401 的 JSON，说明网络是通的）。
- 服务没在 Windows 上跑过。
- 媒体：只用合成素材（Pillow 画的聊天截图、`say` 合成的语音）在 Mac 上测过，**没有用真实录音和截图测过**，合成语音检测的准确率**没有验证**；没有测过视频的端到端；没有在手机上实际上传过。实测里有 1 次（约 9 次中）语音转写没在 6 秒预算内完成、媒体记成「分析超时」，原因没查清，之后 8 次没有复现。
- 断网只做了**模拟**（把大模型、向量、RDAP、WHOIS 地址指到本机连不上的端口）：连接被拒时 0.1–0.3 秒、连上但不回应时约 12 秒拿到 `suspicious` 报告，`degraded` 带 `llm_unavailable_rules_only` 和 `cases_unavailable`。真实关掉 Wi-Fi 的情况没有测，App 的轮询超时请至少按 20 秒加上网络余量设置。
- `shared/rules.json` 的正则只用 Python 做过语法检查（测试会拒绝后行断言、命名分组、内联标志、占有量词，见契约 4.5），**没有在 Kotlin/Java 正则引擎上实际跑过**。接入时请先跑一遍下面第 9 节说的样例测试。
- 没有任何大模型密钥，真实模型下的结论（包括「必须通过的场景」）**没有验证过**，只用打桩模型验证了流程和打分。

## 9. 在 App 里使用规则集 `shared/rules.json`

服务端和 App 用同一份规则文件做离线 / 兜底判断。版本号就是健康检查里的 `rules_version`。

1. **拷贝**：把 `AI anti lie/shared/rules.json` 拷到 App 的 `assets/`。规则改动时后端会改 `version`，并在 `docs/server-progress.md` 里写明。App 可以比较本地 `version` 和健康检查返回的 `rules_version`；不一致时可以用 `GET /v1/rules`（带 `If-None-Match: "<本地版本>"`）拉新版本，拿到后先按下面第 5 条跑一遍样例测试的思路校验（至少每条 `pattern` 能在 Kotlin 里编译），失败就继续用内置的。拉不到（断网、503）时一律用内置的。
2. **结构**：
   - `normalize.strip` 是要删掉的零宽字符；
   - `normalize.phrases` 是变体词替换表（「薇信」→「微信」等）；
   - `rules[]` 每条有 `id`、`pattern`（正则）、`scene`、`intent`、`weight`、`title`。`scene` 和 `intent` 的取值与契约枚举一致。
3. **匹配顺序**（和服务端 `server/app/rules/__init__.py` 一致）：
   1. 先按契约第 2 节**脱敏**。规则里有 `<AMOUNT:` 这样的写法，是在脱敏后的文本上写的，所以必须先脱敏再匹配。
   2. 逐字符做 Unicode **NFKC** 归一化，删掉 `strip` 里的字符。
   3. 按 `phrases` 做替换，**长的词优先**。
   4. 对每条规则做一次 `find`（不要求整行匹配），每条规则在一行里只取第一处命中。
4. **只匹配对方说的话**：`speaker` 为 `other` 或 `unknown` 的行。自己说的话不参与。
5. **测试**：`shared/rules-samples.json` 按规则 `id` 给出 `hit`（必须命中）和 `miss`（必须不命中）样例。样例是脱敏后的文本，直接走第 3 步的 2–4 即可。服务端测试 `server/tests/test_rules.py` 用的就是这份文件，建议 App 端也写一个读它的单元测试，这样能发现 Kotlin 和 Python 正则行为的差异。
6. **怎么用命中结果**由 App 决定。服务端的做法是：命中规则的权重乘系数后计入打分；没有大模型时，最高只给到 `suspicious`（链接命中黑名单除外）。App 离线时**不要因为规则命中就给出 `high_risk`**，也不要因为没命中就说「安全」。
7. **`2026.11.2` 的改动**（只收紧，不新增、不删除规则）：IA-003、SC-001、BR-003、BR-012、UR-002 加了先行断言 `(?!…)`，排除「帮我转发 / 转告 / 打个电话」「别告诉别人我买了」「先垫付了，回头返我就行」「作废吧这张券」「两小时内没到我就走了」这类日常说法。先行断言在契约 4.5 允许的子集里，Java 也支持；对应的不命中样例，以及「别告诉家人我是警察」「24小时内未到我行办理」这类仍须命中的诈骗说法，已加进 `rules-samples.json`。已经拷过 `2026.11.1` 的请重新拷一次。
