"""仓库内的固定路径。一律用 pathlib，从本文件位置推算，与当前工作目录无关。"""

from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = SERVER_DIR.parent  # AI anti lie/
SHARED_DIR = PROJECT_DIR / "shared"
CONTRACT_EXAMPLES_DIR = SHARED_DIR / "contract-examples"
RULES_FILE = SHARED_DIR / "rules.json"
DATA_DIR = SERVER_DIR / "data"
