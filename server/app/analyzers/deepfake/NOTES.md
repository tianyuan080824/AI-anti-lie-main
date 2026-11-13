# 媒体与深伪 `deepfake` 实现说明

本包自带上传路由、预处理入口和分析器，已经接进主流程（见文末清单和 `docs/server-progress.md` 的「媒体接进主流程」一节）。

## 做了什么

- `POST /v1/media`（`router.py`）：`multipart/form-data`，字段 `file` + `kind`。按内容认格式（图片走 Pillow，音频和视频走 ffprobe，协议白名单只放 `file`），不看扩展名和 Content-Type。随机文件名存在 `var/media/`，无扩展名；先写 `.part` 再改名。超大小或超时长返回 413 `PAYLOAD_TOO_LARGE`，认不出格式返回 415 `UNSUPPORTED_MEDIA`，缺字段或空文件返回 400 `INVALID_INPUT`。
- 图片：`rapidocr` 识别，按行拼成 `derived_lines`（`line_id` 形如 `d2-0`，`speaker=unknown`），出包前过 `app/redact.py`。
- 语音：ffmpeg 转 16 kHz 单声道。SenseVoice（FunASR，本机 CPU）转写成派生行；AASIST-L（`clovaai/aasist`，MIT）按 4 秒分段打分再聚合。
- 视频：只抽音轨走语音那条路。说明里固定写「画面是否换脸没有检测」。换脸检测没做。
- 原文件在分析读进内存后立即删除；认领是一次性的，过期行和孤儿文件由 `cleanup()` 清掉。有效期 = min(`MEDIA_MAX_AGE_S`, 3600) − 2 × 清理间隔，最短 60 秒，所以 1 小时内一定删掉。
- 缺 `media` 扩展、缺权重、缺 ffmpeg 时服务照常启动，鉴定里的媒体条目返回 `not_analyzed` 并带 `deepfake_unavailable`；上传接口返回 503 `UNAVAILABLE`。没装 `python-multipart` 时不注册表单路由，避免 FastAPI 启动报错。

## 两个入口

四个分析器是并行跑的，派生行到不了另外三路，除非流水线先跑一遍预处理。所以有两个入口：

1. `preprocess.prepare(inp, services, engines, ms)`：在 `MEDIA_PREPROCESS_BUDGET_S`（默认 6 秒）内取文件、转码、OCR / 转写、打分。返回补了派生行的输入和 `Prepared`。不抛异常（`CancelledError` 除外）；超时的条目记 `not_analyzed` + `deepfake_unavailable`。
2. `DeepfakeAnalyzer.run()`：优先取预处理阶段按 `check_id` 存下的结果，只做合成打分和回传派生行；没有预处理结果时自己分析。

## 阈值和局限

公开权重在 ASVspoof 2019 LA 上训练，对压缩过的中文语音消息不可靠。所以：

- 平均分 ≥ 0.9 且至少一半片段 ≥ 0.5 才给 `likely_synthetic`；平均分 ≤ 0.1 且没有片段 ≥ 0.5 才给 `likely_genuine`；其余一律 `uncertain`。有效语音不足 1 秒不打分。
- 每条 `note` 都带「仅供参考」。
- 深伪对风险概率的贡献在 `app/scoring` 里封顶（`deepfake.cap = 0.8`），单独不足以给 `high_risk`（`app/summary/base.py` 另有硬性约束）。
- 建议文案已经在 `app/summary/templates.yaml` 的 `deepfake` 场景里写了「挂断后用你原来存的号码回拨」。
- 没有测过准确率，本文档不写检测准确率。

## 怎么验证的

- `uv run pytest -q tests/deepfake`：25 通过（2026-11-12）。覆盖大小 / 时长 / 格式校验、伪造扩展名、派生行格式和脱敏、缺依赖降级、分析后删原文件、过期清理、关闭开关时上传返回 `UNAVAILABLE`、拿不准时给 `uncertain`。模型推理全部打桩，默认测试不联网、不加载大依赖。
- 真实跑了一次（本机 CPU，`server/.venv`，合成输入，没有用真实录音或截图）：
  - OCR：Pillow 生成的 640×160 白底黑字图，识别 1.236 秒，1 个文本框、12 个字。
  - 转写：`say` 合成 4.52 秒中文，SenseVoice 加载加预热 23.210 秒，转写 0.972 秒，19 个字。
  - AASIST：同一段音频切出 1 个片段，加载 0.033 秒，打分 0.707 秒。`say` 的输出被打成合成分 1.000，这只说明合成语音能被打高分，不能当作准确率。
- 全量 `uv run pytest -q` 还没在这次改动后重跑，见文末。

## 没有做、没有验证的

- 视频换脸检测没做。DeepfakeBench 权重是 CC BY-NC 4.0，要做的话只能非商用，并在作品文档里注明出处（https://github.com/SCLBD/DeepfakeBench）。
- 没有在 Windows 上跑过（文件句柄、ffmpeg 路径、`say` 不存在）。
- 没有用团队自己录的样本测过，所以不下任何准确率结论。
- 预处理预算默认 6 秒。实测转写本身 0.97 秒，但模型第一次加载要 23 秒：必须在 `startup()` 里预加载，不能等第一次请求。真实服务里实测语音转写加载 15–17 秒，加载期间来的语音跳过转写，见 `docs/server-progress.md`。
- `MOCK_MODE=1` 时按建议做了：`POST /v1/media` 照样校验并返回 `media_id`，鉴定结果仍是模拟报告。

## 权重

清单在 `data/deepfake/weights.json`，文件放 `server/models/`（已在 `.gitignore`）。

```bash
uv run python -m app.analyzers.deepfake download   # 按清单下载并校验 SHA-256
uv run python -m app.analyzers.deepfake verify     # 只校验已放好的文件
uv run python -m app.analyzers.deepfake check      # 依赖、ffmpeg、权重
uv run python -m app.analyzers.deepfake sweep      # 清理过期媒体
```

下载失败会打印来源地址、目标路径、大小和 SHA-256，按提示手动放置后再 `verify`。

- AASIST-L：MIT，Copyright (c) 2021-present NAVER Corp.，来自 https://github.com/clovaai/aasist （commit a04c9863），约 0.4 MB。
- SenseVoiceSmall：来自 https://www.modelscope.cn/models/iic/SenseVoiceSmall ，许可证以模型页为准（清单里记的是 Apache-2.0）。

各库的缓存目录在加载前被指到 `MEDIA_MODELS_DIR` 下，不会写 `~/.cache`。

## 环境变量（前缀 `MEDIA_`，本包自己读，没有加进 `app/config.py`）

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MEDIA_ENABLED` | 1 | 总开关。关掉后上传返回 503，鉴定里的媒体一律 `not_analyzed` |
| `MEDIA_PRELOAD` | 1 | 启动时后台加载模型 |
| `MEDIA_MODELS_DIR` | `server/models` | 权重和缓存目录 |
| `MEDIA_MANIFEST_PATH` | `data/deepfake/weights.json` | 权重清单 |
| `MEDIA_PREPROCESS_BUDGET_S` | 6 | 预处理时间预算 |
| `MEDIA_THREADS` | 4 | torch / onnxruntime 线程数 |
| `MEDIA_PROBE_TIMEOUT_S` | 10 | 上传时 ffprobe / ffmpeg 超时 |
| `MEDIA_OCR_MIN_SCORE` | 0.5 | OCR 行置信度下限 |
| `MEDIA_ASR_CHUNK_S` | 25 | 长音频按静音切段的上限 |
| `MEDIA_SYNTHETIC_THRESHOLD` | 0.9 | 判 `likely_synthetic` 的平均分 |
| `MEDIA_GENUINE_THRESHOLD` | 0.1 | 判 `likely_genuine` 的平均分 |
| `MEDIA_MIN_SPEECH_S` | 1 | 短于它不打分 |
| `MEDIA_SWEEP_INTERVAL_S` | 60 | 清理间隔 |
| `MEDIA_PREPARED_TTL_S` | 120 | 预处理结果等 `run()` 来取的最长时间 |
| `MEDIA_MAX_DERIVED_LINES` | 100 | 一次鉴定最多产出多少条派生行 |

沿用公共配置、不用重复定义：`LIMIT_IMAGE_BYTES`（5 MB）、`LIMIT_AUDIO_BYTES`（10 MB）/`LIMIT_AUDIO_SECONDS`（60）、`LIMIT_VIDEO_BYTES`（50 MB）/`LIMIT_VIDEO_SECONDS`（30）、`FFMPEG_PATH`、`MEDIA_MAX_AGE_S`、`TIMEOUT_DEEPFAKE_S`。`MEDIA_MAX_AGE_S` 已经是公共字段，本包没有再定义 `max_age_s`。

## 集成清单（已全部完成，2026-11-12）

做法和清单有两处不同：预处理通过分析器接口的 `prepare()` 钩子接入（`runner.py` 不直接导入本包）；`Prepared` 由 `DeepfakeAnalyzer.prepare()` 自己存。权重清单里语音转写的键是 `asr`（`runtime.py` 原来按 `sensevoice` 找，已修正）。

1. `app/main.py`：`from app.analyzers.deepfake.router import router as media_router`，再 `app.include_router(media_router)`。表不用手动注册，`Database.create_all()` 会导入 `app.analyzers.deepfake.models`。
2. `app/api/middleware.py`：所有 `/v1/` 的 POST 都按 `limit_body_bytes`（1 MB）卡请求体，媒体上传（最大 50 MB）会全部被拒。需要把 `/v1/media` 排除，改用各类型自己的上限。
3. `app/pipeline/runner.py` 的 `run()`：在「并行运行」之前调用 `preprocess.prepare`，把返回的输入传给四路分析器，并把 `Prepared` 放进 `DeepfakeAnalyzer.store`（按 `check_id`）。预算用 `MEDIA_PREPROCESS_BUDGET_S`，不要占掉话术那 12 秒。`runs` 里已经会收集 `derived_lines`，这步不用动。
4. `.env.example`：加上面这张表。`README.md` 补安装（`uv sync --extra media`、ffmpeg）和权重命令。
5. `docs/android-handoff.md` 现在写着 `POST /v1/media` 未实现、返回 404。接上之后改成：201 返回 `media_id` / `kind` / `size_bytes` / `expires_at`；413 / 415 / 400 / 503 的含义见契约第 7 节；视频结果的 `note` 会说明没有检测换脸。
6. 自检不用额外接线：`uv run python -m app.selfcheck` 会调到 `DeepfakeAnalyzer.selfcheck()`，它已经检查依赖、ffmpeg 和两份权重。
