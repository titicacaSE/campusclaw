"""会话契约与令牌契约的 JSON 端点。

两条通道（design.md §2.2）：

  - **Cookie 会话**：`POST /api/login` / `POST /api/logout` / `GET /api/me`。
    登录态只由 `Set-Cookie` 承载，响应体**不含**令牌字段 —— 旧断言
    `no-token-in-storage` 仍然成立。
  - **Bearer JWT**：`POST /api/auth/token` 签发、`POST /api/auth/revoke` 撤销、
    `GET /api/auth/me` 回身份（第 5 课 T5 要求）。

两条通道共用同一套「回库读角色与班级」的身份解析，因此改库后下一次请求即生效。

失败响应**同形**：用户名不存在与口令错误返回完全相同的状态码与响应体，
不泄露账号是否存在，也不回显任何哈希片段。
"""
from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, request, session

from . import db, jwt_auth

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
    payload["auth_mode"] = getattr(g, "auth_mode", "cookie")
    return jsonify(payload)


# --------------------------------------------------------------------- Bearer JWT


@bp.post("/api/auth/token")
def api_token():
    """签发 HS256 JWT。载荷只含 `sub/jti/iat/exp` —— 角色与班级不进令牌。"""
    from . import auth

    username, password = _credential_payload()
    row = db.get_user_by_username(username) if username and password else None
    if row is None or not auth.verify_password(password, row["password_hash"]):
        log.info("令牌签发失败（username=%r）", username)
        return jsonify(INVALID_CREDENTIALS), 401

    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    try:
        ttl = int(body.get("ttl") or jwt_auth.DEFAULT_TTL)
    except (TypeError, ValueError):
        return jsonify(error="invalid_request", message="ttl 必须是整数"), 400

    issued = jwt_auth.encode(int(row["id"]), secret=jwt_auth.secret_of(app_of()), ttl=ttl)
    log.info("签发 JWT：user_id=%s role=%s（角色不入令牌）", row["id"], row["role"])
    return jsonify(
        token=issued["token"],
        token_type="Bearer",
        expires_in=issued["expires_in"],
        claims=issued["claims"],          # 便于课堂核验：可见 payload 里没有 role/class_id
        user=_profile(row),
    )


@bp.post("/api/auth/revoke")
def api_revoke():
    """撤销当前 Bearer 令牌（幂等）。"""
    claims = getattr(g, "jwt_claims", None)
    if not claims:
        return jsonify(error="invalid_request", message="该端点只接受 Bearer 令牌"), 400
    jwt_auth.revoke(claims["jti"], int(claims["sub"]), int(claims["exp"]))
    session.clear()
    return jsonify(ok=True, revoked=True, jti=claims["jti"])


@bp.get("/api/auth/me")
def api_auth_me():
    """回身份：来源是库，不是令牌载荷。"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(error="unauthorized", message="未登录"), 401
    row = db.get_user(int(user_id))
    if row is None:
        session.clear()
        return jsonify(error="unauthorized", message="未登录"), 401
    payload = _profile(row)
    payload["auth_mode"] = getattr(g, "auth_mode", "cookie")
    payload["claims"] = {
        key: value for key, value in (getattr(g, "jwt_claims", None) or {}).items()
        if key in ("sub", "jti", "iat", "exp")
    }
    return jsonify(payload)


def app_of():
    """当前应用的轻量包装，避免在函数签名里反复引入 flask.current_app。"""
    from flask import current_app

    return current_app._get_current_object()
