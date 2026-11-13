"""链接体检 `link`（归链接体检会话）。

本包内的文件全部归链接体检会话；对外只需保证 `create()` 返回一个 `name == AnalyzerName.LINK` 的 Analyzer。
自己的数据表放在 `models.py`（会被自动建表），命令行入口放在 `__main__.py`（`uv run python -m app.analyzers.link`）。
"""

from app.analyzers.link.analyzer import LinkAnalyzer, create

__all__ = ["LinkAnalyzer", "create"]
