# 相似案例 `cases`：实现说明

## 文件

| 文件 | 作用 |
| --- | --- |
| `embedder.py` | `Embedder` 接口 + `OpenAICompatEmbedder`（`POST {EMBEDDING_BASE_URL}/embeddings`） |
| `models.py` | 表 `cases_case`：契约 `similar_cases` 的字段 + 正文 `body` + 向量（float32 bytes）+ 向量型号/维度 + 内容哈希 |
| `index.py` | 启动时把向量读进 numpy 矩阵（行已归一化），余弦相似度检索 |
| `importer.py` | jsonl 逐行校验、按 `case_id` 插入或更新、按需重算向量 |
| `analyzer.py` | `CasesAnalyzer`：`startup` / `should_run` / `run` / `selfcheck` / `status_line` |
| `__main__.py` | 命令行：`status`、`import` |

jsonl 的字段和添加方法见 `server/data/cases/README.md`。

## 运行时行为

| 情况 | 结果 | 是否发请求 |
| --- | --- | --- |
| 没有对方 / 未知说话人的文字 | `skipped` | 否 |
| 案例库一条都没有 | `skipped`，不带降级标记（部署没做完，由 `status_line` 和 `selfcheck` 提醒） | 否 |
| 库里有案例，但向量型号或维度都和当前配置不符 | `failed` + `cases_unavailable`（note `index_unusable`） | 否 |
| 没有密钥 | `failed` + `cases_unavailable`（note `no_api_key`） | 否 |
| 本路剩余时间不足 | `failed` + `cases_unavailable`（note `no_time`） | 否 |
| 向量接口连不上、超时、非 200、返回格式或维度不对 | `failed` + `cases_unavailable`（note `embedding_<原因>`） | 是，一次 |
| 正常 | `done`，相似度 ≥ `CASES_SIMILARITY_THRESHOLD` 的前 `CASES_TOP_K` 条，降序 | 是，一次 |

- 查询文本：`speaker` 为 `other` 和 `unknown` 的行（已脱敏），按行用换行拼接，截断到 `CASES_QUERY_MAX_CHARS`（默认 1500 字）。
- 超时：读取超时取 `min(EMBEDDING_TIMEOUT_S, 本路剩余时间 − 0.1 秒)`；连接超时取 `min(LLM_CONNECT_TIMEOUT_S, EMBEDDING_TIMEOUT_S/2)`，同时不超过读取超时的一半，连不上就尽快放弃。外面再套一层 `asyncio.timeout` 兜底。
- `similarity` 保留 4 位小数，裁到 [0, 1]。
- `extras`（不对外）：`cases_total`、`cases_usable`、`embedding_model`、`query_chars`、`best_similarity`（不论是否过阈值，最高的那个分数，调阈值用）、`returned`。
- `startup()` 只读本地数据库，不联网；几百条 × 1024 维的矩阵乘法是毫秒级，没有放进线程。

## 向量型号 / 维度和配置对不上

每条案例存了算向量时的 `embedding_model` 和 `embedding_dim`。启动时只把和当前配置（`EMBEDDING_MODEL`、`EMBEDDING_DIM`）一致的向量放进索引，其余计数为「不符」，不和新查询比较（不同型号的向量放在一起算相似度没有意义）。

- `status_line()` 写出不符和缺向量的条数，提示重新导入；
- `selfcheck()` 有一项「案例向量与配置一致」，不一致就失败并给出导入命令；
- 重新运行 `import` 会自动重算这些案例的向量（型号或维度变化即触发）。

## 导入命令

```bash
uv run python -m app.analyzers.cases import            # 导入 server/data/cases/*.jsonl
uv run python -m app.analyzers.cases import --dry-run  # 只校验和统计，不写库、不调用向量接口
uv run python -m app.analyzers.cases import --prune    # 同时删除 jsonl 里已经没有的案例
uv run python -m app.analyzers.cases import --dir 路径 # 指定别的目录
uv run python -m app.analyzers.cases status            # 条数和向量状态（不联网）
```

- **可重复执行**：按 `case_id` 插入或更新。算向量的文本是 `title + 换行 + body`，存 sha256；文本、向量型号、维度都没变就不重新算，只更新其他字段。
- **逐行校验，不合格的行跳过**：以「文件名:行号 原因」列出，其余行照常导入，命令返回 1。选择跳过而不是中止，是为了一个人加的一条坏数据不挡住其他人的案例；返回值非零保证不会被忽略。
- **没有密钥**：只要有一条需要算向量，就什么都不写，提示配置 `EMBEDDING_API_KEY`（或沿用 `LLM_PRIMARY_API_KEY`），返回 1。只改了摘要等字段、不需要算向量时没有密钥也能更新。
- **向量接口中途失败**：每批（默认 10 条）算完就提交，失败时已完成的批次保留，提示重跑；重跑只补算剩下的。
- jsonl 里已经没有、库里还有的案例默认只提示，加 `--prune` 才删除。
- **导入后要重启服务**才能看到新案例：服务启动时一次性把向量读进内存，不做自动重载。命令结束时会打印这句提醒。

## 向量接口请求格式（未用真实密钥验证）

```http
POST {EMBEDDING_BASE_URL}/embeddings
Authorization: Bearer <密钥>
{"model": "text-embedding-v4", "input": ["..."], "dimensions": 1024, "encoding_format": "float"}
```

响应按 OpenAI 格式取 `data[].embedding`，按 `data[].index` 排序。一次最多 10 条（批大小写在 `OpenAICompatEmbedder.batch_size`）。

**没有用真实密钥跑过。** 开发时访问不了阿里云百炼的文档页面，上面的格式是按 OpenAI 兼容接口的通用约定写的，有三处要在拿到密钥后对照百炼控制台/官方文档确认：

1. `text-embedding-v4` 是否接受 `dimensions: 1024`；
2. 单次请求的最大条数（这里假设 10）；
3. 单条输入的长度上限（查询最多 1500 字，案例正文最多 4000 字 + 标题）。

验证方法：配置好 `.env` 后运行 `uv run python -m app.analyzers.cases import`；或者在第三阶段的自检命令里看「向量接口」一项。

## 测试

`uv run pytest tests/cases -q`：全部用假的 `Embedder` 或 respx 打桩，不联网。覆盖导入的幂等、逐行校验、何时重算向量、中途失败续传；检索的 top-k、阈值、降序、查询文本取法和截断；空库 / 失败 / 没密钥 / 时间不够的区分；启动加载和维度/型号不符；自检；通过 `open_pipeline(settings)` 的端到端；命令行。

## 没做的 / 留给第三阶段

- 真实向量接口没有实测；阈值 0.5 是配置里的默认值，没有用数据调过。
- Windows 上没有运行过（编码都显式 UTF-8，控制台强制 UTF-8 输出）。
- 自检命令本身（调用各分析器 `selfcheck()` 并打印）是第三阶段的公共工作。
