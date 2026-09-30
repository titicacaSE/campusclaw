"""检索与问答的 HTTP 端点（规约 §5 / §6 / §7）

端点一览：
  - `POST /api/search`                        三模式检索（课件契约）
  - `POST /api/ask`                           简短问答（课件契约）
  - `GET  /api/knowledge/sources/<chunk_id>`  来源读取（草案别名，保留）
  - `GET  /api/knowledge/search`              检索（草案别名，保留）
  - `GET  /search`                            检索与问答页面

班级隔离纪律（本模块的唯一授权来源是 session）：
  - `class_id` / `role` 只取 `session`；
  - query / JSON 正文 / header 中的 `class_id`、`class`、`role`、`user_id` 一律**丢弃**；
  - 跨班检索对外是 **200 + 空 hits**，MUST NOT 用 403/404 暗示资料属于其他班级。
"""
from __future__ import annotations

import json
import logging

from flask import Blueprint, current_app, jsonify, render_template, request, session

from . import db, gateway, qa
from .auth import login_required
from .retrieval import QueryError, retrieve, validate_query
from .vector_store import VectorStoreUnavailable, client_for

log = logging.getLogger("campusclaw.search")

bp = Blueprint("search", __name__)

# 客户端可能用来"声明"班级的字段名 —— 一律读取后丢弃（保留在此以便审计与测试断言）
IGNORED_CLIENT_FIELDS = ("class_id", "class", "classId", "role", "user_id", "userId")


def _session_scope() -> tuple[int, str]:
    """唯一授权来源：会话。"""
    return int(session["class_id"]), str(session["role"])


def _request_payload() -> dict:
    """合并解析 JSON 正文与表单，便于两种提交方式共用同一套校验。"""
    payload: dict = {}
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        payload.update(body)
    if request.form:
        payload.update(request.form.to_dict())
    payload.update(request.args.to_dict())
    return payload


def _payload_scalar(payload: dict, *names):
    """按优先级取第一个非空值；浮点/整数原样返回。"""
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return None


def _error(code: str, message: str, status: int):
    return jsonify(error=code, message=message), status


def _log_cross_class_chunk(chunk_id: int, class_id: int) -> None:
    row = db.get_chunk_any(chunk_id)
    if row is not None and int(row["class_id"]) != int(class_id):
        log.warning(
            "跨班来源被拒：chunk_id=%s 属于班级 %s，来访会话班级 %s",
            chunk_id,
            row["class_id"],
            class_id,
        )


# --------------------------------------------------------------------- 检索


@bp.post("/api/search")
@login_required
def search():
    """三模式检索。班级范围只来自会话；客户端传入的班级字段被丢弃。"""
    class_id, role = _session_scope()
    payload = _request_payload()

    for field_name in IGNORED_CLIENT_FIELDS:
        if field_name in payload:
            log.info("丢弃客户端字段 %s（班级/角色只取自会话）", field_name)

    try:
        q, limit, offset, mode = validate_query(
            _payload_scalar(payload, "q", "query"),
            _payload_scalar(payload, "limit"),
            _payload_scalar(payload, "offset"),
            str(_payload_scalar(payload, "mode") or "hybrid"),
        )
    except QueryError as exc:
        return _error("invalid_query", str(exc), 400)

    try:
        result = retrieve(
            class_id,
            q,
            mode=mode,
            limit=limit,
            offset=offset,
            qdrant=client_for(current_app),
            cfg=gateway.GatewayConfig.from_app(current_app),
        )
    except VectorStoreUnavailable as exc:
        return _error("vector_store_unavailable", str(exc), 503)
    except gateway.EmbeddingUnavailable as exc:
        return _error("embedding_unavailable", str(exc), 503)
    except gateway.EmbeddingDimMismatch as exc:
        return _error("embedding_unavailable", str(exc), 503)

    body = result.to_dict()
    body["class_id"] = class_id
    body["role"] = role
    return jsonify(body)


# --------------------------------------------------------------------- 问答


@bp.post("/api/ask")
@login_required
def ask():
    """简短问答：先检索，再生成。无命中时不触碰对话网关。"""
    class_id, role = _session_scope()
    payload = _request_payload()

    for field_name in IGNORED_CLIENT_FIELDS:
        if field_name in payload:
            log.info("丢弃客户端字段 %s（班级/角色只取自会话）", field_name)

    try:
        q, limit, _offset, _mode = validate_query(
            _payload_scalar(payload, "q", "query"), 4, 0, "hybrid"
        )
    except QueryError as exc:
        return _error("invalid_query", str(exc), 400)

    try:
        result = qa.answer(
            class_id,
            q,
            payload.get("history"),
            qdrant=client_for(current_app),
            cfg=gateway.GatewayConfig.from_app(current_app),
        )
    except VectorStoreUnavailable as exc:
        return _error("vector_store_unavailable", str(exc), 503)
    except gateway.EmbeddingUnavailable as exc:
        return _error("embedding_unavailable", str(exc), 503)
    except gateway.EmbeddingDimMismatch as exc:
        return _error("embedding_unavailable", str(exc), 503)
    except gateway.ChatUnavailable as exc:
        # 不返回本地拼凑的答案
        return _error("upstream_error", str(exc), 502)

    body = dict(result)
    body["class_id"] = class_id
    body["role"] = role
    body["top_k"] = qa.TOP_K_CONTEXT
    return jsonify(body)


# --------------------------------------------------------------------- 来源


@bp.get("/api/knowledge/sources/<int:chunk_id>")
@login_required
def source(chunk_id: int):
    """来源读取：**独立重新鉴权 + 双条件 + 版本核对**。跨班与不存在**同形同体** 404。"""
    class_id = int(session["class_id"])
    row = db.get_chunk(chunk_id, class_id)
    if row is None:
        _log_cross_class_chunk(chunk_id, class_id)
        return _error("not_found", "来源不存在", 404)

    material_source = row["material_source_sha256"]
    if material_source and row["source_sha256"] != material_source:
        # 过期：不返回正文
        return _error("source_stale", "来源已过期，请重新检索", 409)

    return jsonify(
        chunk_id=int(row["id"]),
        material_id=int(row["material_id"]),
        material_title=row["material_title"],
        chunk_index=int(row["chunk_index"]),
        body_text=row["body_text"],
        start_offset=int(row["start_offset"]),
        end_offset=int(row["end_offset"]),
        line_start=int(row["line_start"]),
        line_end=int(row["line_end"]),
        chunk_sha256=row["chunk_sha256"],
        source_sha256=row["source_sha256"],
        section=row["section"],
        download_url=f"/api/materials/{int(row['material_id'])}/download",
    )


# --------------------------------------------------------------------- 草案别名


@bp.get("/api/knowledge/search")
@login_required
def legacy_search():
    """草案期端点：保留旧字段名，指向同一实现（旧判据因此仍然有效）。"""
    class_id, role = _session_scope()
    try:
        q, limit, _offset, _mode = validate_query(
            _payload_scalar(request.args.to_dict(), "q"),
            request.args.get("limit"),
            0,
            "keyword",
        )
    except QueryError as exc:
        return _error("invalid_query", str(exc), 400)

    result = retrieve(class_id, q, mode="keyword", limit=limit, offset=0)
    return jsonify(
        query=result.query,
        class_id=class_id,
        role=role,
        hit=bool(result.hits),
        total_matched=result.total_matched,
        returned=len(result.hits),
        limit=limit,
        degraded=result.degraded,
        index_warnings=result.index_warnings,
        coordinate_system={
            "unit": "unicode_code_point",
            "normalization": "NFC",
            "offset_base": "material_full_text",
            "offset_basis": "preprocessed_text",
            "snippet_object": "knowledge_chunk.body_text",
        },
        results=[h.to_legacy_dict() for h in result.hits],
    )


# --------------------------------------------------------------------- 页面


@bp.get("/search")
@login_required
def page():
    class_id = int(session["class_id"])
    klass = db.get_class(class_id)
    return render_template(
        "search.html",
        klass=klass["name"] if klass else "-",
        role=session["role"],
        display_name=session["display_name"],
        material_count=db.count_materials(class_id),
        chunk_count=db.count_chunks(class_id),
    )
