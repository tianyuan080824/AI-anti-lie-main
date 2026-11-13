"""运行环境：可选依赖是否装了、ffmpeg 在哪、各库的缓存目录。

都不导入大依赖，可以在任何时候调用。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

#: 能力 → 需要的模块（import 名）
MODULES: dict[str, str] = {
    "python-multipart": "python_multipart",
    "Pillow": "PIL",
    "rapidocr": "rapidocr",
    "onnxruntime": "onnxruntime",
    "torch": "torch",
    "funasr": "funasr",
}


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def missing(*packages: str) -> list[str]:
    """返回没装的包名（`MODULES` 的键）。"""
    return [p for p in packages if not has_module(MODULES[p])]


@dataclass(frozen=True)
class Tools:
    ffmpeg: str | None
    ffprobe: str | None

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)


def find_tools(settings: Settings) -> Tools:
    """`FFMPEG_PATH` 指向的文件优先，否则在 PATH 里找；ffprobe 先找 ffmpeg 旁边的同名文件。"""
    ffmpeg: str | None = None
    if settings.ffmpeg_path and Path(settings.ffmpeg_path).is_file():
        ffmpeg = str(Path(settings.ffmpeg_path))
    else:
        ffmpeg = shutil.which("ffmpeg")
    ffprobe: str | None = None
    if ffmpeg:
        p = Path(ffmpeg)
        sibling = p.with_name("ffprobe" + p.suffix)
        if sibling.is_file():
            ffprobe = str(sibling)
    if ffprobe is None:
        ffprobe = shutil.which("ffprobe")
    return Tools(ffmpeg=ffmpeg, ffprobe=ffprobe)


def configure_cache_dirs(models_dir: Path) -> None:
    """导入 FunASR / ModelScope / torch / Hugging Face 之前调用：把它们的缓存目录都指到 models_dir 下，
    并让 Hugging Face 只用本地文件。加载本地权重时本来就不联网，这一步是防止意外写到 ~/.cache。"""
    cache = models_dir / ".cache"
    values = {
        "MODELSCOPE_CACHE": models_dir / "modelscope",
        "HF_HOME": models_dir / "huggingface",
        "TORCH_HOME": models_dir / "torch",
        "XDG_CACHE_HOME": cache,
        "NUMBA_CACHE_DIR": cache / "numba",
        "MPLCONFIGDIR": cache / "matplotlib",
    }
    for key, value in values.items():
        os.environ[key] = str(value)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


__all__ = ["MODULES", "Tools", "configure_cache_dirs", "find_tools", "has_module", "missing"]
