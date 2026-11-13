"""媒体与深伪 `deepfake`（P1，归话术/汇总会话）。P0 只有占位实现。"""

from app.analyzers.deepfake.analyzer import DeepfakeAnalyzer, create

__all__ = ["DeepfakeAnalyzer", "create"]
