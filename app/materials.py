"""材料列表、按 ID 访问与教师上传（规约 FR-02 / FR-03 / FR-04）

班级隔离要点（FR-03）：
  - class_id 一律取自 session，客户端传入的 class_id/class 字段被显式忽略；
  - 列表查询调用 db.list_materials(session["class_id"])，SQL 内强制 WHERE class_id = ?；
  - 按 ID 访问调用 db.get_material(material_id, session["class_id"])，
    不存在或跨班一律 404（实现择一，已在 README/design.md 固定为 404）。

本变更追加（`add-traceable-vector-retrieval`）：
  - `_perform_upload()` 同事务写入切片，提交后再做嵌入与写向量（T8）
  - `POST /api/materials/<id>/reindex` 重建切片与向量（教师；跨班 404；参数越界 400）
  - `GET  /api/materials/<id>/vectors` 对照关系库与向量库（**不回传向量分量**）
  - `GET  /api/materials` 支持 `page` / `page_size=20` / `title`
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import db, gateway, indexing, knowledge
from .auth import login_required, teacher_required
from .chunking import ChunkConfig, ChunkError
from .retrieval import build_chunk_records
from .vector_store import VectorStoreUnavailable, client_for

log = logging.getLogger("campusclaw.materials")

bp = Blueprint("materials", __name__)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _title_from(filename: str) -> str:
    return knowledge.safe_filename(filename).rsplit(".", 1)[0] or "未命名材料"


def _serialize(row) -> dict:
    keys = row.keys()

    def opt(name, default=None):
        return row[name] if name in keys else default

    return {
        "id": row["id"],
        "title": row["title"],
        "subject": row["subject"],
        "ext": row["ext"],
        "size_bytes": row["size_bytes"],
        "uploaded_by": row["uploaded_by"],
        "uploader_name": opt("uploader_name"),
        "created_at": row["created_at"],
        "entry_count": opt("entry_count"),
        "class_id": row["class_id"],
        # 索引侧字段（本变更追加）：既有断言不受影响
        "index_status": opt("index_status", "pending"),
        "chunk_strategy": opt("chunk_strategy", "auto"),
        "chunk_count": opt("chunk_count"),
        "indexed_at": opt("indexed_at"),
        "source_sha256": opt("source_sha256"),
    }


# --------------------------------------------------------------------- 页面

@bp.get("/materials")
@login_required
def page():
    class_id = int(session["class_id"])
    materials = db.list_materials(class_id)
    klass = db.get_class(class_id)
    return render_template(
        "materials.html",
        materials=[_serialize(row) for row in materials],
        klass=klass["name"] if klass else "-",
        role=session["role"],
        display_name=session["display_name"],
        can_upload=session["role"] == "teacher",
    )


# --------------------------------------------------------------------- API

@bp.get("/api/materials")
@login_required
def api_list():
    """本班材料列表：class_id 只来自 session（FR-03 S2/S3）。

    分页参数（T9）：`page`（1 起）、`page_size`（默认 20，上限 100）、`title`（子串筛选）。
    不传分页参数时 `materials` 仍是**本班全量**，`total` 仍是**本班总数** —— 既有断言不变；
    额外追加 `page` / `page_size` / `returned` / `has_more` 供新页面使用。
    """
    class_id = int(session["class_id"])
    klass = db.get_class(class_id)

    raw_page = request.args.get("page")
    raw_size = request.args.get("page_size")
    title = (request.args.get("title") or "").strip() or None
    paged = bool(raw_page or raw_size or title)

    if paged:
        page = _positive_int(raw_page, 1, "page")
        page_size = _positive_int(raw_size, DEFAULT_PAGE_SIZE, "page_size")
        if page < 1 or page_size < 1 or page_size > MAX_PAGE_SIZE:
            return (
                jsonify(
                    error="invalid_query",
                    message=f"page 必须 ≥ 1，page_size 必须在 1..{MAX_PAGE_SIZE} 之间",
                ),
                400,
            )
        rows = db.list_materials_paged(class_id, page=page, page_size=page_size, title=title)
        total = db.count_materials_filtered(class_id, title=title)
    else:
        page, page_size = 1, DEFAULT_PAGE_SIZE
        rows = db.list_materials(class_id)
        total = len(rows)

    materials = [_serialize(row) for row in rows]
    return jsonify(
        class_id=class_id,
        class_name=klass["name"] if klass else None,
        role=session["role"],
        total=total,
        page=page,
        page_size=page_size,
        returned=len(materials),
        has_more=paged and (page * page_size < total),
        title=title,
        materials=materials,
    )


def _positive_int(raw, default: int, label: str) -> int:
    """查询串整数参数解析。缺省回落为默认值；非整数返回 -1，交由调用方给出 400。"""
    if raw in (None, ""):
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return -1


@bp.get("/api/materials/<int:material_id>")
@login_required
def api_detail(material_id: int):
    """按 ID 访问：先查行 → 不存在 404 → class 不匹配同样 404（不泄露跨班材料存在性）。"""
    class_id = int(session["class_id"])
    row = db.get_material(material_id, class_id)
    if row is None:
        _log_cross_class_attempt(material_id, class_id)
        return jsonify(error="not_found", message="材料不存在"), 404

    entries = db.list_knowledge_entries(material_id, class_id)
    payload = _serialize(row)
    payload["knowledge_entries"] = [
        {"id": e["id"], "ord": e["ord"], "char_count": e["char_count"], "body_text": e["body_text"]}
        for e in entries
    ]
    return jsonify(payload)


@bp.get("/api/materials/<int:material_id>/download")
@login_required
def download(material_id: int):
    class_id = int(session["class_id"])
    row = db.get_material(material_id, class_id)
    if row is None:
        _log_cross_class_attempt(material_id, class_id)
        return jsonify(error="not_found", message="材料不存在"), 404

    # DB 有行但磁盘文件缺失/被移走：属数据完整性问题，给 4xx 而不是让 send_file 抛 500
    if not Path(row["file_path"]).is_file():
        log.error(
            "材料文件缺失：material_id=%s file_path=%s", material_id, row["file_path"]
        )
        return jsonify(error="file_missing", message="材料文件不可用"), 404

    try:
        return send_file(
            row["file_path"],
            as_attachment=True,
            download_name=row["original_filename"] or row["title"],
        )
    except OSError:
        # 竞态兜底：is_file 通过后被并发删除
        log.error(
            "材料文件读取失败：material_id=%s file_path=%s", material_id, row["file_path"]
        )
        return jsonify(error="file_missing", message="材料文件不可用"), 404


@bp.post("/api/materials/upload")
@teacher_required
def upload():
    """教师上传（JSON API，规约 FR-04 指定的路径）。"""
    payload, status = _perform_upload()
    return jsonify(payload), status


@bp.post("/materials/upload")
@teacher_required
def upload_form():
    """同一套逻辑的页面入口：成功后回到列表页（上传后列表 MUST 可见新记录）。"""
    payload, status = _perform_upload()
    if status == 201:
        flash(f"已写入本班知识库：{payload['title']}")
    else:
        flash(payload.get("message") or "上传失败")
    return redirect(url_for("materials.page"))


def _perform_upload() -> tuple[dict, int]:
    """落盘 → 解析 → 切分 → 同事务三表写入 → 提交后嵌入与写向量（FR-04 + T8）。"""
    # FR-03：写入时 class_id 只取自 session，忽略/拒绝表单中的 class 字段
    class_id = int(session["class_id"])
    user_id = int(session["user_id"])

    file = request.files.get("file")
    if file is None or not (file.filename or "").strip():
        return {"error": "missing_file", "message": "请通过 file 字段上传文件"}, 400

    original_filename = knowledge.safe_filename(file.filename)
    upload_dir = current_app.config["UPLOAD_DIR"]
    max_bytes = int(current_app.config["MAX_CONTENT_LENGTH"])

    try:
        ext = knowledge.validate_extension(original_filename)
    except knowledge.UploadError as exc:
        return {"error": "unsupported_upload", "message": str(exc)}, 400

    # 切分配置在**落盘之前**校验：越界直接 400，连文件都不写（T8 参数越界用例）
    try:
        chunk_cfg = ChunkConfig.from_request(request.form or {})
    except ChunkError as exc:
        return {"error": "invalid_query", "message": str(exc)}, 400

    # 先按声明长度兜底，避免把超大文件写进磁盘
    if request.content_length and request.content_length > max_bytes:
        return {
            "error": "file_too_large",
            "message": f"文件超过大小上限 {max_bytes} 字节",
        }, 400

    stored_path, size = knowledge.store_upload(upload_dir, class_id, original_filename, file)

    if size <= 0:
        knowledge.discard(stored_path)
        return {"error": "empty_file", "message": "文件内容为空"}, 400
    if size > max_bytes:
        knowledge.discard(stored_path)
        return {
            "error": "file_too_large",
            "message": f"文件超过大小上限 {max_bytes} 字节",
        }, 400

    try:
        body_text = knowledge.parse_file(stored_path)
    except knowledge.UploadError as exc:
        knowledge.discard(stored_path)  # 失败则删文件，返回 400，无 DB 行
        return {"error": "parse_failed", "message": str(exc)}, 400

    # 切分也放在写库之前：抛错时不会留下任何一行、也不留孤立文件
    try:
        chunk_records, source_sha256 = build_chunk_records(0, class_id, body_text, chunk_cfg)
    except ChunkError as exc:
        knowledge.discard(stored_path)
        return {"error": "invalid_query", "message": str(exc)}, 400

    title = (request.form.get("title") or "").strip() or _title_from(original_filename)
    subject = (request.form.get("subject") or "").strip() or None

    try:
        material_id, entry_id = knowledge.persist_material(
            title=title,
            class_id=class_id,
            file_path=str(stored_path),
            original_filename=original_filename,
            ext=ext,
            size_bytes=size,
            uploaded_by=user_id,
            body_text=body_text,
            subject=subject,
            chunk_records=chunk_records,
            chunk_source_sha256=source_sha256,
            chunk_strategy=chunk_cfg.strategy,
            chunk_config_json=json.dumps(chunk_cfg.as_dict(), ensure_ascii=False, sort_keys=True),
        )
    except Exception:
        knowledge.discard(stored_path)
        raise

    # 事务已提交：现在做嵌入与写向量。失败只影响 index_status，正文已可用。
    material_id_from_records = material_id
    for record in chunk_records:
        record["material_id"] = material_id_from_records
    try:
        inserted = [int(r["id"]) for r in db.list_chunks(class_id, material_id)]
    except Exception:  # pragma: no cover - 仅在极端异常下触发
        inserted = []
    outcome = indexing.index_vectors(
        material_id=material_id,
        records=chunk_records,
        chunk_ids=inserted,
        qdrant=client_for(current_app),
        gateway_cfg=gateway.GatewayConfig.from_app(current_app),
    )

    payload = {
        "material_id": material_id,
        "knowledge_entry_id": entry_id,
        "class_id": class_id,
        "title": title,
        "char_count": len(body_text),
        "chunk_count": outcome.chunk_count,
        "chunk_strategy": chunk_cfg.strategy,
        "index_status": outcome.index_status,
        "vectors_written": outcome.vectors_written,
    }
    if outcome.vector_error:
        payload["vector_error"] = outcome.vector_error
    return payload, 201


@bp.post("/api/materials/<int:material_id>/reindex")
@login_required
def reindex(material_id: int):
    """重建某材料的切片与向量（T8）。

    顺序纪律：
      1. 角色：非教师 → 403，并**明确给出** `required_role` / `your_role`
      2. 班级：`db.get_material(material_id, session_class)` 查不到 → 404（跨班与不存在同形）
      3. 参数：`chunk_*` 越界 → 400（`ChunkConfig` 不静默夹取）
      4. 动作：先删旧切片与旧向量，再写新的；`chunk_count`/`chunk_strategy` 反映新策略
    """
    class_id = int(session["class_id"])
    role = str(session["role"])
    if role != "teacher":
        return (
            jsonify(
                error="forbidden",
                message="无权限：仅教师可重建本班材料的索引",
                required_role="teacher",
                your_role=role,
            ),
            403,
        )

    row = db.get_material(material_id, class_id)
    if row is None:
        _log_cross_class_attempt(material_id, class_id)
        return jsonify(error="not_found", message="材料不存在"), 404

    payload = _request_payload()
    try:
        cfg = ChunkConfig.from_request(payload)
    except ChunkError as exc:
        return jsonify(error="invalid_query", message=str(exc)), 400

    body_text = _material_body_text(material_id, class_id, row)
    if not body_text.strip():
        return jsonify(error="empty_material", message="材料正文为空，无法重建索引"), 400

    try:
        outcome = indexing.index_material(
            material_id=material_id,
            class_id=class_id,
            body_text=body_text,
            cfg=cfg,
            qdrant=client_for(current_app),
            gateway_cfg=gateway.GatewayConfig.from_app(current_app),
        )
    except ChunkError as exc:
        return jsonify(error="invalid_query", message=str(exc)), 400

    body = outcome.to_dict()
    body["reindexed"] = True
    body["title"] = row["title"]
    return jsonify(body)


@bp.get("/api/materials/<int:material_id>/vectors")
@login_required
def vectors(material_id: int):
    """关系库切片 ↔ 向量库记录对照（T9）。**只返回标识与哈希，不返回任何向量分量。**"""
    class_id = int(session["class_id"])
    row = db.get_material(material_id, class_id)
    if row is None:
        _log_cross_class_attempt(material_id, class_id)
        return jsonify(error="not_found", message="材料不存在"), 404

    chunk_rows = db.list_chunks(class_id, material_id)
    try:
        points = client_for(current_app).points_by_material(material_id)
        vector_store = "ok"
    except VectorStoreUnavailable as exc:
        points = []
        vector_store = "unavailable"
        log.warning("读取向量库失败（material_id=%s）：%s", material_id, exc)

    present: set[int] = set()
    for point in points:
        payload = point.get("payload") or {}
        try:
            present.add(int(payload.get("chunk_id", point.get("id"))))
        except (TypeError, ValueError):
            continue

    keys = row.keys()
    body = {
        "material_id": int(row["id"]),
        "class_id": int(row["class_id"]),
        "title": row["title"],
        "index_status": row["index_status"] if "index_status" in keys else "pending",
        "chunk_strategy": row["chunk_strategy"] if "chunk_strategy" in keys else "auto",
        "indexed_at": row["indexed_at"] if "indexed_at" in keys else None,
        "source_sha256": row["source_sha256"] if "source_sha256" in keys else None,
        "vector_store": vector_store,
        "chunk_count": len(chunk_rows),
        "vector_count": len(points),
        "consistent": len(chunk_rows) == len(points) and all(
            int(c["id"]) in present for c in chunk_rows
        ),
        "chunks": [
            {
                "chunk_id": int(c["id"]),
                "chunk_index": int(c["chunk_index"]),
                "chunk_sha256": c["chunk_sha256"],
                "source_sha256": c["source_sha256"],
                "index_status": c["index_status"],
                "char_count": int(c["char_count"]),
                "start_offset": int(c["start_offset"]),
                "end_offset": int(c["end_offset"]),
                "in_vector_store": int(c["id"]) in present,
                # 说明：这里刻意**不返回**向量分量（规约：页面与响应都不暴露向量）
            }
            for c in chunk_rows
        ],
    }
    return jsonify(body)


def _request_payload() -> dict:
    """合并 JSON 正文、表单与查询串，便于三种提交方式共用同一套校验。"""
    payload: dict = {}
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        payload.update(body)
    if request.form:
        payload.update(request.form.to_dict())
    payload.update(request.args.to_dict())
    return payload


def _material_body_text(material_id: int, class_id: int, row) -> str:
    """重建索引的正文来源：优先关系库里的知识条目，缺失时回读落盘文件。"""
    entries = db.list_knowledge_entries(material_id, class_id)
    text = "\n\n".join(e["body_text"] for e in entries if e["body_text"])
    if text.strip():
        return text
    try:
        return knowledge.parse_file(row["file_path"])
    except (knowledge.UploadError, OSError):
        return ""


def _log_cross_class_attempt(material_id: int, session_class_id: int) -> None:
    """跨班/不存在的按 ID 访问：记录告警，便于审计（响应仍是 404，不泄露信息）。"""
    row = db.get_material_any(material_id)
    if row is not None and row["class_id"] != session_class_id:
        log.warning(
            "跨班访问被拒：material_id=%s 属于班级 %s，来访会话班级 %s",
            material_id,
            row["class_id"],
            session_class_id,
        )
