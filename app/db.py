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

-- 检索用的分块索引（班级私有，派生数据；可由 materials + knowledge_entries 重建）
-- 规约：add-traceable-vector-retrieval design.md §2.1
--   id 同时是 Qdrant point id；偏移为「相对预处理后文本」的 Unicode 码点、左闭右开。
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id    INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  class_id       INTEGER NOT NULL REFERENCES classes(id),
  chunk_index    INTEGER NOT NULL,
  body_text      TEXT    NOT NULL,
  start_offset   INTEGER NOT NULL,
  end_offset     INTEGER NOT NULL,
  line_start     INTEGER NOT NULL,
  line_end       INTEGER NOT NULL,
  char_count     INTEGER NOT NULL DEFAULT 0,
  chunk_sha256   TEXT    NOT NULL,
  source_sha256  TEXT    NOT NULL,
  index_status   TEXT    NOT NULL DEFAULT 'pending',
  section        TEXT,
  created_at     TEXT    NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_chunks_class     ON knowledge_chunks(class_id, material_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_chunks_material  ON knowledge_chunks(material_id);
"""

# materials 的增量列（迁移时按需补齐，可重复执行）
MATERIAL_EXTRA_COLUMNS = (
    ("source_sha256", "TEXT"),
    ("indexed_at", "TEXT"),
    ("index_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("chunk_strategy", "TEXT NOT NULL DEFAULT 'auto'"),
    ("chunk_config_json", "TEXT"),
)

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
    """当前请求上下文内的连接。

    脚本（`scripts/backfill_index.py` 等）没有 Flask 应用上下文，用
    `bind_connection()` 显式把连接交给 db 层即可复用同一套查询封装，
    不必为脚本在 db 里开后门、也不必伪造 app 上下文。
    """
    if _BOUND_CONN is not None:
        return _BOUND_CONN
    if "conn" not in g:
        g.conn = connect(current_app.config["DATABASE"])
    return g.conn


_BOUND_CONN: sqlite3.Connection | None = None


def bind_connection(conn: sqlite3.Connection | None):
    """把外部连接绑定给 db 层（脚本用）。传 None 解绑。返回上一次绑定的连接。"""
    global _BOUND_CONN
    previous, _BOUND_CONN = _BOUND_CONN, conn
    return previous


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
    # 增量迁移：幂等补齐本变更新增的表（knowledge_chunks）与 materials 的五个新列
    migrate(db_path, quiet=True)
    return


def migrate(db_path: str | Path, quiet: bool = True) -> list[str]:
    """幂等迁移：补建缺失的表与列。列已存在则跳过（规约 design.md §10）。返回本次新增的列名。"""
    conn = connect(db_path)
    added: list[str] = []
    try:
        conn.executescript(SCHEMA_SQL)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(materials)")}
        for name, decl in MATERIAL_EXTRA_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE materials ADD COLUMN {name} {decl}")
                added.append(name)
        conn.commit()
    finally:
        conn.close()
    if added and not quiet:
        print(f"[migrate] materials 新增列：{', '.join(added)}")
    return added


def schema_initialized(db_path: str | Path) -> bool:
    """库是否已建表（以 users 表是否存在为准）。"""
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
    """建表（幂等）。顺带补齐增量列，保证「新建的库」就是**当前**的完整结构。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    migrate(db_path, quiet=True)
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


# --------------------------------------------------------------------- 切片检索
# 规约：add-traceable-vector-retrieval §5.2 —— class_id MUST 是候选生成的第一条谓词，
# 且排在匹配、排序、计数、limit 之前。下列 SQL 的顺序即为约束本身，改动顺序即违反规约。

SQL_KEYWORD_SEARCH = """
SELECT c.id, c.material_id, c.chunk_index, c.body_text, c.start_offset, c.end_offset,
       c.line_start, c.line_end, c.char_count, c.chunk_sha256, c.source_sha256,
       m.title AS material_title, m.ext AS material_ext
  FROM knowledge_chunks c
  JOIN materials m ON m.id = c.material_id
 WHERE c.class_id = ?                                  -- ① 班级边界：最先、单表谓词
   AND instr(lower(c.body_text), lower(?)) > 0          -- ② 匹配：仅在班级范围内
"""

SQL_CHUNK_BY_ID = """
SELECT c.*, m.title AS material_title, m.ext AS material_ext,
       m.original_filename AS material_filename,
       m.source_sha256 AS material_source_sha256
  FROM knowledge_chunks c
  JOIN materials m ON m.id = c.material_id
 WHERE c.id = ? AND c.class_id = ?                     -- 双条件：跨班与不存在一律查不到
"""


def search_chunks(class_id: int, keyword: str, limit: int = 10, offset: int = 0):
    """关键字检索（片内字面匹配）。返回 `(命中窗口, 本班匹配总数)`。

    命中窗口元素为 `(hit_count, match_in_chunk, row)`；
    排序键 `命中次数 DESC → material_id ASC → chunk_index ASC`。
    计数只统计本班（SQL 的 class_id 谓词已限定范围）。
    """
    rows = query(SQL_KEYWORD_SEARCH, (int(class_id), keyword or ""))
    needle = (keyword or "").lower()
    scored = []
    for row in rows:
        haystack = row["body_text"].lower()
        count = haystack.count(needle) if needle else 0
        if count <= 0:
            continue
        match_in_chunk = haystack.find(needle)
        scored.append((count, match_in_chunk, row))

    scored.sort(key=lambda t: (-t[0], t[2]["material_id"], t[2]["chunk_index"]))
    total_matched = len(scored)
    window = scored[int(offset): int(offset) + int(limit)]
    return [(item[0], item[1], item[2]) for item in window], total_matched


def count_chunks(class_id: int, keyword: str | None = None) -> int:
    if keyword is None:
        row = query_one(
            "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE class_id = ?", (int(class_id),)
        )
        return int(row["n"]) if row else 0
    hits, total = search_chunks(class_id, keyword, limit=1, offset=0)
    return total


def get_chunk(chunk_id: int, class_id: int):
    """按 ID 取切片：class_id 参与条件，跨班与不存在都查不到（两者对外同形 404）。"""
    return query_one(SQL_CHUNK_BY_ID, (int(chunk_id), int(class_id)))


def get_chunk_any(chunk_id: int):
    """仅供审计：不带班级过滤，调用方 MUST NOT 直接返回给客户端。"""
    return query_one("SELECT * FROM knowledge_chunks WHERE id = ?", (int(chunk_id),))


def list_chunks(class_id: int, material_id: int):
    """某材料的全部切片（按 chunk_index 升序）。"""
    return query(
        """
        SELECT * FROM knowledge_chunks
         WHERE class_id = ? AND material_id = ?
         ORDER BY chunk_index
        """,
        (int(class_id), int(material_id)),
    )


def list_chunks_by_material_any(material_id: int):
    """仅供回填脚本使用（脚本自行保证遍历边界）。"""
    return query(
        "SELECT * FROM knowledge_chunks WHERE material_id = ? ORDER BY chunk_index",
        (int(material_id),),
    )


def insert_chunk(
    *,
    material_id: int,
    class_id: int,
    chunk_index: int,
    body_text: str,
    start_offset: int,
    end_offset: int,
    line_start: int,
    line_end: int,
    chunk_sha256: str,
    source_sha256: str,
    index_status: str = "pending",
    section: str | None = None,
) -> int:
    return execute(
        """
        INSERT INTO knowledge_chunks
          (material_id, class_id, chunk_index, body_text, start_offset, end_offset,
           line_start, line_end, char_count, chunk_sha256, source_sha256,
           index_status, section, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(material_id),
            int(class_id),
            int(chunk_index),
            body_text,
            int(start_offset),
            int(end_offset),
            int(line_start),
            int(line_end),
            len(body_text),
            chunk_sha256,
            source_sha256,
            index_status,
            section,
            utcnow(),
        ),
    )


def delete_chunks_for_material(material_id: int) -> list[int]:
    """重建前清旧切片。返回被删除的切片 id 列表，供调用方同步删除对应的旧向量。"""
    conn = get_conn()
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM knowledge_chunks WHERE material_id = ?", (int(material_id),)
    ).fetchall()]
    conn.execute("DELETE FROM knowledge_chunks WHERE material_id = ?", (int(material_id),))
    return ids


def replace_chunks(material_id: int, class_id: int, chunks: list[dict]) -> tuple[list[int], list[int]]:
    """以「先清后写」的方式替换某材料的全部切片（**必须在调用方的事务内**）。

    `class_id` 是强制参数：重建只允许作用于「会话班级内的材料」，写入行的 class_id
    也只取这个值，客户端无法通过参数把切片写到别的班级。

    返回 `(deleted_ids, inserted_ids)`；`deleted_ids` 供调用方同步清理 Qdrant 旧向量。
    """
    deleted = delete_chunks_for_material(material_id)
    inserted: list[int] = []
    for record in chunks:
        inserted.append(
            insert_chunk(
                material_id=int(material_id),
                class_id=int(class_id),          # 忽略 record 中的 class_id，防止越班写入
                chunk_index=int(record["chunk_index"]),
                body_text=record["body_text"],
                start_offset=int(record["start_offset"]),
                end_offset=int(record["end_offset"]),
                line_start=int(record["line_start"]),
                line_end=int(record["line_end"]),
                chunk_sha256=record["chunk_sha256"],
                source_sha256=record["source_sha256"],
                index_status=str(record.get("index_status") or "pending"),
                section=record.get("section"),
            )
        )
    return deleted, inserted


def set_chunk_status(chunk_id: int, status: str) -> None:
    execute("UPDATE knowledge_chunks SET index_status = ? WHERE id = ?", (status, int(chunk_id)))


def set_material_index_state(
    material_id: int,
    status: str,
    *,
    source_sha256: str | None = None,
    strategy: str | None = None,
    config_json: str | None = None,
    indexed: bool = False,
) -> None:
    """更新材料的索引状态。`indexed=True` 时同时写入 indexed_at。"""
    sets = ["index_status = ?"]
    params: list = [status]
    if source_sha256 is not None:
        sets.append("source_sha256 = ?")
        params.append(source_sha256)
    if strategy is not None:
        sets.append("chunk_strategy = ?")
        params.append(strategy)
    if config_json is not None:
        sets.append("chunk_config_json = ?")
        params.append(config_json)
    if indexed:
        sets.append("indexed_at = ?")
        params.append(utcnow())
    params.append(int(material_id))
    execute(f"UPDATE materials SET {', '.join(sets)} WHERE id = ?", tuple(params))


def index_warnings(class_id: int):
    """本班索引未就绪的材料（`index_status != 'indexed'`），供 degraded 信号使用。"""
    return query(
        """
        SELECT id AS material_id, title, index_status
          FROM materials
         WHERE class_id = ? AND index_status != 'indexed'
         ORDER BY id
        """,
        (int(class_id),),
    )


def count_materials_filtered(class_id: int, title: str | None = None) -> int:
    if title:
        row = query_one(
            "SELECT COUNT(*) AS n FROM materials WHERE class_id = ? AND title LIKE ?",
            (int(class_id), f"%{title}%"),
        )
    else:
        row = query_one("SELECT COUNT(*) AS n FROM materials WHERE class_id = ?", (int(class_id),))
    return int(row["n"]) if row else 0


def list_materials_paged(class_id: int, page: int = 1, page_size: int = 20, title: str | None = None):
    """本班材料分页（课件接口契约：`page` / `page_size=20` / `title`）。"""
    offset = max(0, (int(page) - 1) * int(page_size))
    base = """
        SELECT m.*, u.display_name AS uploader_name,
               (SELECT COUNT(*) FROM knowledge_entries k WHERE k.material_id = m.id) AS entry_count,
               (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.material_id = m.id) AS chunk_count
          FROM materials m
          LEFT JOIN users u ON u.id = m.uploaded_by
         WHERE m.class_id = ?"""
    params: list = [int(class_id)]
    if title:
        base += " AND m.title LIKE ?"
        params.append(f"%{title}%")
    base += " ORDER BY m.id DESC LIMIT ? OFFSET ?"
    params.extend([int(page_size), offset])
    return query(base, tuple(params))
