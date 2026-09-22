"""Local SQLite database location."""

import sqlite3
from pathlib import Path


def connect():
    path = Path.home() / "Library" / "Application Support" / "MoneyManager" / "money_manager.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)
