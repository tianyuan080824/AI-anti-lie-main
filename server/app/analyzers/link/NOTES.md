# 链接体检 `link` 实现说明

## 流程

每条链接一个任务，并行处理，结果逐步写进 `_LinkState`（`analyzer.py`）：

1. **抽取**（`extract.py`）：`inp.urls` 在前，再扫所有说话人的文本。有没有协议头都认；占位符（`<PHONE>` `<AMOUNT:200>` 等）、`3.5元`、`v1.2`、`下午3.30` 这类不算链接。同一链接（规范化后）只出现一次，保留 url 条目的 `origin`；只在文本里出现的 `origin` 是 `null`。每次最多 10 条（`MAX_LINKS`），多出来的计入 `extras.found`/`truncated`。
2. **词法特征 + 本地黑名单**（`features.py`，不联网，立即得到）。
3. **短链展开**（`net.py`）：只展开 `data/link/short_links.txt` 里的公开短链服务，逐跳防 SSRF（见下）。
4. **最终地址的可注册域名**：查注册时间（`domain_age.py`，RDAP，`.cn` 走 CNNIC WHOIS，结果缓存在 `link_domain_cache` 表）和已配置的外部情报（`intel.py`），两者并行。

预算：内部截止 = `ctx.remaining_s()` − 0.3 s。到点时取消没做完的步骤，用已有结果出报告，所以至少有词法特征和本地黑名单，状态仍是 `done`。保留的示例域名（`.example` `.test` `.invalid` `.localhost` `.local`、`example.*`）不做任何联网查询。

## 风险映射（只在 `features.assess_risk` 一处）

| 条件 | risk |
| --- | --- |
| 含 `blacklist_hit`（本地黑名单，或外部情报判定恶意，或跳转链上任一主机在黑名单） | `high` |
| 命中强组合：品牌仿冒+新注册、品牌仿冒+punycode、品牌仿冒+直链 APK、直链 APK+新注册、直链 APK+IP 主机 | `high` |
| 其他任意 reason（单独一个短链、单独一个可疑后缀等） | `medium` |
| 链接解析不了（`weixin://`、乱码等） | `unknown` |
| 什么特征都没发现 | `low`（**不等于安全**） |

`redirect_chain` = 至少跳转 2 次（短链跳一次到目标是正常的）。`domain_age_lt_30d` 只在查到注册时间时判断；查不到就留空，不算失败。

## 防 SSRF（`net.py`）

- 只允许 http/https、端口 80/443、不带 userinfo；
- 自己解析主机：十进制/十六进制/八进制/缩写 IPv4 都按 WHATWG 规则还原；
- DNS 解析出的**任一**地址不是公网（回环、私网、CGNAT、链路本地、云元数据、组播、保留、IPv4 映射/兼容 IPv6、NAT64、6to4、Teredo 里嵌的私网地址）就拒绝；`localhost`、单标签、`.internal` `.lan` `.local` `.home` `.corp` 等名字直接拒绝；
- 连接固定到解析出的 IP（Host 头 + TLS SNI 用原域名），防 DNS 重绑定；
- 不自动跟随跳转，每跳重新检查；最多 `link_max_hops` 跳；只读 `Location` 头，不读正文；`.apk` 地址不请求；黑名单/保留域名在请求前停下；
- 直接用 `httpx.AsyncHTTPTransport`（`trust_env=False`，不用系统代理，不复用连接）。

## 数据（`data/link/`，见那里的 README）

黑名单只有保留示例域名（`scam-demo.example` 等）；团队加演示域名的方法写在 README。RDAP 服务表来自 IANA bootstrap（发布于 2026-10-28T22:00:04Z，2026-11-11 下载）。没有编造任何注册时间或情报结论。

## 外部情报

- 已实现：微步 ThreatBook（`intel_threatbook_api_key`），只发可注册域名。
- 奇安信、腾讯：配置了密钥也不会调用，`selfcheck` 会提示「还没有接入」。
- 已配置的外部情报调用失败（连不上、超时、返回错、到点没查完）→ `degraded` 带 `intel_unavailable`，状态仍是 `done`。没配置时不会有这个标记。
- `intel.verdict` 取值：`malicious` / `unknown`。`intel.provider`：判定恶意的来源优先，否则有回答的外部情报，否则 `local`。

## 生命周期

`startup` 读数据文件（不联网）并建 HTTP 客户端；`shutdown` 关闭自己的客户端（可重复调用）；`cleanup` 删除过期的域名缓存；`selfcheck` 检查数据文件、RDAP 连通性和外部情报；`status_line` 显示数据条数和外部情报配置。

## 命令行

```bash
uv run python -m app.analyzers.link check https://t.cn/xxxx
uv run python -m app.analyzers.link check --offline "加我 t.cn/abc 领红包"
uv run python -m app.analyzers.link check --json https://example.com/
uv run python -m app.analyzers.link selfcheck
```

带空格的参数当聊天文本处理（`origin` 为空），否则当用户输入的链接（`origin="typed"`）。

## 测试（`tests/link/`）

所有请求都经过 respx 拦截，没打桩的请求直接失败；系统 DNS 和 WHOIS 在本目录测试里被替换成报错，不会联网。覆盖：抽取（协议头、占位符/数字、去重和 origin）、每个 reason 的命中和不命中样例、风险映射、SSRF（全部被拒目标断言没发请求，跳转/DNS 到内网、跳数、端口、协议、不下载正文、慢响应）、RDAP/WHOIS 解析、缓存、失败返回空、黑名单、外部情报恶意/失败、全部断网、网络卡住到点返回、`open_pipeline` 端到端、生命周期、命令行。

## 没有验证的

- Windows 上运行（包括 WHOIS 的 socket 和 `getaddrinfo`）；
- 用真实密钥调用微步（请求格式按公开文档写的，没有真实调通）；奇安信、腾讯没有实现；
- 对真实短链服务的展开（按要求没有访问真实可疑链接）；
- 开发时只联网了三次：下载 IANA RDAP 表、对 google.com 查一次 RDAP、对 cnnic.cn 查一次 WHOIS，用来确认格式。

## 和要求/契约的出入

- 每次最多分析 10 条链接；
- 只在文本里出现的链接 `origin` 为 `null`；
- `redirect_chain` 需要至少 2 次跳转；
- 外部情报判定恶意时也记作 `blacklist_hit`（契约里没有单独的「情报命中」reason）；
- 保留示例域名永不联网，所以演示黑名单链接时不会有注册时间。

## 给 Android 交接文档

- `risk`：`high` 高风险；`medium` 有可疑特征；`low` 是「没发现风险特征」，**不能写成「安全」**；`unknown` 是没法分析。
- `reasons` 是英文枚举，需要映射成中文说明（可参照 `__main__.py` 里的文字）。
- `domain_age_days`、`final_url`、`registrable_domain` 都可能为空；`intel.verdict` 为 `unknown` 时显示「暂无情报」而不是「安全」。
- `degraded` 里出现 `intel_unavailable` 时，提示「外部情报暂不可用，结果仅供参考」。

## 需要改的公共文件（未改，请上层会话决定）

- 可选：在日志配置里把 `httpx`/`httpcore` 的日志级别调到 WARNING，避免 RDAP 请求的 URL（含被查域名）逐条刷 INFO 日志。
- 可选：如需调整每次最多分析的链接数，可以在 `app/config.py` 加 `link_max_links`（现在是 `extract.MAX_LINKS = 10` 常量）。
