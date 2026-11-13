# 一键鉴诈（方案 B）端云接口契约 v1

Android 客户端（`android/`）与鉴诈服务（`server/`）之间唯一的接口约定。两端各自实现，并各自把本文件的示例 JSON 做成测试夹具。任何一端需要改字段：先改本文件并单独提交、写明兼容影响，再改代码。

## 运行形态（笔记本版）

- 鉴诈服务跑在团队的笔记本上（Mac 为主，Windows 备用），不部署到云服务器。大模型密钥只放在笔记本上，不进 APK。
- 演示手机用数据线连接笔记本。在笔记本上执行 `adb reverse tcp:8000 tcp:8000` 之后，手机访问自己的 `127.0.0.1:8000` 就等于访问笔记本上的服务。App 默认的服务地址是 `http://127.0.0.1:8000`。
- 备用连接方式：手机开热点、笔记本连热点，App 里把服务地址改成笔记本的局域网地址。
- 明文 HTTP 只允许用于本机回环地址，其他地址一律 HTTPS。调试构建例外：它放开全部明文限制，供备用连接方式访问笔记本的局域网地址（Android 的网络安全配置按主机名放行，没法按网段放行）。调试构建只装在团队自己的演示机上，不对外分发。
- 笔记本自己要联网才能调用大模型。断网时服务按第 6 节降级，仍然返回报告。
- 接口本身与服务跑在哪里无关。以后搬到云服务器，只需要改服务地址。

## 1. 通用约定

- 基础路径 `/v1`。除媒体上传外，请求和响应都是 UTF-8 JSON，字段名 snake_case，时间为 ISO 8601 UTC。
- 鉴权：`Authorization: Bearer <APP_TOKEN>`。比赛阶段是固定令牌，只用来挡住同一网络里其他设备的误访问，不当作安全边界。
- 设备标识：`X-Device-Id: <首次启动时生成的随机 UUID>`，只用于限流和反馈归并。不得使用 IMEI、Android ID、手机号等能关联到人的标识。
- 兼容规则：v1 内只允许新增可选字段。客户端遇到不认识的枚举值按 `other` / `unknown` 展示，不得崩溃；服务端忽略不认识的字段。
- 限制（初始值，服务端可配置）：单次 `items` ≤ 8 个，`lines` 合计 ≤ 200 行，单行 ≤ 500 字，URL ≤ 5 个；图片 ≤ 5 MB，音频 ≤ 10 MB 且 ≤ 60 秒，视频 ≤ 50 MB 且 ≤ 30 秒。

## 2. 隐私边界（两端都必须遵守）

- 文本一律在手机上脱敏后才上传。占位符固定为 `<PHONE>`、`<CARD>`、`<ID>`、`<AMOUNT:数值>`（金额保留数值，例如 `<AMOUNT:200>`）。
- 脱敏顺序固定：先把 URL 整体保护起来不动 → 身份证号（18 位且校验位通过）→ 银行卡号（16–19 位连续数字且 Luhn 通过）→ 手机号（大陆 11 位）→ 金额（带货币符号或「元 / 块 / 万」单位的数字）。脱敏必须幂等：已经是占位符的内容不得被再次改写。
- 服务端收到文本后用同一套规则再脱敏一遍，之后才允许进入大模型、数据库和日志。
- 音频、视频和未遮盖的图片无法在端侧脱敏：客户端每次上传前单独征得同意；服务端分析结束立即删除原文件，最迟不超过 1 小时。
- 服务端只保存脱敏文本、报告和耗时指标，默认保留 7 天。日志里不得出现正文，只记 ID、长度和耗时。
- 向第三方威胁情报查询时只发送可注册域名（如 `example.cc`），不带路径和参数。

## 3. 枚举

| 名称 | 取值 |
| --- | --- |
| `verdict` | `high_risk` 高风险 · `suspicious` 可疑 · `low_risk` 暂未发现明显诈骗特征 · `inconclusive` 内容不足无法判断 |
| `scene` | `brushing_rebate` 刷单返利 · `investment` 虚假投资理财 · `impersonation_service` 冒充客服 · `impersonation_acquaintance` 冒充熟人或领导 · `deepfake` AI 换脸拟声 · `other` · `none` |
| `stage` | `s1_lead` 引流 · `s2_trust` 建立信任 · `s3_migrate` 转移阵地 · `s4_invest` 诱导投入 · `s5_harvest` 收割 |
| `intent` | `high_return_promise` 承诺高收益 · `small_rebate` 小额返利 · `induce_download` 诱导下载 · `request_payment` 要求转账或充值 · `urgency` 制造紧迫 · `secrecy` 要求保密 · `identity_packaging` 身份包装 · `block_verification` 阻止核实 |
| `speaker` | `other` 对方 · `self` 用户本人 · `system` 时间或系统提示 · `unknown` |
| 分析器 `name` | `script` 话术 · `link` 链接 · `cases` 相似案例 · `deepfake` 深伪 · `summary` 汇总 |
| 分析器 `status` | `pending` · `running` · `done` · `skipped` · `failed` |
| 任务 `status` | `queued` · `running` · `succeeded` · `failed` |
| 链接 `risk` | `high` · `medium` · `low` · `unknown` |
| 链接 `reasons` | `blacklist_hit` · `domain_age_lt_30d` · `brand_lookalike` · `suspicious_tld` · `ip_host` · `nonstandard_port` · `punycode` · `short_link` · `redirect_chain` · `apk_direct_link` |
| 媒体 `label` | `likely_synthetic` · `uncertain` · `likely_genuine` · `not_analyzed` |
| `degraded` | `backup_llm_used` · `llm_unavailable_rules_only` · `cross_check_disagreed` · `intel_unavailable` · `cases_unavailable` · `deepfake_unavailable` · `partial_timeout` |

`scene`、`stage`、`intent` 与立项文档 5.3、5.4 的标签体系一致，后续方案 A、C 复用同一套取值。

## 4. 接口

### 4.1 `POST /v1/media` 上传媒体（P1）

`multipart/form-data`，字段 `file`（二进制）和 `kind`（`audio` / `video` / `image`）。

~~~json
{"media_id": "m_01JCXYZ", "kind": "audio", "size_bytes": 482133, "expires_at": "2026-11-11T09:30:00Z"}
~~~

### 4.2 `POST /v1/checks` 发起鉴定

~~~json
{
  "client": {"app_version": "0.1.0", "os_version": "14", "device_model": "Xiaomi 14", "rules_version": "2026.11.1"},
  "source": "floating_ball",
  "items": [
    {
      "type": "chat",
      "origin": "screenshot_ocr",
      "lines": [
        {"line_id": "0-0", "speaker": "other", "text": "日结兼职，点赞截图一单3元"},
        {"line_id": "0-1", "speaker": "self", "text": "怎么做"},
        {"line_id": "0-2", "speaker": "other", "text": "先垫付<AMOUNT:200>返<AMOUNT:260>，下载这个 https://t.example.cn/a1"}
      ]
    },
    {"type": "url", "url": "https://t.example.cn/a1", "origin": "qrcode"},
    {"type": "audio", "media_id": "m_01JCXYZ"}
  ]
}
~~~

- `source`：`floating_ball` / `share` / `in_app` / `process_text`，只用于统计。
- `items[].type`：
  - `chat`：文本行。`origin` 为 `screenshot_ocr` / `pasted_text` / `shared_text`。粘贴的整段文字按换行拆成行，`speaker` 填 `unknown`。
  - `url`：`origin` 为 `typed` / `shared` / `qrcode`。正文里夹带的链接由服务端自己提取，客户端不用重复提交。
  - `audio` / `video`（P1）：引用 `media_id`。
  - `image`（P1）：引用 `media_id`，`masked` 表示敏感区域是否已在端侧涂黑。
- `line_id` 由客户端生成，格式 `<item 序号>-<行序号>`，单次请求内唯一。`text` 是归一化并脱敏之后的文本。

响应 `202`：

~~~json
{"check_id": "c_01JCXYZ", "status": "queued", "poll_after_ms": 800}
~~~

### 4.3 `GET /v1/checks/{check_id}` 查询进度与结果

客户端按 `poll_after_ms` 轮询，直到 `status` 为 `succeeded` 或 `failed`。五个分析器始终全部返回，顺序固定，客户端据此逐项点亮进度。

~~~json
{
  "check_id": "c_01JCXYZ",
  "status": "running",
  "poll_after_ms": 800,
  "analyzers": [
    {"name": "script", "status": "done", "elapsed_ms": 2100},
    {"name": "link", "status": "running"},
    {"name": "cases", "status": "done", "elapsed_ms": 420},
    {"name": "deepfake", "status": "skipped"},
    {"name": "summary", "status": "pending"}
  ],
  "derived_lines": [],
  "report": null,
  "error": null
}
~~~

- `derived_lines`：服务端自己从语音（转写）或图片（OCR）得到的文本行，结构与请求里的行相同并多一个 `item_index`，`line_id` 以 `d` 开头（如 `d2-0`）。客户端用它展示和高亮这部分证据。
- 任务级失败时 HTTP 仍是 200，`status` 为 `failed`，`error` 为第 7 节的错误对象。
- 结果保留期内可重复查询，过期返回 404。

### 4.4 `POST /v1/checks/{check_id}/feedback` 用户反馈（P1）

~~~json
{"label": "not_scam", "note": "这是我同事"}
~~~

`label`：`scam` / `not_scam` / `unsure`；`note` 可空，≤ 200 字，服务端同样先脱敏。响应 `204`。

### 4.5 `GET /v1/rules` 规则集下发（P1）

请求头 `If-None-Match: "<当前版本>"`，未更新返回 `304`。

~~~json
{
  "version": "2026.11.1",
  "normalize": {
    "phrases": {"薇信": "微信", "威信": "微信", "V信": "微信", "扣扣": "QQ"},
    "strip": ["​", "‌", "‍", "﻿"]
  },
  "rules": [
    {"id": "BR-003", "pattern": "垫付.{0,10}返", "scene": "brushing_rebate", "intent": "small_rebate", "weight": 1.2, "title": "垫付返利"},
    {"id": "IV-001", "pattern": "稳赚不赔|保本保息|内部消息", "scene": "investment", "intent": "high_return_promise", "weight": 1.0, "title": "承诺高收益"}
  ]
}
~~~

- `pattern` 只用 Kotlin（`java.util.regex`）和 Python `re` 都支持的公共子集：不用后行断言、占有量词、命名分组和内联标志。
- `normalize.phrases` 是短语级替换，不做单字替换（单字替换会把「威胁」改成「微胁」）。
- 同一份规则集随 APK 内置一份、服务端内置一份；P0 阶段不做热更新，两边用同一个文件。

### 4.6 `GET /v1/health`

~~~json
{"status": "ok", "version": "0.1.0", "rules_version": "2026.11.1", "mock": false, "llm": "ok", "db": "ok"}
~~~

- `mock`：服务是否运行在模拟模式。为 `true` 时返回的报告是写死的示例，客户端必须显示「模拟数据」标记。
- `llm`：`ok` / `down` / `unknown`，反映最近一次真实调用或启动自检的结果。健康检查本身不调用大模型。

## 5. 报告对象

~~~json
{
  "verdict": "high_risk",
  "risk_score": 87,
  "confidence": "high",
  "scene": "brushing_rebate",
  "stage": "s4_invest",
  "headline": "这很可能是刷单返利诈骗",
  "summary": "对方先用小额返利取得信任，现在要求你垫付资金并下载陌生应用。",
  "next_move": "接下来通常会以连单、卡单为由要求继续充值，之后无法提现。",
  "evidence": [
    {
      "line_id": "0-2",
      "quote": "先垫付<AMOUNT:200>返<AMOUNT:260>",
      "intent": "small_rebate",
      "source": "llm",
      "explanation": "要求先垫付再返利，是刷单诈骗的核心话术。"
    }
  ],
  "links": [
    {
      "url": "https://t.example.cn/a1",
      "final_url": "https://dl.example-app.cc/app.apk",
      "registrable_domain": "example-app.cc",
      "origin": "qrcode",
      "risk": "high",
      "reasons": ["short_link", "domain_age_lt_30d", "apk_direct_link"],
      "domain_age_days": 6,
      "intel": {"provider": "local", "verdict": "unknown"}
    }
  ],
  "media": [
    {"media_id": "m_01JCXYZ", "kind": "audio", "label": "likely_synthetic", "spoof_score": 0.71, "note": "合成语音检测仅供参考"}
  ],
  "similar_cases": [
    {
      "case_id": "case_0042",
      "title": "点赞返利后被要求垫付连单",
      "scene": "brushing_rebate",
      "snippet": "受害人先收到 3 元返利，随后被拉入群聊下载应用并垫付……",
      "source_name": "公开通报来源名称",
      "source_url": "https://example.gov.cn/notice/123",
      "published_at": "2026-05-12",
      "similarity": 0.84,
      "synthetic": false
    }
  ],
  "advice": ["不要垫付或充值任何资金", "不要安装对方发来的应用", "已经转账请立即拨打 110 并联系银行"],
  "actions": [{"type": "dial", "label": "拨打反诈专线 96110", "value": "96110"}],
  "degraded": [],
  "disclaimer": "本结果由算法生成，仅供参考。未发现风险不代表安全。"
}
~~~

字段说明：

- `risk_score`：0–100 的整数，由服务端代码计算，不是大模型直接给的数。
- `confidence`：`high` / `medium` / `low`。
- `scene` 为 `none` 时 `stage` 为 `null`。
- `headline` 由服务端按 `verdict` 和 `scene` 用固定模板生成；`summary`、`next_move`、`evidence[].explanation` 可由大模型撰写。
- `evidence[].source`：`llm` 或 `rule`。**保证**：`quote` 一定是 `line_id` 那一行 `text` 的逐字子串。客户端按行高亮，能在原文里找到 `quote` 时再精确到字。
- `similar_cases[].synthetic` 为 `true` 时是开发用的合成样例，此时 `source_url` 为 `null`、`source_name` 为「合成示例」；客户端必须标注「示例」，不得当作真实通报展示。
- `actions[].type`：`dial` / `open_url`。
- `links`、`media`、`similar_cases`、`evidence` 没有内容时返回空数组，不省略字段。

## 6. 判定规则（服务端负责，客户端只展示）

1. 结论由代码算，大模型只负责抽取信号。风险概率 p = sigmoid(偏置 + 通过证据回查的意图权重 + 规则命中权重 + 链接风险 + 相似案例 + 深伪分数（封顶）)，`risk_score = round(100·p)`。
2. 三支阈值，初始值取立项文档 5.7：p ≥ 0.60 为 `high_risk`，p ≤ 0.05 为 `low_risk`，中间为 `suspicious`。
3. 硬性约束，优先于阈值：
   - `high_risk` 至少要有一条通过回查的原文证据，或一条 `high` 风险链接。
   - 深伪分数单独不足以给出 `high_risk`。
   - 开启主备模型交叉复核时，如果按一方的证据算出 `high_risk`、按另一方算出 `low_risk`，最高给 `suspicious`，并带 `cross_check_disagreed`。一方 `high_risk`、另一方 `suspicious` 不算不一致。
   - 大模型全部不可用时只用规则和链接信号，最高给 `suspicious`（命中黑名单的链接除外），并带 `llm_unavailable_rules_only`。
   - 可分析文本不足 10 个字且没有链接和媒体时给 `inconclusive`。
4. 文案约束：`low_risk` 对外表述是「暂未发现明显诈骗特征」。两端任何界面和文案都不得出现「安全」「可以放心转账」一类的保证。

## 7. 错误

~~~json
{"error": {"code": "RATE_LIMITED", "message": "请求太频繁，请稍后再试", "retryable": true, "trace_id": "t_01JCXYZ"}}
~~~

| HTTP | `code` | 含义 |
| --- | --- | --- |
| 400 | `INVALID_INPUT` | 字段缺失、超出条数或长度限制 |
| 401 | `UNAUTHORIZED` | 令牌缺失或错误 |
| 404 | `NOT_FOUND` | 任务或媒体不存在、已过期 |
| 413 | `PAYLOAD_TOO_LARGE` | 文件超过大小或时长限制 |
| 415 | `UNSUPPORTED_MEDIA` | 文件类型不支持或无法解码 |
| 429 | `RATE_LIMITED` | 触发限流 |
| 500 | `INTERNAL` | 服务内部错误 |
| 503 | `UNAVAILABLE` | 服务暂不可用 |

`message` 是可以直接展示给用户的中文句子，不含堆栈、密钥和正文内容。
