# 离线评测（eval/）

不经过 HTTP，直接用 `app.pipeline.open_pipeline()` 把一批带标签的对话跑完整条流水线，统计效果和时延。

> **注意：** `eval/data/synthetic.jsonl` 里的 12 条是自己编的合成样例，只用来证明脚本能从头跑到尾。在它上面跑出来的任何数字都**不是**系统效果，不要写进文档、报告或答辩材料。

## 怎么跑

在 `server/` 目录下：

```bash
uv run python -m eval.run_eval
```

```bash
uv run python -m eval.run_eval eval/data/team_v1.jsonl 另一个目录/
```

```bash
uv run python -m eval.run_eval --cross-check config
```

```bash
uv run python -m eval.run_eval --isolated
```

| 参数 | 含义 |
|---|---|
| `paths` | 评测数据文件或目录（目录取其中的 `*.jsonl`）。缺省为 `eval/data/*.jsonl` |
| `--cross-check both` | 默认。`CROSS_CHECK=off` 和 `CROSS_CHECK=parallel` 各跑一遍，最后打印对照表 |
| `--cross-check off` / `parallel` | 只跑一种 |
| `--cross-check config` | 只按 `.env` 里的 `CROSS_CHECK` 跑一遍 |
| `--isolated` | 用临时目录当数据库（跑完删除），案例库为空 |

没有配置任何密钥也能跑：大模型、向量接口都不可用时，流水线按降级规则走，脚本照常统计，但会醒目提示（见下文「必须看的提醒」）。

### 用哪个数据库

- **默认**：用 `.env` 配置的数据库（`<VAR_DIR>/antifraud.db` 或 `DB_PATH`），也就是服务正在用的那个。这样用 `python -m app.analyzers.cases import` 导入过的相似案例会参与评测。
  评测走 `open_pipeline()`，不写任务表，也不改案例表；库文件不存在时会按正常启动流程建库建表。
- **`--isolated`**：临时数据库，案例库为空，相似案例这一路会是 `skipped`。适合只看话术/链接/汇总层，或者不想往正式库里写东西的时候。

### 结果文件

完整结果（每种设置的汇总 + 每条样本的分数、结论、降级标记、各分析器状态和耗时）写到 `<VAR_DIR>/eval/eval-YYYYmmdd-HHMMSS.json`，不进 git。即使用了 `--isolated`，结果文件也写到配置的 `VAR_DIR` 下面。

## 数据格式

UTF-8 编码的 JSON Lines（带不带 BOM 都行），每行一条对话。空行和以 `//` 开头的行跳过。

```json
{"id": "team_0001", "label": "fraud", "scene": "brushing_rebate", "source": "team_v1", "lines": [{"speaker": "other", "text": "先垫付200返260"}, {"speaker": "self", "text": "好的"}], "urls": ["https://example.cn/x"], "note": "可选备注"}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 全部数据文件里唯一 |
| `label` | 是 | `fraud`（诈骗，正类）或 `normal` |
| `source` | 是 | 数据来源，按它分组统计，如 `synthetic`、`team_v1`、`tft5` |
| `scene` | 否 | 契约里的场景枚举。缺省：`normal` 记 `none`，`fraud` 记 `other`。目前只记录，不参与指标 |
| `lines` | 二选一 | 对话行。每项可以是字符串（说话人记 `unknown`，相当于粘贴的整段文字），也可以是 `{"speaker": "other" / "self" / "unknown", "text": "..."}` |
| `urls` | 二选一 | 单独提交的网址（`origin` 记 `typed`） |
| `note` | 否 | 备注，脚本忽略 |

`lines` 和 `urls` 不能都为空，不认识的字段报错。每条样本转成和 `POST /v1/checks` 一样的请求体：对话放进一个 `type: chat, origin: pasted_text` 的条目，`line_id` 为 `0-0`、`0-1`……

不合格的行会打印「文件名:行号 原因」并跳过，其余照跑。请求本身不合法（例如单行超过契约长度上限）的样本记在结果的 `invalid` 里，不计入指标。

## 统计什么

约定：风险概率 **p = `report.risk_score` / 100**；p ≥ 阈值判为诈骗；正类是 `label = fraud`。

每种 `CROSS_CHECK` 设置分别输出：

- **混淆矩阵**：阈值 0.05、0.1、0.2 … 0.9 各一行，含 TP/FP/TN/FN、召回、误报率、精确率。
- **固定误报率下的召回**：误报率 ≤ 1% / 5% / 10% 时能达到的最高召回，以及对应阈值（满足条件的最低阈值）。没有可行阈值时显示「无可行阈值」，召回按一条都不判计算。
- **三支结论分布**：按契约阈值 0.60 / 0.05 把 p 分成 high_risk / suspicious / low_risk，分别统计诈骗样本和正常样本。这一项只看阈值；报告里真实的 `verdict` 还受硬性约束影响，另列一行。
- **按数据来源分组**：每个 `source` 的样本数、阈值 0.60 下的召回和误报率、三支分布。
- **时延**：整单 `elapsed_ms` 的 P50 / P90 / P95 / 最大值，以及每个分析器的 P50 / P95 和状态分布（done / skipped / failed / timeout）。样本按顺序一条一条跑，时延才有参考意义。
- **降级标记计数**：每种 `degraded` 标记出现的次数。

跑了两种设置时，最后打印对照表：召回@0.60、误报率@0.60、各固定误报率下的召回、时延 P50/P95、`cross_check_disagreed` 次数、只用规则的样本数。

### 必须看的提醒

输出里以「！！！」开头的行：

- **N/M 条带 `llm_unavailable_rules_only`**：这些样本大模型没参与，只用了规则。数字只代表规则层，不能当作整个系统的效果。如果对照表里所有设置都是全部只用规则，会额外提示这个对照不能用来决定开不开交叉复核。
- **N/M 条报告来自占位实现**：报告 headline 以「【占位】」开头，说明汇总还是占位版本，指标没有意义。
- **相似案例这一路一条都没有参与**：案例库为空、没有向量密钥或接口不可用。

## 合成样例

`eval/data/synthetic.jsonl`：6 条诈骗（刷单返利 2、投资理财 1、冒充客服 1、冒充熟人 2）、6 条正常（面试邀约、聊基金、家人转账、约爬山、快递、只有「在吗」）。全部是为了测试脚本手写的，`source` 为 `synthetic`。不代表真实分布，数量也远远不够得出任何结论。

真实评测请另建文件（例如 `eval/data/team_v1.jsonl`），`source` 写清楚来源，按团队的数据使用规定决定能不能提交进仓库。

## 关于 Telecom_Fraud_Texts_5（ChangMianRen/Telecom_Fraud_Texts_5）

- **没有下载**，也不要下载进仓库：许可只限科研用途。
- 写脚本时无法访问 Hugging Face，**没能确认它的字段和标签格式**，所以没有写转换器。
- 需要用的话：在仓库外下载，自己确认字段后，写一个一次性脚本转成上面的 jsonl 格式（`source` 写 `tft5`，诈骗类记 `fraud`，正常类记 `normal`），结果文件也放在仓库外，跑评测时把路径传给 `run_eval`。
- 按公开描述它是通用电信诈骗文本，可能没有本项目重点的刷单返利、投资理财细分类别，按 `source` 分组看结果时要考虑这一点。以上描述未经核实。

## 测试

```bash
uv run pytest tests/eval -q
```

`tests/eval/test_metrics.py` 用手算的小例子核对混淆矩阵、固定误报率召回、三支划分和分位数；`tests/eval/test_run_eval.py` 通过 `evaluate(..., analyzers=..., summarizer=...)` 注入假的分析器和汇总，从头跑到尾核对各项统计，并检查数据格式校验、结果文件位置和命令行入口。
