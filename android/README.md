# Android 客户端

原生 Kotlin + Jetpack Compose 客户端，按 `docs/android-handoff.md` 与 `docs/api-contract.md` 对接。

## 环境

- Android Studio（包含 Android SDK 35）
- JDK 17 或更新版本
- Android Gradle Plugin 8.9.2

在 Android Studio 中打开本目录并同步 Gradle，然后运行 `app` 配置。当前开发机若通过 USB 联调，先启动后端模拟模式，再运行：

```bash
cd server
uv run python -m app --mock
adb reverse tcp:8000 tcp:8000
```

App 默认请求 `http://127.0.0.1:8000`，令牌默认是 `dev-token`。使用热点时，以 `0.0.0.0` 启动服务，并在 App 的「设置 → 服务连接」填写笔记本局域网地址。调试构建允许局域网 HTTP；发布构建只允许本机回环 HTTP，其余地址必须使用 HTTPS。

首次进入可选字号、试听朗读并预览暖阳客厅风格。首页只分析用户手动粘贴的文字，发送前在端侧脱敏；当前版本尚未接入媒体选择、家庭协作和演练内容，这些页面会明确说明未接入，不会展示伪造结果。分析报告包含证据、建议、可用的拨号操作和用户反馈入口。

## 验证

在 Android Studio 的 Gradle 工具窗口运行 `test` 与 `assembleDebug`。也可在安装 Gradle 8.11.1 的终端中，于本目录运行 `gradle test assembleDebug`。本仓库未提交 Gradle Wrapper。开发机需安装 JDK 17 和 Android SDK 35。