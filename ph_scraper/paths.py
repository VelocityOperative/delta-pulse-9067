"""应用数据目录（~/.ph_scraper）。"""
from pathlib import Path

APP_DIR = Path.home() / ".ph_scraper"


def app_dir() -> Path:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    return APP_DIR


def session_cache_file() -> Path:
    return app_dir() / "session.json"


def config_file() -> Path:
    return app_dir() / "config.json"


def db_file() -> Path:
    return app_dir() / "products.db"


def checkpoint_file(task_key: str) -> Path:
    d = app_dir() / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{task_key}.json"
