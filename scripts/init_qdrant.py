#!/usr/bin/env python3
"""幂等初始化 Qdrant 集合（规约 T4）

  python scripts/init_qdrant.py            # 用环境变量里的 QDRANT_* / EMBEDDING_DIM
  python scripts/init_qdrant.py --dim 1024 # 显式指定维度

建的东西：集合 `campusclaw_chunks`（**Cosine**，size = EMBEDDING_DIM）+ `class_id`/`material_id`
两个 payload 索引。重复执行不会报错，也不会清空已有数据（建集合是 PUT 幂等语义）。

本机没有 Docker 时，可先用 `python scripts/qdrant_stub.py --port 6333` 起测试替身，
再把 `QDRANT_URL=http://127.0.0.1:6333` 指过去 —— 该替身只为验证行为，见其文件头说明。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.vector_store import QdrantClient, QdrantConfig, VectorStoreUnavailable  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="幂等初始化 Qdrant 集合")
    parser.add_argument("--url", help="覆盖 QDRANT_URL")
    parser.add_argument("--collection", help="覆盖 QDRANT_COLLECTION")
    parser.add_argument("--dim", type=int, help="覆盖 EMBEDDING_DIM（集合向量维度）")
    args = parser.parse_args()

    cfg = QdrantConfig.from_app()
    if args.url:
        cfg.url = args.url.rstrip("/")
    if args.collection:
        cfg.collection = args.collection
    if args.dim:
        cfg.dim = int(args.dim)

    if not cfg.ready:
        print(
            "[init_qdrant] 无法继续：缺少 QDRANT_URL（或 EMBEDDING_DIM=0）。\n"
            "  本地替身：python scripts/qdrant_stub.py --port 6333\n"
            "  然后：    QDRANT_URL=http://127.0.0.1:6333 EMBEDDING_DIM=1024 "
            "python scripts/init_qdrant.py",
            file=sys.stderr,
        )
        return 2

    client = QdrantClient(cfg)
    try:
        client.ensure_collection()
        count = client.count()
    except VectorStoreUnavailable as exc:
        print(f"[init_qdrant] 失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"[init_qdrant] 就绪：collection={cfg.collection} dim={cfg.dim} distance=Cosine "
        f"url={cfg.url} 现有向量数={count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
