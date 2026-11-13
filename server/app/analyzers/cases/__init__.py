"""相似案例 `cases`（归案例与评测会话）。

本包内的文件全部归案例与评测会话；对外只需保证 `create()` 返回一个 `name == AnalyzerName.CASES` 的 Analyzer。
自己的数据表放在 `models.py`（会被自动建表），导入命令放在 `__main__.py`（`uv run python -m app.analyzers.cases`）。
"""

from app.analyzers.cases.analyzer import CasesAnalyzer, create

__all__ = ["CasesAnalyzer", "create"]
