#!/usr/bin/env python3
"""初始化数据库：建表 + 写入种子数据（规约 T2 / FR-05 / NFR-02）

用法：
  python scripts/init_db.py                 # 幂等：已有数据则跳过种子
  python scripts/init_db.py --force         # 清空业务表后重建种子
  DATABASE_PATH=/app/data/app.db UPLOAD_DIR=/app/uploads python scripts/init_db.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import BASE_DIR, db, seed  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="CampusClaw 数据库初始化")
    parser.add_argument(
        "--db",
        default=os.environ.get("DATABASE_PATH") or str(BASE_DIR / "data" / "app.db"),
        help="SQLite 文件路径",
    )
    parser.add_argument(
        "--uploads",
        default=os.environ.get("UPLOAD_DIR") or str(BASE_DIR / "uploads"),
        help="上传根目录",
    )
    parser.add_argument("--force", action="store_true", help="清空并重建种子数据")
    args = parser.parse_args()

    db.init_database(args.db)
    if args.force:
        seed.reseed(args.db, Path(args.uploads))
    else:
        conn = db.connect(args.db)
        try:
            seed.seed_database(conn, Path(args.uploads))
        finally:
            conn.close()

    conn = db.connect(args.db)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("classes", "users", "materials", "knowledge_entries")
        }
    finally:
        conn.close()

    print(
        "[init_db] 就绪："
        + " / ".join(f"{k}={v}" for k, v in counts.items())
        + f" / 数据库={args.db}"
    )
    print("[init_db] 六类核心表：classes, users, handouts, assignments, assistants, skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
