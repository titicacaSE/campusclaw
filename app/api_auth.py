"""会话契约的 JSON 端点：`POST /api/login`、`POST /api/logout`、`GET /api/me`（T9）

为什么单独一个模块而不是塞进 `auth.py`
------------------------------------
`auth.py` 承担的是「页面会话」（表单登录 + 302 跳转 + `login_required`/`teacher_required` 装饰器），
本模块只承担**课件接口契约**里的三个 JSON 端点。两者共用同一套会话写入函数
（`auth.login_session`）与同一枚 `HttpOnly` 会话 Cookie —— 也就是说：

  - 登录态**只**由 `Set-Cookie` 承载，不使用 bearer token，也不写 `localStorage`（T9 最后一条）
  - 失败响应**同形**：用户名不存在与口令错误返回完全相同的状态码与响应体，
    不泄露账号是否存在，也不回显任何哈希片段
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request, session

from . import db

log = logging.getLogger("campusclaw.api_auth")

bp = Blueprint("api_auth", __name__)

# 同形体：未知用户名与错误口令共用这一份响应，避免账号枚举
INVALID_CREDENTIALS = {"error": "invalid_credentials", "message": "用户名或密码不正确"}


def _credential_payload() -> tuple[str, str]:
    """从 JSON 正文或表单里取凭据（两种提交方式共用同一套解析）。"""
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    form = request.form or {}

    def pick(*names) -> str:
        for name in names:
            for source in (body, form):
                value = source.get(name) if hasattr(source, "get") else None
                if value not in (None, ""):
                    return str(value)
        return ""

    return pick("username", "user"), pick("password", "pass")


def _profile(row) -> dict:
    klass = db.get_class(int(row["class_id"]))
    return {
        "user_id": int(row["id"]),
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "class_id": int(row["class_id"]),
        "class_name": klass["name"] if klass else None,
    }


@bp.post("/api/login")
def api_login():
    """建立会话（Cookie）。成功 200 + `Set-Cookie`；失败 401 同形体，且不下发会话。"""
    # 延迟导入：auth 里只有一个模块级 Blueprint，避免循环导入
    from . import auth

    username, password = _credential_payload()
    row = db.get_user_by_username(username) if username and password else None

    if row is None or not auth.verify_password(password, row["password_hash"]):
        log.info("JSON 登录失败（username=%r）", username)
        return jsonify(INVALID_CREDENTIALS), 401

    auth.login_session(row)          # 与页面登录写同一套 session 键
    payload = _profile(row)
    payload["ok"] = True
    return jsonify(payload)


@bp.post("/api/logout")
def api_logout():
    """登出：清空会话并让 Cookie 失效。重复调用同样返回 200（幂等）。"""
    session.clear()
    payload = jsonify(ok=True, logged_out=True)
    payload.delete_cookie("campusclaw_session", path="/")
    return payload


@bp.get("/api/me")
def api_me():
    """当前会话画像。未登录 401，**不含任何材料内容**。"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(error="unauthorized", message="未登录"), 401

    row = db.get_user(int(user_id))
    if row is None:
        # 会话指向已删除的用户：按未登录处理并清空，避免留下悬空会话
        session.clear()
        return jsonify(error="unauthorized", message="未登录"), 401

    payload = _profile(row)
    payload["authenticated"] = True
    return jsonify(payload)
