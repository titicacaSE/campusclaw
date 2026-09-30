"""索引编排：切片入库 → 嵌入 → 写向量 → 置 `index_status`（design.md §3 / §4 / §8）

规约依据 `add-traceable-vector-retrieval`：

  - 切片与正文同事务写入（materials / knowledge_entries / knowledge_chunks 一起提交）；
    **切分本身在事务外**（纯函数，抛错则整体不写）
  - 嵌入与写向量在**提交之后**进行：失败不回滚材料，只把切片与材料置为 `failed`，
    正文仍可被 `keyword` 模式命中（T4 明确要求）
  - `index_status` 状态机：`pending → indexing → indexed`，向量侧任何一步失败落到 `failed`
    （`failed` 不是终态：`reindex` 与 `backfill_index.py` 都可重试）
  - Qdrant / 嵌入网关不可用 → 切片与材料置 `failed`，**不写入不完整向量**，也不编造相似度

拆成两个阶段是为了让「上传」能把自己的事务边界握在手里：

  `build_chunk_records()` → `store_chunks()`（调用方事务内） → `commit`
                            → `index_vectors()`（提交后，各自短事务）
  `reindex` / `backfill` 用 `index_material()` 把两阶段串起来，自带事务。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from . import db, gateway
from .chunking import ChunkConfig
from .retrieval import build_chunk_records
from .vector_store import QdrantClient, VectorStoreUnavailable

log = logging.getLogger("campusclaw.indexing")

STATUS_PENDING = "pending"
STATUS_INDEXING = "indexing"
STATUS_INDEXED = "indexed"
STATUS_FAILED = "failed"


@dataclass
class IndexOutcome:
    """一次索引动作的结果。`vector_error` 非空表示向量侧未就绪（正文仍在库中）。"""

    material_id: int
    class_id: int
    chunk_count: int = 0
    chunk_strategy: str = "auto"
    source_sha256: str = ""
    index_status: str = STATUS_PENDING
    vectors_written: int = 0
    deleted_chunk_ids: list[int] = field(default_factory=list)
    inserted_chunk_ids: list[int] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    vector_error: str | None = None
    skipped: bool = False

    def to_dict(self) -> dict:
        payload = {
            "material_id": self.material_id,
            "class_id": self.class_id,
            "chunk_count": self.chunk_count,
            "chunk_strategy": self.chunk_strategy,
            "source_sha256": self.source_sha256,
            "index_status": self.index_status,
            "vectors_written": self.vectors_written,
        }
        if self.vector_error:
            payload["vector_error"] = self.vector_error
        if self.skipped:
            payload["skipped"] = True
        return payload


# ---------------------------------------------------------------- 阶段一：切片入库


def store_chunks(
    material_id: int,
    class_id: int,
    records: list[dict],
    source_sha256: str,
    cfg: ChunkConfig,
    *,
    mark_indexing: bool = True,
) -> tuple[list[int], list[int]]:
    """**在调用方事务内**写入切片并置 `indexing`。返回 `(deleted_ids, inserted_ids)`。

    本函数不提交、不回滚：事务边界属于调用方（上传路径与材料行、知识条目同生共死）。
    """
    deleted, inserted = db.replace_chunks(material_id, class_id, records)
    if mark_indexing:
        db.set_material_index_state(
            material_id,
            STATUS_INDEXING,
            source_sha256=source_sha256,
            strategy=cfg.strategy,
            config_json=json.dumps(cfg.as_dict(), ensure_ascii=False, sort_keys=True),
        )
    return deleted, inserted


# ---------------------------------------------------------------- 阶段二：向量


def _points_for(records: list[dict], chunk_ids: list[int], vectors: list[list[float]]) -> list[dict]:
    """组装 Qdrant 点：id = `knowledge_chunks.id`，payload **只含标识、不含正文**（D4）。"""
    points: list[dict] = []
    for record, chunk_id, vector in zip(records, chunk_ids, vectors):
        points.append(
            {
                "id": int(chunk_id),
                "vector": [float(x) for x in vector],
                "payload": {
                    "chunk_id": int(chunk_id),
                    "material_id": int(record["material_id"]),
                    "class_id": int(record["class_id"]),
                    "chunk_index": int(record["chunk_index"]),
                    "chunk_sha256": record["chunk_sha256"],
                },
            }
        )
    return points


def _mark_failed(chunk_ids: list[int], material_id: int, reason: str) -> None:
    """向量侧失败：切片保留、状态置 failed（正文仍可被 keyword 命中）。"""
    with db.transaction():
        for chunk_id in chunk_ids:
            db.set_chunk_status(chunk_id, STATUS_FAILED)
        db.set_material_index_state(material_id, STATUS_FAILED)
    log.info("材料 %s 置 failed（%s），正文保留", material_id, reason)


def index_vectors(
    *,
    material_id: int,
    records: list[dict],
    chunk_ids: list[int],
    qdrant: QdrantClient | None,
    gateway_cfg: gateway.GatewayConfig | None,
    embed_transport=None,
    delete_vectors: bool = True,
) -> IndexOutcome:
    """**提交之后**的向量阶段。任何失败都只影响 `index_status`，不影响正文可用性。"""
    outcome = IndexOutcome(material_id=int(material_id), class_id=0)
    outcome.chunk_count = len(records)
    outcome.inserted_chunk_ids = list(chunk_ids)

    if not records:
        # 空正文：没有切片可嵌入。这一步**不调用嵌入网关**。
        with db.transaction():
            db.set_material_index_state(material_id, STATUS_INDEXED, indexed=True)
        outcome.index_status = STATUS_INDEXED
        return outcome

    if qdrant is None or gateway_cfg is None:
        _mark_failed(chunk_ids, material_id, "向量库或嵌入网关未配置")
        outcome.index_status = STATUS_FAILED
        outcome.vector_error = "向量库或嵌入网关未配置"
        return outcome

    # 重建时先按 material_id 删旧向量；失败不阻断（回表侧仍有 class_id 双保险）
    if delete_vectors:
        try:
            qdrant.ensure_collection()      # 幂等：全新部署未跑 init_qdrant 时也能自愈
            qdrant.delete_by_material(material_id)
        except VectorStoreUnavailable as exc:
            log.warning("清理旧向量失败（material_id=%s）：%s", material_id, exc)

    try:
        vectors = gateway.embed(
            [r["body_text"] for r in records], cfg=gateway_cfg, transport=embed_transport
        )
    except (gateway.EmbeddingUnavailable, gateway.EmbeddingDimMismatch) as exc:
        _mark_failed(chunk_ids, material_id, str(exc))
        outcome.index_status = STATUS_FAILED
        outcome.vector_error = str(exc)
        log.warning("嵌入失败，材料 %s 保留正文并置 failed：%s", material_id, exc)
        return outcome

    points = _points_for(records, chunk_ids, vectors)
    try:
        qdrant.upsert(points)
    except VectorStoreUnavailable as exc:
        _mark_failed(chunk_ids, material_id, str(exc))
        outcome.index_status = STATUS_FAILED
        outcome.vector_error = str(exc)
        log.warning("写向量失败，材料 %s 保留正文并置 failed：%s", material_id, exc)
        return outcome

    with db.transaction():
        for chunk_id in chunk_ids:
            db.set_chunk_status(chunk_id, STATUS_INDEXED)
        db.set_material_index_state(material_id, STATUS_INDEXED, indexed=True)
    outcome.index_status = STATUS_INDEXED
    outcome.vectors_written = len(points)
    return outcome


# ---------------------------------------------------------------- 便捷入口（自带事务）


def index_material(
    *,
    material_id: int,
    class_id: int,
    body_text: str,
    cfg: ChunkConfig | None = None,
    qdrant: QdrantClient | None = None,
    gateway_cfg: gateway.GatewayConfig | None = None,
    embed_transport=None,
    delete_vectors: bool = True,
) -> IndexOutcome:
    """自带事务的完整索引（`reindex` 与回填脚本用）。

    抛错语义：切分/写库失败 → 异常向上抛（DB 已回滚）；向量失败 → 不抛，落在 outcome 上。
    """
    cfg = (cfg or ChunkConfig()).validate(for_custom=(cfg or ChunkConfig()).strategy == "custom")
    records, source_sha256 = build_chunk_records(material_id, class_id, body_text, cfg)

    with db.transaction():
        _deleted, inserted = store_chunks(material_id, class_id, records, source_sha256, cfg)

    outcome = index_vectors(
        material_id=material_id,
        records=records,
        chunk_ids=inserted,
        qdrant=qdrant,
        gateway_cfg=gateway_cfg,
        embed_transport=embed_transport,
        delete_vectors=delete_vectors,
    )
    outcome.class_id = int(class_id)
    outcome.chunk_strategy = cfg.strategy
    outcome.source_sha256 = source_sha256
    outcome.records = records
    return outcome


def needs_index(chunk_rows, source_sha256: str | None) -> bool:
    """回填判据：没有切片，或切片记录的 `source_sha256` 与当前正文不一致。

    这保证 `backfill_index.py` 真正幂等 —— 第二次运行既不会重复切片，也不会重复嵌入。
    """
    rows = list(chunk_rows or [])
    if not rows:
        return True
    return bool(source_sha256) and rows[0]["source_sha256"] != source_sha256


__all__ = [
    "IndexOutcome",
    "index_material",
    "index_vectors",
    "store_chunks",
    "needs_index",
    "STATUS_PENDING",
    "STATUS_INDEXING",
    "STATUS_INDEXED",
    "STATUS_FAILED",
]
