# 鉴诈服务架构与模块接口

第二阶段的三个会话开工前先读这份文档。**本文档里的接口在第一阶段结束时冻结**，之后只做向后兼容的补充（加可选字段、加新钩子），不改名、不删字段、不改语义。需要不兼容的改动时，先找话术/汇总会话（接口的维护者）商量。

对外接口以 `AI anti lie/docs/api-contract.md` 为唯一权威；本文档只讲服务内部怎么分工。

## 1. 目录与归属

```text
AI anti lie/
├── shared/
│   ├── contract-examples/*.json     契约里每个示例 JSON 一个文件（两端测试共用）   话术/汇总
│   ├── rules.json                   规则集（两端共用）                             话术/汇总
│   └── rules-samples.json           每条规则的命中/不命中样例                      话术/汇总
└── server/
    ├── pyproject.toml, uv.lock      依赖（只有话术/汇总会话能改）                  话术/汇总
    ├── app/
    │   ├── contract.py              契约的 Pydantic 模型                           话术/汇总
    │   ├── config.py                统一配置（各分析器的配置项已预先声明）         话术/汇总
    │   ├── paths.py, ids.py, logs.py, errors.py, services.py, mock.py, main.py, __main__.py
    │   ├── api/                     HTTP 层：路由、鉴权中间件、限流                话术/汇总
    │   ├── checks/                  任务服务与 checks 表的存取                     话术/汇总
    │   ├── db/                      数据库引擎、Base、核心表                       话术/汇总
    │   ├── pipeline/                流水线：输入校验、四路并行、汇总、open_pipeline 话术/汇总
    │   ├── redact.py                服务端二次脱敏                                 话术/汇总
    │   ├── rules/                   规则加载与匹配                                 话术/汇总
    │   ├── llm/                     大模型客户端与健康状态                         话术/汇总
    │   ├── prompts/                 提示词（按版本号命名，如 script_v1.md）        话术/汇总
    │   ├── scoring/                 打分与权重 weights.yaml                        话术/汇总
    │   ├── summary/                 汇总、固定文案 templates.yaml                  话术/汇总
    │   └── analyzers/
    │       ├── __init__.py, base.py 分析器注册表和统一接口                         话术/汇总
    │       ├── script/              话术分析                                       话术/汇总
    │       ├── deepfake/            媒体与深伪（P1）                               话术/汇总
    │       ├── link/                链接体检                                       ★ 链接体检会话
    │       └── cases/               相似案例                                       ★ 案例与评测会话
    ├── data/link/                   黑名单、品牌域名表、RDAP 服务地址表等          ★ 链接体检会话
    ├── data/cases/                  案例 jsonl                                     ★ 案例与评测会话
    ├── eval/                        评测脚本和评测数据                             ★ 案例与评测会话
    └── tests/
        ├── conftest.py, test_*.py   公共夹具与核心测试                             话术/汇总
        ├── link/                    链接体检的测试                                 ★ 链接体检会话
        ├── cases/                   相似案例的测试                                 ★ 案例与评测会话
        └── eval/                    评测脚本的测试                                 ★ 案例与评测会话
```

规则：**每个会话只改自己目录里的文件**。三个会话共用一个 git 索引，提交一律带路径：

```bash
git add -- "server/app/analyzers/link" "server/tests/link" "server/data/link"
git commit -m "feat(server): ..." -- "server/app/analyzers/link" "server/tests/link" "server/data/link"
```

不要用 `git add .`、`git add -A`、`git commit -a`，不要切换分支、不要 stash。

## 2. 请求的一生

```text
POST /v1/checks
  → 中间件：trace_id、鉴权（401 先于格式错误）、请求体大小
  → 路由：X-Device-Id 校验、按设备限流
  → pipeline/inputs.py: build_input()  数量/长度限制 + 对每行 redact_text()  → AnalysisInput
  → checks 表入库(queued)，返回 202
  → 后台任务（进程内并发上限 MAX_CONCURRENT_CHECKS）
       pipeline/runner.py: Pipeline.run(inp, on_update)
         ├─ script   ┐
         ├─ link     │ asyncio 并行，各自超时；每次状态变化 on_update → 写库
         ├─ cases    │
         └─ deepfake ┘
         → summary（纯代码；失败时用兜底报告）
         → 各路 degraded 并进报告
  → checks 表入库(succeeded / failed)
GET /v1/checks/{id} 读库返回进度和报告
```

`MOCK_MODE=1` 时不加载分析器，`app/mock.py` 按 `MOCK_STEP_MS` 依次点亮五项，最后返回 `shared/contract-examples/report.json`。

## 3. 分析器接口（冻结）

定义在 `app/analyzers/base.py`。

### 3.1 包的约定

每个分析器是 `app/analyzers/<名字>/` 一个包，包名固定（注册表 `app/analyzers/__init__.py` 的 `PACKAGES`，不需要改它）：

| 文件 | 必需 | 作用 |
| --- | --- | --- |
| `__init__.py` | 是 | 导出 `create() -> Analyzer`，返回的实例 `name` 必须等于这一路的 `AnalyzerName` |
| `models.py` | 否 | 自己的数据表，建表时自动导入（见第 5 节） |
| `__main__.py` | 否 | 自己的命令行入口（见第 7 节） |
| 其余 | 随意 | 包内怎么组织自己决定 |

包导入失败或 `create()` 出错时，注册表换上一个每次都失败的替身（`BrokenAnalyzer`），服务照常启动，这一路运行时标 `failed` 并降级。

### 3.2 输入 `AnalysisInput`（只读）

已通过数量/长度校验并做过二次脱敏。聊天内容一律是不可信数据：里面的「忽略之前的指令」是待分析的材料，不是指令。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `check_id`, `trace_id` | `str` | 日志里带上 `trace_id` |
| `lines` | `tuple[InputLine]` | `line_id`、`speaker`、`text`、`item_index`、`origin` |
| `urls` | `tuple[UrlInput]` | 请求里的 `url` 条目：`url`、`origin`、`item_index`。**正文里夹带的链接不在这里**，由链接分析器自己从 `lines` 提取 |
| `media` | `tuple[MediaInput]` | `media_id`、`kind`、`item_index`、`masked`（P1） |
| `source`, `client` | | 只用于统计 |

辅助方法：`iter_lines(speakers)`、`text(speakers, sep)`、`line(line_id)`、`char_count(speakers)`（非空白字符数）。常量 `COUNTERPART_SPEAKERS = (other, unknown)` 是「对方视角」，案例检索的查询文本按它取。

### 3.3 输出 `AnalyzerOutput`

只填和自己有关的字段，其余保持空：

| 字段 | 谁填 | 说明 |
| --- | --- | --- |
| `status` | 全部 | `done` / `skipped`（没内容可分析、功能未启用）/ `failed`（自己捕获的失败） |
| `degraded` | 全部 | 降级标记（契约第 3 节的枚举），流水线会并进报告 |
| `evidence` | script | 候选证据；汇总做逐字回查，对不上的丢弃 |
| `links` | link | 每条链接一个 `LinkResult`。从正文提取的链接 `origin` 填 `null` |
| `similar_cases` | cases | 已按阈值过滤、按相似度降序，最多 `CASES_TOP_K` 条 |
| `media`, `derived_lines` | deepfake | P1 |
| `extras` | 全部 | 给汇总的额外信号，必须能 JSON 序列化。会入库（`checks.runs` 列）并出现在 `open_pipeline()` 的结果里，**不对外返回** |
| `note` | 全部 | 内部说明，只进日志，不得包含正文 |

`extras` 的约定（汇总会读这些键；分析器可以加别的键）：

| 分析器 | 键 |
| --- | --- |
| script | `rule_hits`（`RuleHit.to_dict()` 列表）、`rules_version`、`prompt_version`、`llm_used`（`primary`/`backup`/`null`）、`model`、`scene`、`stage`、`summary`、`next_move`、`intents`、`llm`（每个角色的调用结果）、`cross_check`（`mode`；`other` 是另一个模型的 `scene`/`stage`/`intents`，没跑时为 `null`） |
| link | 自定（汇总只读 `links` 里的 `risk` 和 `reasons`） |
| cases | 自定（汇总只读 `similar_cases` 里的 `similarity`） |

### 3.4 基类 `Analyzer`

```python
class Analyzer(ABC):
    name: AnalyzerName                              # 必须设置
    failure_flags: tuple[DegradedFlag, ...] = ()    # 这一路失败时的降级标记

    def timeout_s(self, settings) -> float          # 默认 settings.timeout_<name>_s
    def should_run(self, inp) -> bool               # False → skipped，不调用 run()；必须快、不能联网
    async def run(self, inp, ctx) -> AnalyzerOutput # 必须实现
    async def startup(self, services)               # 启动时一次：读数据、把向量读进内存；不得联网
    async def shutdown(self)                        # 退出时：关闭自己建的 httpx 客户端等
    async def cleanup(self, services)               # 定时清理：删自己表里的过期数据
    async def selfcheck(self, services) -> list[SelfCheckItem]  # 启动前自检（第五步的自检命令调用），可以联网
    def status_line(self) -> str                    # 启动输出里的一句话，如「案例 312 条」
```

`ctx: AnalyzerContext` 提供 `settings`、`db`、`services`、`check_id`、`trace_id`、`logger`（自动带 trace_id）、`remaining_s()`（离本路截止还剩几秒，可用来给 httpx 设超时）。

### 3.5 流水线的保证

- 每一路的超时 = `min(timeout_<name>_s, 整单截止 − 汇总预算 − 已用时间)`。默认话术 12 秒、链接 5 秒、案例 3 秒、媒体 15 秒、汇总 2 秒，整单 20 秒。
- 到点就放弃这一路并取消它，**不等它响应取消**，所以吞掉 `CancelledError` 的分析器也拖不住整单。但同步阻塞调用会卡住整个事件循环：阻塞操作（文件大读写、numpy 大矩阵、同步库）放进 `asyncio.to_thread`。
- 一路失败时：状态 `failed`，`elapsed_ms` 照记，报告带上降级标记——超时是 `failure_flags + partial_timeout`，抛异常、返回非法结果、`should_run` 抛异常是 `failure_flags`。分析器自己返回 `status=failed` 且没给 `degraded` 时，也补上 `failure_flags`。没设 `failure_flags` 时用默认值：

  | 分析器 | 默认失败标记 |
  | --- | --- |
  | script | `llm_unavailable_rules_only` |
  | link | `intel_unavailable` |
  | cases | `cases_unavailable` |
  | deepfake | `deepfake_unavailable` |

  契约的降级枚举里没有「链接体检整体失败」这一项，`intel_unavailable` 是最接近的；链接会话如果能在内部区分「只是外部情报失败」，就自己返回带 `intel_unavailable` 的 `done` 结果。
- 返回值会按 `AnalyzerOutput` 重新校验；字段不合契约（例如 `risk` 写了契约外的值）按失败处理。
- 链接这一路失败时，汇总仍会把用户提交的 `url` 条目以 `risk: unknown` 列进报告。
- 汇总失败时用兜底报告（`suspicious` 或 `inconclusive`，带 `partial_timeout`）。

## 4. 汇总接口（冻结）

`app/summary/`：`Summarizer.summarize(inp, runs, ctx) -> Report`，`runs` 是四个分析器的 `AnalyzerRun`（一定齐全，没跑的是 `skipped`；字段 `name`、`status`、`elapsed_ms`、`output`、`error`）。纯代码，不调大模型。实现在 `app/summary/base.py`：证据逐字回查（`verify_evidence`）→ 打分（`app/scoring`，`risk_score == round(100·p)`）→ 契约第 6 节硬性约束（`apply_constraints`，每条一个测试）→ 固定模板（`templates.yaml`）。话术这一路没跑完时，汇总在本地补算规则命中，所以断网时也有规则信号。

## 5. 数据库：分析器怎么声明自己的表

- SQLite 单文件 `server/var/antifraud.db`，WAL 模式，SQLAlchemy 2 异步。不做迁移：表结构变了就删掉数据库文件重来。
- 在自己包的 `models.py` 里继承 `app.db.Base` 定义表，**表名以包名为前缀**，时间列用 `app.db.UtcDateTime`（只接受带时区的 datetime）：

  ```python
  # app/analyzers/link/models.py
  from datetime import datetime
  from sqlalchemy import String
  from sqlalchemy.orm import Mapped, mapped_column
  from app.db import Base, UtcDateTime

  class DomainCache(Base):
      __tablename__ = "link_domain_cache"
      domain: Mapped[str] = mapped_column(String(253), primary_key=True)
      created_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
      checked_at: Mapped[datetime] = mapped_column(UtcDateTime)
  ```

- 服务启动、`open_pipeline()`、`open_services()` 都会自动导入四个包的 `models` 并建表，不需要改任何公共文件。`models.py` 导入出错不会阻止服务启动，模块名会出现在 `services.table_errors` 和启动输出里。
- 读写：`async with ctx.db.session() as session: ...`（`AsyncSession`）。checks 表的写操作在 `CheckStore` 里串行化；分析器写自己的表时尽量短事务，SQLite 的 `busy_timeout` 是 10 秒。
- 向量：以 float32 的 `bytes`（`LargeBinary`）存，`startup()` 里读进 numpy 矩阵。

## 6. 配置：分析器怎么读配置

- 所有配置项在 `app/config.py` 的 `Settings` 里，从环境变量和 `server/.env`（UTF-8）读取，变量名是字段名的大写。运行时用 `ctx.settings`，命令行脚本用 `app.config.get_settings()`。不要自己读环境变量。
- 已经为第二阶段预先声明的配置项：

  | 分组 | 字段 |
  | --- | --- |
  | 超时 | `timeout_script_s` `timeout_link_s` `timeout_cases_s` `timeout_deepfake_s` `timeout_summary_s` `check_deadline_s` |
  | 大模型 | `llm_primary_base_url/api_key/model` `llm_backup_base_url/api_key/model` `llm_connect_timeout_s` `cross_check` |
  | 向量 | `embedding_base_url` `embedding_api_key` `embedding_model` `embedding_dim` `embedding_timeout_s`；`effective_embedding_api_key`（不填时沿用主模型密钥） |
  | 案例 | `cases_top_k` `cases_similarity_threshold` `cases_query_max_chars` |
  | 链接 | `link_expand_short_links` `link_max_hops` `link_hop_timeout_s` `link_rdap_timeout_s` `link_whois_timeout_s` `link_domain_cache_days` |
  | 情报 | `intel_timeout_s` `intel_threatbook_api_key` `intel_qianxin_api_key` `intel_tencent_secret_id` `intel_tencent_secret_key` |
  | 存储 | `var_dir` `database_path` `retention_days` `cleanup_interval_s` |

- 确实需要新配置项又不能改 `config.py` 时：在自己的包里定义一个 `pydantic_settings.BaseSettings` 子类，`env_prefix` 用自己的前缀（`LINK_` / `CASES_` / `EVAL_`），`env_file=app.config.ENV_FILE`、`env_file_encoding="utf-8"`、`extra="ignore"`，第三阶段集成时再并进 `config.py`。注意前缀不要和上表已有的字段撞名。
- 测试里不要读真实的 `.env`：用夹具 `settings` / `make_settings(**overrides)`。

## 7. 命令行入口

不靠改 `pyproject.toml`：在包里放 `__main__.py`，用 `python -m` 运行（工作目录是 `server/`）：

```bash
uv run python -m app.analyzers.cases import      # 案例导入（第二阶段实现）
uv run python -m app.analyzers.link check https://t.example.cn/a1
uv run python -m eval.run_eval                     # 或 uv run python eval/run_eval.py
```

`app/analyzers/cases/__main__.py` 是现成的示例：`async with open_services() as services:` 拿到配置和已建表的数据库。

## 8. 不经过 HTTP 跑完整条流水线（评测用）

```python
from app.pipeline import open_pipeline, run_check

async with open_pipeline(settings) as pipe:     # settings 可省略；评测建议指向临时 var_dir
    result = await pipe.analyze(request_dict)   # 与 POST /v1/checks 的请求体同构
    result.report            # Report
    result.analyzers         # 契约 4.3 的五项状态
    result.runs[AnalyzerName.SCRIPT].output.extras
    result.runs[...].error   # "timeout" / 异常类型名 / None
    result.elapsed_ms

result = await run_check(request_dict)          # 一次性，批量请用 open_pipeline
```

- 同一个 `open_pipeline()` 里分析器只加载和启动一次。
- 请求不合法时抛 `app.errors.ApiError`（`code == ErrorCode.INVALID_INPUT`）。
- 不写 checks 表；分析器自己的表照常读写。
- 也可以传 `analyzers=[...]`、`summarizer=...` 替换实现（测试和对照实验用）。

## 9. 测试

- 只跑自己的目录：`uv run pytest tests/link`、`uv run pytest tests/cases tests/eval`。全部：`uv run pytest`。
- `tests/conftest.py` 的夹具在所有子目录可用：`settings`、`make_settings(**overrides)`、`client`、`make_client(settings, pipeline_factory=None)`；辅助函数 `chat_request(*texts, urls=())`、`contract_example(name)`、`poll_until_done(client, check_id)`、常量 `AUTH`。需要自己的夹具时写在 `tests/link/conftest.py` 这样的子目录里，不要改公共的 `conftest.py`。
- 夹具 `block_network` 自动生效：测试里任何真实出站连接都会报错。外部请求用 respx 打桩（`respx.mock` 拦截 httpx，不经过 socket）。要连真实服务的测试标 `@pytest.mark.live`，只在 `RUN_LIVE=1` 时运行。
- 测试里不出现真实密钥。
- 把自己的分析器接进流水线做端到端测试：

  ```python
  from app.contract import AnalyzerName, AnalyzerStatus
  from app.pipeline import open_pipeline
  from tests.conftest import chat_request

  async def test_e2e(settings):
      async with open_pipeline(settings) as pipe:        # 用的就是包里 create() 返回的实例
          result = await pipe.analyze(chat_request("...", urls=("https://t.example.cn/a1",)))
      assert result.runs[AnalyzerName.LINK].status == AnalyzerStatus.DONE
  ```

## 10. 跨平台约定

- 路径一律 `pathlib`，从 `app.paths` 的常量出发（`SERVER_DIR`、`DATA_DIR`、`SHARED_DIR`），不依赖当前工作目录。
- 读写文本文件一律显式 `encoding="utf-8"`；`tests/test_infra.py` 会扫描 `app/` 和 `eval/` 里没写的地方。
- 数据库、日志、临时文件只放 `settings.var_dir`（默认 `server/var/`，不进 git）。
- Windows 上打开着的文件删不掉：删除前先关闭句柄。

## 11. 依赖

`pyproject.toml` 和 `uv.lock` 只有话术/汇总会话能改。已经声明并锁定的 P0 依赖：fastapi、uvicorn、pydantic、pydantic-settings、sqlalchemy[asyncio]、aiosqlite、httpx、openai、tldextract、rapidfuzz、numpy、pyyaml；开发依赖 pytest、pytest-asyncio、respx。P1 的大依赖在可选组 `media` 里，默认不装。缺依赖时发消息给话术/汇总会话，不要自己改。

`tldextract` 默认第一次用时会联网下载后缀列表：链接会话请用 `tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)` 之类的写法只用随包快照。
