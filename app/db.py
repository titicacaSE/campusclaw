"""数据访问层（SQLite 3，标准库 sqlite3）

规约依据：
  - FR-03 实现约束：所有 SELECT 强制 `WHERE class_id = ?`，并封装为 list_materials(class_id)
    等函数；按 ID 访问先查行，再比对 class_id；写入时 class_id 只取自 session。
  - FR-05：建立班级、用户、讲义、作业、助手、技能六类核心数据结构，另有 materials、knowledge_entries。
  - NFR-02：`GET /health` 可选检测 SQLite 可读性。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- 六类核心结构之一：班级
CREATE TABLE IF NOT EXISTS classes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL UNIQUE,
  grade       TEXT,
  created_at  TEXT    NOT NULL
);

-- 六类核心结构之二：用户
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    NOT NULL UNIQUE,
  display_name  TEXT    NOT NULL,
  role          TEXT    NOT NULL CHECK (role IN ('teacher','student')),
  class_id      INTEGER NOT NULL REFERENCES classes(id),
  password_hash TEXT    NOT NULL,          -- 仅存单向哈希，禁止明文 password 列（NFR-01）
  created_at    TEXT    NOT NULL
);

-- 材料（班级私有数据）
CREATE TABLE IF NOT EXISTS materials (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  title             TEXT    NOT NULL,
  class_id          INTEGER NOT NULL REFERENCES classes(id),
  file_path         TEXT    NOT NULL,
  original_filename TEXT,
  ext               TEXT,
  subject           TEXT,
  size_bytes        INTEGER NOT NULL DEFAULT 0,
  uploaded_by       INTEGER NOT NULL REFERENCES users(id),
  parse_status      TEXT    NOT NULL DEFAULT 'ok',
  created_at        TEXT    NOT NULL
);

-- 知识库条目（班级私有数据，与材料同事务写入）
CREATE TABLE IF NOT EXISTS knowledge_entries (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  class_id    INTEGER NOT NULL REFERENCES classes(id),
  ord         INTEGER NOT NULL DEFAULT 0,
  body_text   TEXT    NOT NULL,
  char_count  INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT    NOT NULL
);

-- 六类核心结构之三：讲义
CREATE TABLE IF NOT EXISTS handouts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  class_id    INTEGER NOT NULL REFERENCES classes(id),
  title       TEXT    NOT NULL,
  subject     TEXT,
  material_id INTEGER REFERENCES materials(id),
  created_at  TEXT    NOT NULL
);

-- 六类核心结构之四：作业
CREATE TABLE IF NOT EXISTS assignments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  class_id   INTEGER NOT NULL REFERENCES classes(id),
  title      TEXT    NOT NULL,
  due_date   TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT    NOT NULL
);

-- 六类核心结构之五：助手
CREATE TABLE IF NOT EXISTS assistants (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  description TEXT,
  scope       TEXT    NOT NULL DEFAULT 'class' CHECK (scope IN ('class','school')),
  class_id    INTEGER REFERENCES classes(id),
  owner_id    INTEGER REFERENCES users(id),
  created_at  TEXT    NOT NULL
);

-- 六类核心结构之六：技能
CREATE TABLE IF NOT EXISTS skills (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT    NOT NULL,
  description  TEXT,
  assistant_id INTEGER REFERENCES assistants(id),
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_class      ON users(class_id);
CREATE INDEX IF NOT EXISTS idx_materials_class  ON materials(class_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_class  ON knowledge_entries(class_id, material_id);
"""

CORE_TABLES = (
    "classes",
    "users",
    "handouts",
    "assignments",
    "assistants",
    "skills",
)
PRIVATE_TABLES = ("materials", "knowledge_entries")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """建立连接（不依赖 Flask 上下文，供脚本与测试使用）。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """当前请求上下文内的连接。"""
    if "conn" not in g:
        g.conn = connect(current_app.config["DATABASE"])
    return g.conn


def close_conn(_exc=None) -> None:
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


def ensure_ready(app) -> None:
    """确保库与目录可用：库不存在或未初始化时建表并写入种子数据。"""
    from . import seed

    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    db_path = Path(app.config["DATABASE"])
    needs_init = not db_path.exists() or not schema_initialized(db_path)
    if needs_init:
        init_database(db_path, quiet=True)
        conn = connect(db_path)
        try:
            seed.seed_database(conn, Path(app.config["UPLOAD_DIR"]), quiet=True)
        finally:
            conn.close()
    return


def schema_initialized(db_path: str | Path) -> bool:
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def init_database(db_path: str | Path, quiet: bool = False) -> None:
    """建表（幂等）。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    if not quiet:
        print(f"[init_db] 表结构已就绪：{db_path}")


def database_ok() -> bool:
    """NFR-02：/health 检测 SQLite 可读性。"""
    try:
        row = query_one("SELECT COUNT(*) AS n FROM users")
        return row is not None
    except Exception:
        return False


# --------------------------------------------------------------------- 基础查询

def query(sql: str, params=()):
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params=()):
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params=()):
    conn = get_conn()
    cur = conn.execute(sql, params)
    return cur.lastrowid


@contextmanager
def transaction():
    """显式事务：材料与知识库条目必须同事务写入（FR-04）。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --------------------------------------------------------------------- 用户与班级

def get_user_by_username(username: str):
    return query_one("SELECT * FROM users WHERE username = ?", (username,))


def get_user(user_id: int):
    return query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def get_class(class_id: int):
    return query_one("SELECT * FROM classes WHERE id = ?", (class_id,))


# --------------------------------------------------------------------- 班级私有数据
# 以下函数一律要求调用方传入「来自 session 的 class_id」，SQL 强制按班级过滤（FR-03）。

def list_materials(class_id: int):
    return query(
        """
        SELECT m.*, u.display_name AS uploader_name,
               (SELECT COUNT(*) FROM knowledge_entries k WHERE k.material_id = m.id) AS entry_count
          FROM materials m
          LEFT JOIN users u ON u.id = m.uploaded_by
         WHERE m.class_id = ?
         ORDER BY m.id DESC
        """,
        (class_id,),
    )


def get_material(material_id: int, class_id: int):
    """按 ID 取材料：class_id 参与查询条件，跨班一律查不到（FR-03 S1）。"""
    return query_one(
        """
        SELECT m.*, u.display_name AS uploader_name
          FROM materials m
          LEFT JOIN users u ON u.id = m.uploaded_by
         WHERE m.id = ? AND m.class_id = ?
        """,
        (material_id, class_id),
    )


def get_material_any(material_id: int):
    """仅供审计/诊断：不带班级过滤，调用方 MUST NOT 直接把结果返回给客户端。"""
    return query_one("SELECT * FROM materials WHERE id = ?", (material_id,))


def count_materials(class_id: int) -> int:
    row = query_one("SELECT COUNT(*) AS n FROM materials WHERE class_id = ?", (class_id,))
    return int(row["n"]) if row else 0


def count_knowledge_entries(class_id: int) -> int:
    row = query_one("SELECT COUNT(*) AS n FROM knowledge_entries WHERE class_id = ?", (class_id,))
    return int(row["n"]) if row else 0


def count_knowledge_entries_for_material(material_id: int) -> int:
    row = query_one(
        "SELECT COUNT(*) AS n FROM knowledge_entries WHERE material_id = ?", (material_id,)
    )
    return int(row["n"]) if row else 0


def list_knowledge_entries(material_id: int, class_id: int):
    return query(
        "SELECT * FROM knowledge_entries WHERE material_id = ? AND class_id = ? ORDER BY ord",
        (material_id, class_id),
    )


def insert_material(
    *,
    title: str,
    class_id: int,
    file_path: str,
    original_filename: str,
    ext: str,
    size_bytes: int,
    uploaded_by: int,
    subject: str | None = None,
    parse_status: str = "ok",
) -> int:
    return execute(
        """
        INSERT INTO materials
          (title, class_id, file_path, original_filename, ext, subject, size_bytes,
           uploaded_by, parse_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            class_id,
            file_path,
            original_filename,
            ext,
            subject,
            size_bytes,
            uploaded_by,
            parse_status,
            utcnow(),
        ),
    )


def insert_knowledge_entry(*, material_id: int, class_id: int, body_text: str, ord_: int = 0) -> int:
    return execute(
        """
        INSERT INTO knowledge_entries
          (material_id, class_id, ord, body_text, char_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (material_id, class_id, ord_, body_text, len(body_text), utcnow()),
    )


def list_handouts(class_id: int):
    return query("SELECT * FROM handouts WHERE class_id = ? ORDER BY id", (class_id,))


def list_assignments(class_id: int):
    return query("SELECT * FROM assignments WHERE class_id = ? ORDER BY id", (class_id,))


def list_assistants(class_id: int):
    return query(
        "SELECT * FROM assistants WHERE class_id = ? OR class_id IS NULL ORDER BY id", (class_id,)
    )


def list_skills():
    return query("SELECT * FROM skills ORDER BY id")
