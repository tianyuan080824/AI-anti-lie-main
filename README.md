# AI 智能反诈系统

面向文本、链接和媒体内容的反诈辅助分析项目。当前仓库包含 FastAPI 后端、共享规则、接口示例及项目文档；Android 客户端的对接要求见交接文档，仓库尚未包含客户端源码。

## 项目结构

- server/：Python 3.12 后端、测试及依赖锁文件。
- shared/：共享规则和接口示例。
- docs/：接口契约、架构交接、开发进度及项目报告。

## 开始使用

安装 Python 3.12 和 uv 后，在 server/ 下运行 uv sync --frozen，并将 .env.example 复制为 .env。

按 [服务端说明](server/README.md) 配置、启动与运行测试。密钥填写在本地 .env 中，不要提交。媒体分析需要额外依赖和模型，安装步骤也见服务端说明。

## 文档

- [API 契约](docs/api-contract.md)
- [后端架构](server/ARCHITECTURE.md)
- [Android 对接](docs/android-handoff.md)
- [开发进度与验证记录](docs/server-progress.md)
- [项目报告](docs/report/project-report.md)
- [Android 客户端](android/README.md)

当前实现和已验证范围以源码及进度记录为准。模型、运行数据库、本地密钥与工具缓存不包含在仓库中。
