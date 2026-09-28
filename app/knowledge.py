"""材料解析与知识库入库（规约 FR-04 / D5）

数据流（与 spec FR-04 的伪代码一致）：
  Client (teacher, multipart/form-data)
  POST /api/materials/upload
  ├─ login_required + role == teacher，否则 403
  ├─ 校验扩展名/大小（MVP：.txt、.md）
  ├─ 保存 uploads/{class_id}/{uuid}_{safe_filename}
  ├─ parse → plain text（失败则删文件，返回 400，无 DB 行）
  ├─ BEGIN TRANSACTION
  │   INSERT materials (title, class_id, file_path, uploaded_by, ...)
  │   INSERT knowledge_entries (material_id, class_id, body_text, ...)
  ├─ COMMIT
  └─ 201 { "material_id": ... }
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from . import db

# D5：MVP 仅支持 .txt / .md
ALLOWED_EXTENSIONS = (".txt", ".md")

_UNSAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


class UploadError(ValueError):
    """上传/解析阶段的失败 → HTTP 400，且不产生任何 DB 行。"""


def safe_filename(filename: str, fallback: str = "material") -> str:
    """清洗文件名，去掉路径分隔符与控制字符，防止目录穿越。"""
    name = Path(filename or "").name.strip()
    name = name.replace("\\", "_").replace("/", "_")
    name = _UNSAFE.sub("_", name).strip("._")
    if not name:
        name = fallback
    return name[:120]


def extension_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def validate_extension(filename: str) -> str:
    """校验扩展名（在落盘前调用），返回规范化后缀。"""
    ext = extension_of(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadError(
            f"不支持的文件类型「{ext or '未知'}」，MVP 仅支持 {'、'.join(ALLOWED_EXTENSIONS)}"
        )
    return ext


def store_upload(
    upload_dir: str | Path, class_id: int, filename: str, file_obj
) -> tuple[Path, int]:
    """保存到 uploads/{class_id}/{uuid}_{safe_filename}，返回（落盘路径, 字节数）。

    `file_obj` 既接受 werkzeug 的 `FileStorage`（有 `.save()`），也接受裸文件对象
    （`SpooledTemporaryFile` / `BytesIO`），后者按块拷贝 —— 避免 `file.stream`
    这类没有 `.save()` 的底层流直接把上传打成 500。
    """
    dest_dir = Path(upload_dir) / str(class_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{uuid.uuid4().hex[:12]}_{safe_filename(filename)}"
    if hasattr(file_obj, "save"):  # werkzeug FileStorage
        file_obj.save(target)
    else:  # 裸流
        with open(target, "wb") as fh:
            shutil.copyfileobj(file_obj, fh)
    return target, target.stat().st_size


def parse_file(path: str | Path) -> str:
    """把落盘文件解析为纯文本；失败抛 UploadError（调用方负责删除文件）。"""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadError(
            "文件不是有效的 UTF-8 文本（可能是损坏文件或 GBK 编码），已放弃入库"
        ) from exc

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise UploadError("文件解析后内容为空")
    return text


def persist_material(
    *,
    title: str,
    class_id: int,
    file_path: str,
    original_filename: str,
    ext: str,
    size_bytes: int,
    uploaded_by: int,
    body_text: str,
    subject: str | None = None,
) -> tuple[int, int]:
    """同事务写入 materials 与 knowledge_entries（FR-04：两表各增 1 行且 class 一致）。"""
    with db.transaction():
        material_id = db.insert_material(
            title=title,
            class_id=class_id,
            file_path=file_path,
            original_filename=original_filename,
            ext=ext,
            size_bytes=size_bytes,
            uploaded_by=uploaded_by,
            subject=subject,
            parse_status="ok",
        )
        entry_id = db.insert_knowledge_entry(
            material_id=material_id, class_id=class_id, body_text=body_text, ord_=0
        )
    return material_id, entry_id


def discard(path: str | Path) -> None:
    """失败路径：删除落盘文件，避免出现孤立未关联文件（FR-04 S3）。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
