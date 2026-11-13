# data/link/（归链接体检会话）

链接体检随代码分发的数据。服务启动时读一次（`LinkData.load()`），**改完要重启服务**才生效。
文件一律 UTF-8；`#` 后面是注释，空行忽略。

| 文件 | 内容 | 命中后的特征 |
| --- | --- | --- |
| `blacklist.txt` | 本地黑名单，每行一个域名或 IP | `blacklist_hit`（单独就是 `high`） |
| `short_links.txt` | 公开短链服务的域名 | `short_link`（弱特征），并尝试防 SSRF 地展开 |
| `suspicious_tlds.txt` | 冷门 / 滥用较多的顶级域（不带点） | `suspicious_tld`（弱特征） |
| `brands.txt` | 银行、支付、电商、证券的**官方**域名，一行一个品牌 | 和品牌很像但不是官方域名时 `brand_lookalike` |
| `rdap_dns.json` | IANA 的 RDAP 服务地址表 | 查域名注册时间用 |

域名条目都按「本身或其子域名」匹配：写 `scam-demo.example` 会同时命中 `dl.scam-demo.example`。

## 怎么把演示用的域名加进黑名单

1. 只加团队自己掌握、确定用于演示的域名，或者保留的示例域名（RFC 2606 / 6761 的 `.example`、`.test`、`.invalid`）。
   **不要加真实存在的正常网站**，也不要加别人的域名。
2. 在 `blacklist.txt` 末尾加一行，例如 `demo-fake-shop.test`，后面可以写 `# 2026-11-20 路演用`。中文域名直接写中文即可。
3. 重启服务，然后单独验证一条：

   ```bash
   uv run python -m app.analyzers.link check --offline http://dl.demo-fake-shop.test/app.apk
   ```

   应当看到 `风险：high` 和 `blacklist_hit`。`selfcheck` 子命令会列出写错、解析不了的条目。
4. 命中时报告如实返回 `blacklist_hit`、`intel.provider = local`。不要为了演示效果去改注册时间或情报结论，
   代码里也没有这样的开关。

保留的示例域名（`.example`、`.test`、`.invalid`、`.localhost`、`.local`，以及 `example.<任意后缀>`，例如 `t.example.cn`）
**不会触发任何联网查询**（不展开、不查注册时间、不查外部情报），只做词法分析和本地黑名单匹配。

## RDAP 服务地址表

- 来源：<https://data.iana.org/rdap/dns.json>
- 取得日期：2026-11-11；文件内 `publication` 字段为 `2026-10-28T22:00:04Z`
- 用途：按顶级域找到 RDAP 服务地址。请求时不现取。`.cn` 不在表里，改用 CNNIC 的 WHOIS（`whois.cnnic.cn:43`）。
- 更新（可选，几个月一次即可）：在 `server/` 下执行
  `curl -o data/link/rdap_dns.json https://data.iana.org/rdap/dns.json`，然后更新上面的日期。

## 其余几张表

- `short_links.txt` 只收任何人都能生成的通用短链服务；电商、视频网站只能指向自家内容的分享短链不收。
- `suspicious_tlds.txt` 是本项目按经验整理的启发式名单，不是权威统计，可以按评测结果增删。
- `brands.txt` 写的是官方域名（真的），写错会把正常官网判成仿冒，增删时请核对官网。
  3 个字符及以下的品牌名（如 `jd`、`ccb`）只做精确匹配，6 个字符以上的才做编辑距离的模糊匹配。
