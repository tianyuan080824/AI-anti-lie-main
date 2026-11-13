# data/cases/（归案例与评测会话）

相似案例库的原始数据。每个 `*.jsonl` 文件每行一条案例（UTF-8，可带 BOM；空行和以 `//` 开头的行会被忽略）。

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `case_id` | 是 | 唯一编号，字母、数字、`_`、`-`，最长 64。导入按它插入或更新 |
| `title` | 是 | 标题，≤200 字 |
| `scene` | 是 | 契约的 `scene` 取值（`brushing_rebate` / `investment` / `impersonation_service` / `impersonation_acquaintance` / `deepfake` / `other`），不能是 `none` |
| `snippet` | 是 | 展示给用户的摘要，≤300 字，进报告的 `similar_cases[].snippet` |
| `body` | 是 | 正文，≤4000 字，只用来算向量，不进报告 |
| `source_name` | 是 | 来源名称，如发布通报的机构名 |
| `source_url` | 真实案例必填 | 来源链接，`http://` 或 `https://` 开头 |
| `published_at` | 否 | 发布日期 `YYYY-MM-DD` |
| `synthetic` | 否 | 默认 `false`。开发用合成样例写 `true` |

校验规则：

- 真实案例（`synthetic` 为 `false`）必须有 `source_name` 和 `source_url`，`source_name` 不能是「合成示例」。
- 合成样例（`synthetic: true`）的 `source_name` 必须是「合成示例」，`source_url` 必须为空（`null` 或不写）。
- 不认识的字段报错（防止拼错字段名）。

算向量用的文本是 `title + 换行 + body`；`title` 或 `body` 改了，下次导入会重新算这一条的向量，只改 `snippet`、来源等字段不会。

## 现有文件

- `synthetic.jsonl`：8 条开发用合成样例，标题带「【合成示例】」，正文写明「不是真实事件」。只用来验证流程，不代表任何真实事件。正式演示前可以删掉这个文件后加 `--prune` 重新导入。

## 添加真实案例

1. 从公安机关等**公开**通报里整理，每条记下来源机构名称和原文链接。不编写看起来像真实通报的内容，不编造链接。正文里不要出现受害人姓名、电话、账号等个人信息。
2. 新建一个文件，例如 `police_2026.jsonl`，一行一条。
3. 在 `server/` 目录下执行：

   ```bash
   uv run python -m app.analyzers.cases import --dry-run   # 只校验，不写库、不调用向量接口
   uv run python -m app.analyzers.cases import             # 导入并计算向量（需要 EMBEDDING_API_KEY 或 LLM_PRIMARY_API_KEY）
   uv run python -m app.analyzers.cases status             # 查看条数
   ```

4. **重启服务**后新案例才会生效（服务启动时一次性把向量读进内存）。

不合格的行会以「文件名:行号 原因」列出并跳过，其余行照常导入，命令返回非零；改好后重跑即可。
