#!/usr/bin/env python3
"""幂等回填切片与向量索引（规约 T8）

  python scripts/backfill_index.py                       # 用 data/app.db 与默认上传目录
  python scripts/backfill_index.py --db path --uploads path
  python scripts/backfill_index.py --strategy hierarchy  # 换切分策略重建

幂等性从哪来
------------
对每份材料：
  1. 取原文（优先 `knowledge_entries`，缺失时回读落盘文件）
  2. 算出「预处理后正文的 sha256」
  3. **只有在「没有切片」或「已存切片的 source_sha256 ≠ 当前正文哈希」时才动手**
     —— 这就是 `indexing.needs_index()` 的判据

于是第二次运行：`chunks_written=0`、`embedded=0`，既不会重复切片，也不会重复嵌入。
要强制重建（例如换了模型或换了策略），用 `--force`。

输出为单行 JSON（便于验收脚本断言），含 `materials_scanned` / `chunks_written` / `embedded` /
`skipped` / `failed` 等计数。退出码：0 成功；1 存在失败项。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 控制台编码兜底：本脚本输出单行 JSON，可能含中文（材料标题等）。
# 中文 Windows 默认按 GBK 写 stdout，而消费方（验收脚本 / 流水线）通常按 UTF-8 解析，
# 两者错配会直接抛 UnicodeDecodeError。统一固定为 UTF-8，让输出与解析一致。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, gateway, indexing, knowledge  # noqa: E402
from app.chunking import ChunkConfig, ChunkError  # noqa: E402
from app.retrieval import build_chunk_records  # noqa: E402
from app.vector_store import QdrantConfig, client_for  # noqa: E402


def material_body_text(conn, row) -> str:
    rows = conn.execute(
        "SELECT body_text FROM knowledge_entries WHERE material_id = ? ORDER BY ord",
        (int(row["id"]),),
    ).fetchall()
    text = "\n\n".join(r["body_text"] for r in rows if r["body_text"])
    if text.strip():
        return text
    try:
        return knowledge.parse_file(row["file_path"])
    except (knowledge.UploadError, OSError):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="CampusClaw 索引幂等回填")
    parser.add_argument(
        "--db",
        default=os.environ.get("DATABASE_PATH") or str(Path(__file__).resolve().parent.parent / "data" / "app.db"),
    )
    parser.add_argument("--uploads", default=os.environ.get("UPLOAD_DIR") or "")
    parser.add_argument("--strategy", choices=("auto", "custom", "hierarchy"), default="auto")
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--overlap-percent", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="无视幂等判据，全部重建")
    parser.add_argument("--dry-run", action="store_true", help="只报告将要处理的材料，不写任何数据")
    args = parser.parse_args()

    db.init_database(args.db)
    db.migrate(args.db, quiet=True)
    if args.uploads:
        os.environ.setdefault("UPLOAD_DIR", args.uploads)

    try:
        cfg = ChunkConfig.from_request(
            {
                "chunk_strategy": args.strategy,
                "chunk_max_chars": args.max_chars,
                "chunk_overlap_percent": args.overlap_percent,
            }
        )
    except ChunkError as exc:
        print(f"[backfill] 切分参数非法：{exc}", file=sys.stderr)
        return 2

    gateway_cfg = gateway.GatewayConfig.from_app()
    qdrant_cfg = QdrantConfig.from_app()
    qdrant = client_for() if qdrant_cfg.ready else None

    conn = db.connect(args.db)
    db.bind_connection(conn)          # 让 db 层的查询封装在无 app 上下文的脚本里也能用
    summary = {
        "db": args.db,
        "strategy": cfg.strategy,
        "materials_scanned": 0,
        "chunks_written": 0,
        "embedded": 0,
        "skipped": 0,
        "failed": 0,
        "vector_store": "ok" if qdrant else "unconfigured",
        "gateway": "ok" if gateway_cfg.embedding_ready else "unconfigured",
        "errors": [],
    }
    exit_code = 0

    try:
        rows = conn.execute("SELECT * FROM materials ORDER BY id").fetchall()
        summary["materials_scanned"] = len(rows)
        for row in rows:
            material_id = int(row["id"])
            class_id = int(row["class_id"])
            body_text = material_body_text(conn, row)
            if not body_text.strip():
                summary["failed"] += 1
                summary["errors"].append(f"material {material_id}: 正文为空，跳过")
                exit_code = 1
                continue

            records, source_sha256 = build_chunk_records(material_id, class_id, body_text, cfg)
            existing = conn.execute(
                "SELECT source_sha256 FROM knowledge_chunks WHERE material_id = ? LIMIT 1",
                (material_id,),
            ).fetchone()

            if not args.force and not indexing.needs_index(
                [existing] if existing else [], source_sha256
            ):
                summary["skipped"] += 1
                continue

            if args.dry_run:
                summary["chunks_written"] += len(records)
                continue

            try:
                outcome = indexing.index_material(
                    material_id=material_id,
                    class_id=class_id,
                    body_text=body_text,
                    cfg=cfg,
                    qdrant=qdrant,
                    gateway_cfg=gateway_cfg,
                )
            except Exception as exc:  # noqa: BLE001
                summary["failed"] += 1
                summary["errors"].append(f"material {material_id}: {type(exc).__name__}: {exc}")
                exit_code = 1
                continue

            summary["chunks_written"] += outcome.chunk_count
            summary["embedded"] += outcome.vectors_written
            if outcome.index_status == indexing.STATUS_FAILED:
                summary["failed"] += 1
                summary["errors"].append(
                    f"material {material_id}: 向量侧未就绪（{outcome.vector_error or '未知'}）"
                )
                # 向量侧未就绪不算脚本失败：正文与切片已可用，keyword 仍能命中
    finally:
        db.bind_connection(None)
        conn.close()

    print("[backfill] " + json.dumps(summary, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
