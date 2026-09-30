"""检索：三模式 + RRF 融合 + 阈值（design.md §5）

规约依据 `add-traceable-vector-retrieval`：
  - `keyword`：只查关系库，**不调嵌入、不访问 Qdrant**
  - `vector`：嵌入 → Qdrant 带 `class_id` pre-filter → 余弦 **< 0.35 丢弃**
  - `hybrid`：两路各自过滤 → **RRF，k = 60**；`1/(60+rank_kw) + 1/(60+rank_vec)`，
    缺席的一路不贡献分数（**不是分数相加**）
  - 班级过滤 MUST 在两条路径的候选生成之前；向量路径回表时**再带一次** `class_id`
  - Qdrant 不可用 → `vector` / `hybrid` 抛 `VectorStoreUnavailable`（接口层转 503），
    `keyword` 不受影响；**不编造相似度分数**
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import db, gateway
from .chunking import ChunkConfig
from .vector_store import QdrantClient, VectorStoreUnavailable

MODES = ("keyword", "vector", "hybrid")

RRF_K = 60                     # 融合常数（课件口径）
VECTOR_MIN_SCORE = 0.35        # 余弦下界，含等号
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
MAX_OFFSET = 1000
MAX_QUERY_CHARS = 300
VECTOR_CANDIDATES = 50         # 向量路召回候选上限（够做 RRF 名次）


class QueryError(ValueError):
    """输入不合法 → 接口层返回 400 invalid_query。"""

    code = "invalid_query"


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# ---------------------------------------------------------------- 输入校验


def validate_query(q, limit=DEFAULT_LIMIT, offset=0, mode="hybrid") -> tuple[str, int, int, str]:
    """校验并归一化检索输入。**不静默改写、不静默截断**（规约 Scenario 明确要求）。"""
    if mode not in MODES:
        raise QueryError(f"mode 必须是 {'/'.join(MODES)} 之一")

    text = q if isinstance(q, str) else ("" if q is None else str(q))
    text = text.strip()
    if not text:
        raise QueryError("q 不能为空")
    if _CONTROL_RE.search(text):
        raise QueryError("q 不能包含控制字符")
    if len(text) > MAX_QUERY_CHARS:
        raise QueryError(f"q 长度不能超过 {MAX_QUERY_CHARS} 个码点")

    def to_int(value, default, label):
        if value in (None, ""):
            return default
        if isinstance(value, bool):
            raise QueryError(f"{label} 必须是整数")
        try:
            return int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise QueryError(f"{label} 必须是整数") from exc

    limit_i = to_int(limit, DEFAULT_LIMIT, "limit")
    offset_i = to_int(offset, 0, "offset")
    if not (1 <= limit_i <= MAX_LIMIT):
        raise QueryError(f"limit 必须在 1..{MAX_LIMIT} 之间")
    if not (0 <= offset_i <= MAX_OFFSET):
        raise QueryError(f"offset 必须在 0..{MAX_OFFSET} 之间")
    return text, limit_i, offset_i, mode


# ---------------------------------------------------------------- 结果模型


@dataclass
class Hit:
    chunk_id: int
    material_id: int
    material_title: str
    chunk_index: int
    start_offset: int
    end_offset: int
    snippet: str
    line_start: int = 0
    line_end: int = 0
    match_in_chunk: int = 0
    hit_count: int = 0
    chunk_sha256: str = ""
    section: str | None = None
    rank_keyword: int | None = None
    rank_vector: int | None = None
    score_keyword: float | None = None
    score_vector: float | None = None
    score_final: float = 0.0

    def to_dict(self) -> dict:
        """课件接口契约的 hit 字段集（顺序即文档顺序）。"""
        return {
            "chunk_id": self.chunk_id,
            "material_id": self.material_id,
            "material_title": self.material_title,
            "chunk_index": self.chunk_index,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "snippet": self.snippet,
            "score_keyword": self.score_keyword,
            "score_vector": self.score_vector,
            "rank_keyword": self.rank_keyword,
            "rank_vector": self.rank_vector,
            "score_final": round(self.score_final, 8),
            # 便于页面与复核（不影响契约字段）
            "line_start": self.line_start,
            "line_end": self.line_end,
            "match_in_chunk": self.match_in_chunk,
            "hit_count": self.hit_count,
            "chunk_sha256": self.chunk_sha256,
            "section": self.section,
        }

    def to_legacy_dict(self) -> dict:
        """草案别名端点的历史字段名（保留以避免旧判据失效）。"""
        return {
            "chunk_id": self.chunk_id,
            "material_id": self.material_id,
            "title": self.material_title,
            "snippet": self.snippet,
            "snippet_char_start": self.start_offset,
            "char_start": self.start_offset,
            "char_end": self.end_offset,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "match_in_chunk": self.match_in_chunk,
            "hit_count": self.hit_count,
            "chunk_sha256": self.chunk_sha256,
            "source_url": f"/api/knowledge/sources/{self.chunk_id}",
            "download_url": f"/api/materials/{self.material_id}/download",
        }

    def to_citation(self) -> dict:
        """`/api/ask` 的 citation 字段集。"""
        return {
            "chunk_id": self.chunk_id,
            "material_title": self.material_title,
            "chunk_index": self.chunk_index,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "snippet": self.snippet,
        }


@dataclass
class RetrievalResult:
    query: str
    mode: str
    hits: list[Hit] = field(default_factory=list)
    total_matched: int = 0
    query_vector: list[float] | None = None
    degraded: bool = False
    index_warnings: list[dict] = field(default_factory=list)
    message: str | None = None

    @property
    def status(self) -> str:
        return "ok" if self.hits else "no_evidence"

    def to_dict(self) -> dict:
        payload = {
            "query": self.query,
            "mode": self.mode,
            "query_vector": self.query_vector,
            "hits": [h.to_dict() for h in self.hits],
            "total_matched": self.total_matched,
            "returned": len(self.hits),
            "status": self.status,
            "degraded": self.degraded,
            "index_warnings": self.index_warnings,
        }
        if self.status == "no_evidence":
            payload["message"] = self.message or NO_EVIDENCE_MESSAGE
        return payload


NO_EVIDENCE_MESSAGE = "资料中未找到相关内容"


def _rrf(rank: int) -> float:
    return 1.0 / (RRF_K + rank)


def _hit_from_row(row, *, match_in_chunk: int = 0, hit_count: int = 0) -> Hit:
    return Hit(
        chunk_id=int(row["id"]),
        material_id=int(row["material_id"]),
        material_title=row["material_title"],
        chunk_index=int(row["chunk_index"]),
        start_offset=int(row["start_offset"]),
        end_offset=int(row["end_offset"]),
        snippet=row["body_text"],
        line_start=int(row["line_start"]),
        line_end=int(row["line_end"]),
        match_in_chunk=int(row["match_in_chunk"] if "match_in_chunk" in row.keys() else match_in_chunk),
        hit_count=int(hit_count),
        chunk_sha256=row["chunk_sha256"],
        section=row["section"] if "section" in row.keys() else None,
    )


# ---------------------------------------------------------------- 单路候选


def keyword_candidates(class_id: int, q: str, *, limit: int = 10 ** 6):
    """关键字路径候选（已按班级限定）。返回 `(hit_count, match_in_chunk, row)` 列表，名次 1 起。"""
    window, total = db.search_chunks(class_id, q, limit=limit, offset=0)
    return window, total


def vector_candidates(
    class_id: int,
    q: str,
    *,
    qdrant: QdrantClient,
    cfg: gateway.GatewayConfig,
    embed_transport=None,
    top_k: int = VECTOR_CANDIDATES,
):
    """向量路径候选：嵌入 → Qdrant（class_id pre-filter + 0.35 阈值）→ **回表再带 class_id**。"""
    vectors = gateway.embed([q], cfg=cfg, transport=embed_transport)
    query_vector = vectors[0]
    points = qdrant.search(
        query_vector,
        class_id=class_id,
        limit=top_k,
        score_threshold=VECTOR_MIN_SCORE,
    )
    out = []
    for point in points:
        payload = point.get("payload") or {}
        chunk_id = payload.get("chunk_id", point.get("id"))
        row = db.get_chunk(int(chunk_id), class_id)   # ② 回表双保险：必须再带 class_id
        if row is None:
            continue
        score = float(point.get("score") or 0.0)
        if score < VECTOR_MIN_SCORE:                  # 双保险：阈值在本地再判一次
            continue
        out.append((score, row))
    return out, query_vector


# ---------------------------------------------------------------- 主入口


def retrieve(
    class_id: int,
    q: str,
    *,
    mode: str = "hybrid",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    qdrant: QdrantClient | None = None,
    cfg: gateway.GatewayConfig | None = None,
    embed_transport=None,
) -> RetrievalResult:
    """执行检索。输入已由 `validate_query` 校验过。"""
    result = RetrievalResult(query=q, mode=mode)

    warnings = db.index_warnings(class_id)
    result.index_warnings = [
        {"material_id": int(w["material_id"]), "status": w["index_status"], "title": w["title"]}
        for w in warnings
    ]
    result.degraded = bool(result.index_warnings)

    kw_rank: dict[int, tuple[int, int, object]] = {}
    if mode in ("keyword", "hybrid"):
        window, total = keyword_candidates(class_id, q)
        result.total_matched = total
        for idx, (count, match_at, row) in enumerate(window, start=1):
            kw_rank[int(row["id"])] = (idx, count, row, match_at)

    vec_rank: dict[int, tuple[int, float, object]] = {}
    if mode in ("vector", "hybrid"):
        if qdrant is None:
            raise VectorStoreUnavailable("向量库客户端未提供")
        cfg = cfg or gateway.GatewayConfig.from_app()
        candidates, query_vector = vector_candidates(
            class_id, q, qdrant=qdrant, cfg=cfg, embed_transport=embed_transport
        )
        result.query_vector = query_vector
        for idx, (score, row) in enumerate(candidates, start=1):
            vec_rank[int(row["id"])] = (idx, score, row)

    rows_by_id: dict[int, object] = {}
    for _idx, _count, row, _match in kw_rank.values():
        rows_by_id[int(row["id"])] = row
    for _idx, _score, row in vec_rank.values():
        rows_by_id.setdefault(int(row["id"]), row)

    hits: list[Hit] = []
    for chunk_id, row in rows_by_id.items():
        kw = kw_rank.get(chunk_id)
        vec = vec_rank.get(chunk_id)
        hit = _hit_from_row(
            row,
            match_in_chunk=kw[3] if kw else 0,
            hit_count=kw[1] if kw else 0,
        )
        score_final = 0.0
        if kw is not None:
            hit.rank_keyword = kw[0]
            hit.score_keyword = _rrf(kw[0])
            score_final += _rrf(kw[0])
        if vec is not None:
            hit.rank_vector = vec[0]
            hit.score_vector = vec[1]
            score_final += _rrf(vec[0])
        hit.score_final = score_final
        hits.append(hit)

    hits.sort(key=lambda h: (-h.score_final, h.material_id, h.chunk_index))
    result.total_matched = result.total_matched or len(hits)
    result.hits = hits[int(offset): int(offset) + int(limit)]
    return result


def build_chunk_records(
    material_id: int, class_id: int, body_text: str, cfg: ChunkConfig
) -> tuple[list[dict], str]:
    """把正文切成待入库的记录。返回 `(records, source_sha256)`。

    `source_sha256` 是**预处理后全文**的哈希，与 `materials.source_sha256` 同源（过期检测用）。
    """
    from .chunking import chunk_text, preprocess, sha256_text

    processed = preprocess(body_text, cfg)
    source_sha256 = sha256_text(processed)
    chunks = chunk_text(body_text, cfg)
    records = [
        {
            "material_id": material_id,
            "class_id": class_id,
            "chunk_index": c.chunk_index,
            "body_text": c.body_text,
            "start_offset": c.start_offset,
            "end_offset": c.end_offset,
            "line_start": c.line_start,
            "line_end": c.line_end,
            "chunk_sha256": c.chunk_sha256,
            "source_sha256": source_sha256,
            "section": c.section,
        }
        for c in chunks
    ]
    return records, source_sha256
