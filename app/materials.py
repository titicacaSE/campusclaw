"""材料列表、按 ID 访问与教师上传（规约 FR-02 / FR-03 / FR-04）

班级隔离要点（FR-03）：
  - class_id 一律取自 session，客户端传入的 class_id/class 字段被显式忽略；
  - 列表查询调用 db.list_materials(session["class_id"])，SQL 内强制 WHERE class_id = ?；
  - 按 ID 访问调用 db.get_material(material_id, session["class_id"])，
    不存在或跨班一律 404（实现择一，已在 README/design.md 固定为 404）。
"""
from __future__ import annotations

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

from . import db, knowledge
from .auth import login_required, teacher_required

log = logging.getLogger("campusclaw.materials")

bp = Blueprint("materials", __name__)


def _title_from(filename: str) -> str:
    return knowledge.safe_filename(filename).rsplit(".", 1)[0] or "未命名材料"


def _serialize(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "subject": row["subject"],
        "ext": row["ext"],
        "size_bytes": row["size_bytes"],
        "uploaded_by": row["uploaded_by"],
        "uploader_name": row["uploader_name"] if "uploader_name" in row.keys() else None,
        "created_at": row["created_at"],
        "entry_count": row["entry_count"] if "entry_count" in row.keys() else None,
        "class_id": row["class_id"],
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
    """本班材料列表：class_id 只来自 session（FR-03 S2/S3）。"""
    class_id = int(session["class_id"])
    rows = db.list_materials(class_id)
    klass = db.get_class(class_id)
    return jsonify(
        class_id=class_id,
        class_name=klass["name"] if klass else None,
        role=session["role"],
        total=len(rows),
        materials=[_serialize(row) for row in rows],
    )


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
    """落盘 → 解析 → 同事务双表写入（FR-04）。返回 (响应体, 状态码)。"""
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
        )
    except Exception:
        knowledge.discard(stored_path)
        raise

    return (
        {
            "material_id": material_id,
            "knowledge_entry_id": entry_id,
            "class_id": class_id,
            "title": title,
            "char_count": len(body_text),
        },
        201,
    )


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
