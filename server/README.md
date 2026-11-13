# 一键鉴诈服务

「一键鉴诈」的后端：FastAPI + SQLite，运行在笔记本上，手机通过 `adb reverse`（或同一热点）访问。对外接口见 `../docs/api-contract.md`，内部结构和模块接口见 [ARCHITECTURE.md](ARCHITECTURE.md)，进度和验证记录见 `../docs/server-progress.md`。

没有配置大模型密钥、或笔记本断网时，服务照样出报告：只用规则、链接的词法特征和本地黑名单，结论最高 `suspicious`（链接命中黑名单除外），并带降级标记。

下面所有命令在 Mac（终端）和 Windows（PowerShell）上都一样，都在 `server/` 目录下执行。

## 1. 安装

### 需要什么

- Python 3.12（必须是 3.12.x，3.13 不行）
- [uv](https://docs.astral.sh/uv/) 0.12 或更新
- 不需要任何密钥就能启动和跑测试

依赖的下载缓存放在 `server/.uv-cache/`，虚拟环境在 `server/.venv/`，两者都不进 git。默认包索引是清华镜像（`pyproject.toml` 的 `[[tool.uv.index]]`）；uv 不会自己下载 Python（`python-downloads = "never"`），所以要先装好 Python 3.12。

### Mac

团队的 Mac 上用 Homebrew 的 Python 3.12 和 uv。没有的话：

```bash
brew install python@3.12 uv
```

不要用 conda 自带的 `python3`。

### Windows

1. 装 Python 3.12：从 python.org 下载 Python 3.12.x 的「Windows installer (64-bit)」，安装时勾选「Add python.exe to PATH」。装完在新开的 PowerShell 里确认：

   ```powershell
   py -3.12 --version
   ```

2. 装 uv（任选一种）：

   ```powershell
   winget install --id=astral-sh.uv -e
   ```

   或者用刚装好的 Python：

   ```powershell
   py -3.12 -m pip install --user uv
   ```

   装完关掉 PowerShell 再开一个，确认 `uv --version` 能用（pip 方式装的如果找不到，用 `py -3.12 -m uv` 代替下文的 `uv`）。

3. 中文 Windows 不需要改系统编码：代码里所有文本读写都显式用 UTF-8，控制台输出会强制切到 UTF-8。

### 装依赖

```bash
cd "AI anti lie/server"
```

```bash
uv sync
```

如果 uv 报找不到 Python 3.12，显式指定解释器（Mac 上是 `/opt/homebrew/bin/python3.12`；Windows 上用 `py -3.12 -c "import sys; print(sys.executable)"` 查路径）：

```bash
uv sync --python /opt/homebrew/bin/python3.12
```

这只装 P0 依赖和开发依赖。要分析图片、语音和视频（P1），见下一节。

### 媒体分析（可选，P1）

不装也能跑：上传接口返回 503 `UNAVAILABLE`，鉴定里的媒体条目是 `not_analyzed`，报告带 `deepfake_unavailable`，文字、链接照常分析。要用时做三件事：

1. 装可选依赖组（OCR、语音转写、合成语音检测，体积较大）。以后再执行 `uv sync` 时也要带上 `--extra media`，否则这些包会被卸掉：

   ```bash
   uv sync --frozen --extra media
   ```

2. 装 ffmpeg（解码音频和视频要用，同目录下要有 ffprobe）。Mac 上 `brew install ffmpeg`；Windows 上下载 ffmpeg 的发行包解压，把 `bin` 目录加进 PATH，或者在 `.env` 里写 `FFMPEG_PATH=` 指向 `ffmpeg.exe`。

3. 下载并校验模型权重（放在 `server/models/`，不进 git，约 900 MB）：

   ```bash
   uv run --frozen python -m app.analyzers.deepfake download
   ```

   ```bash
   uv run --frozen python -m app.analyzers.deepfake verify
   ```

   下载失败时会打印来源地址、目标路径和 SHA-256，手动放好后再 `verify`。最后检查一遍依赖、ffmpeg 和权重：

   ```bash
   uv run --frozen python -m app.analyzers.deepfake check
   ```

   四项都是「通过」就行（媒体依赖、ffmpeg、语音转写权重、合成检测权重）。启动前自检（第 2 节）也会检查这几项。

启动后模型在后台加载（`MEDIA_PRELOAD=1`，默认）：本机实测文字识别约 2 秒、合成检测约 3 秒、语音转写约 15–17 秒。加载期间服务照常响应；这时来的语音会跳过转写（只做合成检测，报告带 `partial_timeout`），所以路演时启动后等半分钟再演示语音。图片只做文字识别，不做合成检测。视频只分析音轨，画面换脸没有检测。媒体的配置项见 `.env.example` 的「媒体」一段。

### 配置

复制 `.env.example` 为 `.env`（`.env` 不进 git），填上大模型和向量接口的密钥、`APP_TOKEN`：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

`.env` 用 UTF-8 保存（记事本「另存为」里选 UTF-8）。

## 2. 启动前自检

上台前、换了网络或改了 `.env` 之后跑一遍：

```bash
uv run python -m app.selfcheck
```

逐项打印「通过 / 失败 / 未配置 / 提示」，不通过的项下面一行是「怎么办」。检查的项目：

| 项目 | 内容 |
| --- | --- |
| 配置文件 | `server/.env` 是否存在 |
| 模拟模式 | 必须关闭（路演时不要开模拟模式） |
| 访问令牌 | 还是默认的 `dev-token` 时提示 |
| 数据库 | 实际写入、删除一条记录 |
| 规则、话术提示词 | 规则条数和版本 |
| 主模型、备用模型 | 各发一次最小请求；没填密钥显示「未配置」（算没通过，不会显示成通过） |
| 链接体检数据、域名注册时间查询 | 黑名单等数据文件；RDAP 查一次 `example.com` |
| 案例库、向量接口 | 案例条数；向量接口调用一次 |
| ffmpeg（P1 语音） | 找不到只「提示」，不影响结果 |
| 监听地址 | 监听所有网卡时列出局域网地址并提醒放行防火墙；`--host ::` 只监听 IPv6，会提示改用 `0.0.0.0` |

数据库打不开、或 `.env` 里有写错的值（比如 `PORT=abc`）时，自检不会崩，会指出是哪一项、该怎么改。

全部通过（「提示」不算）时最后一行是「全部通过，可以启动服务」，退出码 0；否则列出没通过的项，退出码 1。大模型或向量接口没通过时服务照样能启动，只是报告只用规则、最高 `suspicious`。

主备模型的结果会写进数据库，服务启动时读回来作为 `/v1/health` 里 `llm` 字段的初始值（12 小时内有效；服务运行中有真实调用后以真实调用为准，服务退出时会把真实调用的结果存下来，下次启动接着用）。所以**先跑自检、再启动服务**。

## 3. 启动

真实模式（默认）：

```bash
uv run python -m app
```

模拟模式（只给 Android 联调用，返回写死的契约示例报告，不调用任何外部服务；路演时不要开）：

```bash
uv run python -m app --mock
```

可选参数 `--host`、`--port`（也可以在 `.env` 里设 `HOST`、`PORT`）。Mac 上也可以写 `MOCK_MODE=1 uv run python -m app`，PowerShell 不支持这种写法，用 `--mock`。

启动时先打印一段状态：监听地址、是否模拟模式、访问令牌是否为默认值、规则数量、主备模型和密钥是否配置、数据库位置、各分析器状态（案例条数等）。监听所有网卡（`--host 0.0.0.0`）时还会打印本机局域网地址，并提醒在防火墙里放行端口。按 Ctrl+C 停止。

默认只监听 `127.0.0.1:8000`，配合 `adb reverse` 使用；访问令牌默认是 `dev-token`（只用于开发，改 `.env` 里的 `APP_TOKEN`，App 里填同一个值）。

运行产生的数据在 `server/var/`（不进 git）：

- `var/antifraud.db`：SQLite。鉴定结果保留 7 天（`RETENTION_DAYS`），每小时清理一次（`CLEANUP_INTERVAL_S`），启动时也清理一次。
- `var/logs/server.log`：JSON 行格式日志，每个请求带 `trace_id`，不含聊天正文和密钥。

服务重启时，上次没跑完的鉴定会被标记为失败（`INTERNAL`，可重试）。

## 4. 路演当天的顺序

1. **接电源**。
2. **关闭自动睡眠**：Mac 在「系统设置 → 电池 / 锁定屏幕」里把「接通电源时防止自动睡眠」打开、关显示器的时间调长；Windows 在「设置 → 系统 → 电源」里把「接通电源时进入睡眠」设成「从不」。
3. **连热点**：笔记本连上手机热点（或现场网络），确认能上网。
4. **插数据线**：手机用数据线连笔记本，手机上打开「USB 调试」并允许这台电脑。
5. **端口转发**：

   ```bash
   adb reverse tcp:8000 tcp:8000
   ```

   用 `adb reverse --list` 确认有 `tcp:8000 tcp:8000` 这一行。
6. **跑自检**：`uv run python -m app.selfcheck`，处理掉没通过的项（大模型连不上时见下面「常见故障」）。
7. **启动服务**：`uv run python -m app`，看状态里「模拟模式 关闭」、主备模型「密钥已配置」。
8. 手机上打开 App 发起一次鉴定，确认能拿到报告。

**重新插拔数据线之后要不要重新执行 `adb reverse`**：**未验证**。写这份说明的 Mac 上没有 adb，也没有连手机，没能实际试。按 adb 的工作方式，端口转发挂在这次 USB 连接上，拔线、手机重启或 `adb kill-server` 后通常会丢失。在没试过之前，**每次重新插线后都重新执行一次 `adb reverse tcp:8000 tcp:8000`**（重复执行没有副作用），再用 `adb reverse --list` 确认。请队员实测后把结论补在这里。

## 5. 常见故障怎么判断

### 手机连不上服务

按顺序排查：

1. 笔记本上服务在不在：浏览器打开 `http://127.0.0.1:8000/v1/health`，返回 401 的 JSON 说明服务在跑（健康检查也要令牌）。带令牌的完整检查：

   ```bash
   curl -s http://127.0.0.1:8000/v1/health -H "Authorization: Bearer dev-token"
   ```

2. 数据线方式：`adb devices` 里手机是 `device`（不是 `unauthorized`，那是手机上没点允许）；`adb reverse --list` 里有 `tcp:8000`。没有就重新执行 `adb reverse tcp:8000 tcp:8000`。
3. 手机浏览器打开 `http://127.0.0.1:8000/v1/health`：能看到 401 的 JSON 说明网络通了，问题在 App 的地址或令牌；打不开说明转发没生效。
4. 热点方式：服务要用 `--host 0.0.0.0` 启动，App 里填启动输出里的局域网地址；Windows 第一次启动时防火墙弹窗要选「允许」并勾选「专用网络」；有的手机热点开了「AP 隔离」，设备之间互相访问不了，换数据线方式。
5. App 收到 401：App 里的令牌和 `.env` 里的 `APP_TOKEN` 不一致。

### 大模型调不通

跑 `uv run python -m app.selfcheck` 看主模型、备用模型那两行：

| 显示 | 原因和处理 |
| --- | --- |
| 未配置 | `.env` 里没填 `LLM_PRIMARY_API_KEY` / `LLM_BACKUP_API_KEY` |
| 失败（connect / timeout） | 笔记本没联网或热点没流量；`*_BASE_URL` 写错 |
| 失败（status_401 / status_403） | 密钥不对、过期，或账号没开通这个型号 |
| 失败（status_404 / status_400） | 型号名或接口地址不对，以控制台为准核对 `*_MODEL`、`*_BASE_URL` |
| 失败（status_429） | 被限流或余额不足 |

只要有一个模型能调通，报告就能用上大模型；两个都不通时服务照常出报告，只用规则。

### 报告一直是降级的

看报告里的 `degraded`：

| 标记 | 含义 | 先查什么 |
| --- | --- | --- |
| `llm_unavailable_rules_only` | 话术那一路没用上大模型 | 上面「大模型调不通」；也可能是剩余时间不够 |
| `backup_llm_used` | 主模型失败，用了备用 | 主模型的密钥、余额、网络 |
| `cases_unavailable` | 案例库有数据但向量接口调不通 | 自检的「向量接口」一项；`EMBEDDING_API_KEY` |
| `intel_unavailable` | 外部情报没查成：配置了的情报接口调用失败、到点还没轮到查，或链接那一路整个失败。没配置情报密钥时不会因为情报出现这个标记 | 自检的「外部情报」各项；`.env` 里的 `INTEL_*` 密钥；网络；`var/logs/server.log` 里的 `link.*` 记录 |
| `partial_timeout` | 有一路超时 | 网络慢；日志里各路的 `elapsed_ms` |
| `deepfake_unavailable` | 媒体没分析成：没装 `media` 依赖组、缺 ffmpeg 或权重、媒体已过期或不存在、文件解不开，或者预处理超时（`MEDIA_PREPROCESS_BUDGET_S`，默认 6 秒） | `uv run python -m app.analyzers.deepfake check`；日志里的 `media.*` 和 `analyzer.prepared` 记录 |

`/v1/health` 的 `llm` 是 `down` 也说明最近一次调用或自检失败了。案例库是空的时案例那一路是 `skipped`，不算降级；需要时导入案例：

```bash
uv run python -m app.analyzers.cases import
```

日志在 `var/logs/server.log`，按报告的 `trace_id` 搜索能看到这次鉴定每一路的耗时和失败类别。

## 6. 断网时会怎样

笔记本断网时大模型、向量接口、域名注册时间查询、短链展开都会失败，服务仍然在时限内出报告：

- 整单硬上限 20 秒（`CHECK_DEADLINE_S`）；各路独立超时：话术 12 秒、链接 5 秒、案例 3 秒、媒体 15 秒（`TIMEOUT_*_S`）。
- 连接超时比读取超时短：大模型最多 3 秒、向量接口最多 1.25 秒、链接每跳 1.5 秒，连不上尽快放弃。
- 某一路失败只把那一路标成 `failed` 并写进 `degraded`，报告照常产出。
- 结论封顶 `suspicious` 只在**主备两个大模型都用不上**时发生（带 `llm_unavailable_rules_only`），有链接命中黑名单时例外。只是链接、案例或媒体那一路失败时不封顶，结论照常按其余各路打分。

断网的实测（模拟）记录见 `../docs/server-progress.md` 第 5 步一节。**那次是模拟**：没有真的断开这台机器的网络，而是用下面的配置把所有外部地址指到本机连不上的端口。真实断网请队员在路演前实测一次。

模拟断网（只用于测试，平时不要设；密钥写成任意假值，让请求真的发出去）：

```text
LLM_PRIMARY_API_KEY=sk-test-not-a-real-key
LLM_BACKUP_API_KEY=sk-test-not-a-real-key
EMBEDDING_API_KEY=sk-test-not-a-real-key
LLM_PRIMARY_BASE_URL=http://127.0.0.1:9/v1
LLM_BACKUP_BASE_URL=http://127.0.0.1:9/v1
EMBEDDING_BASE_URL=http://127.0.0.1:9/v1
LINK_RDAP_BASE_URL=http://127.0.0.1:9/rdap
LINK_WHOIS_SERVER=127.0.0.1
```

短链展开和域名解析仍然走系统网络，模拟不覆盖它们。

## 7. 冒烟测试（curl）

另开一个终端，服务启动后：

```bash
curl -s http://127.0.0.1:8000/v1/health -H "Authorization: Bearer dev-token"
```

```bash
curl -s -X POST http://127.0.0.1:8000/v1/checks -H "Authorization: Bearer dev-token" -H "X-Device-Id: 0f8fad5b-d9cb-469f-a165-70867728950e" -H "Content-Type: application/json" --data @../shared/contract-examples/check-request.json
```

返回 `202` 和 `check_id`，然后轮询（把 `c_...` 换成上一步的值）：

```bash
curl -s http://127.0.0.1:8000/v1/checks/c_... -H "Authorization: Bearer dev-token"
```

`status` 变成 `succeeded` 后响应里带完整报告。Windows PowerShell 里 `curl` 是别名，用 `curl.exe` 代替 `curl`。

## 8. 测试

```bash
uv run pytest -q
```

只跑某个模块的测试：

```bash
uv run pytest tests/link
```

测试不联网（夹具会拦截一切非回环连接），也不读 `server/.env`；也不加载真实的媒体模型（夹具设了 `MEDIA_PRELOAD=0`，媒体测试用假引擎）。需要真实密钥的测试标了 `live`，只在 `RUN_LIVE=1` 时运行（`tests/test_scenarios_live.py`，7 个）：

```bash
RUN_LIVE=1 uv run --frozen pytest -m live tests/test_scenarios_live.py
```

PowerShell 里先 `$env:RUN_LIVE = "1"` 再执行 `uv run --frozen pytest -m live tests/test_scenarios_live.py`。没有密钥时这 7 个会被跳过。

## 9. 目录

```text
server/
├── app/              服务代码（结构见 ARCHITECTURE.md）
├── data/             各分析器的数据文件
├── eval/             评测脚本
├── tests/            测试
├── var/              运行时数据：数据库、日志（不进 git）
├── .env.example      配置模板
├── pyproject.toml    依赖声明
└── uv.lock           锁定的依赖版本
```

## 10. Windows 备机逐项核对清单

代码按跨平台写（`pathlib`、所有文本读写显式 UTF-8、控制台强制 UTF-8），但**还没有在 Windows 上实际跑过**。拿到 Windows 备机后逐项核对，并把结果告诉后端：

- [ ] `py -3.12 --version` 是 3.12.x；`uv --version` 能用
- [ ] 在 `server/` 下 `uv sync` 成功（清华镜像能访问）
- [ ] `uv run pytest -q` 全部通过（跳过 7 个 `live` 是正常的）
- [ ] `Copy-Item .env.example .env`，填好密钥；`.env` 是 UTF-8
- [ ] `uv run python -m app.selfcheck` 的中文不乱码，逐项结果合理，主备模型「通过」
- [ ] `uv run python -m app` 启动，状态里中文不乱码，规则条数和 Mac 上一致
- [ ] `curl.exe` 走通「提交 → 轮询 → 拿到报告」，报告里的中文正常
- [ ] `uv run python -m app --mock` 模拟模式能走通
- [ ] `uv run python -m app.analyzers.cases import` 能导入案例，自检里案例条数正确
- [ ] `var/antifraud.db` 和 `var/logs/server.log` 生成在 `server/var/` 下；日志里中文正常
- [ ] Ctrl+C 能正常退出，再次启动不报数据库被占用
- [ ] 数据线方式：装好 adb（Android SDK Platform-Tools），`adb reverse tcp:8000 tcp:8000` 后手机能访问；**拔插数据线后是否需要重新执行**，把结论写进第 4 节
- [ ] 热点方式：`uv run python -m app --host 0.0.0.0`，防火墙弹窗选「允许」并勾选「专用网络」，手机用启动输出里的局域网地址能访问
- [ ] 关掉 Wi-Fi 再走一遍鉴定：20 秒内拿到 `suspicious` 报告，`degraded` 里有 `llm_unavailable_rules_only`
- [ ] 电源设置里「接通电源时进入睡眠」是「从不」
