"""Bearer JWT（HS256）—— 第 5 课 T5 要求的接口鉴权形态。

设计要点（见 design.md §2）：
  - 算法固定 HS256；`alg` 不是字面 `HS256` 一律拒绝，且**在查用户表之前**拒绝；
  - 载荷只放 `sub / jti / iat / exp`，**角色与班级不进令牌**，每次请求回库读；
  - `jti` 可撤销（`token_revocations`）；
  - 请求同时带 Bearer 与 Cookie 时 Bearer 优先，Bearer 无效即 401，不回落 Cookie。

零第三方依赖：只用标准库 `hmac` / `hashlib` / `base64` / `json`。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from flask import g, jsonify, request, session

ALG = "HS256"
DEFAULT_TTL = 3600
MAX_TTL = 24 * 3600
CLOCK_SKEW = 0


class JwtError(ValueError):
    """令牌不合法。`reason` 供日志与测试观测，不作为对外文案。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------------ 编解码


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def secret_of(app=None) -> str:
    """签名密钥：优先 `JWT_SECRET`，回落到 `SECRET_KEY`（二者都只在服务端）。"""
    if app is not None:
        value = app.config.get("JWT_SECRET") or app.config.get("SECRET_KEY")
        if value:
            return str(value)
    return str(os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY") or "")


def encode(user_id: int, *, secret: str, ttl: int = DEFAULT_TTL, now: int | None = None) -> dict:
    if not secret:
        raise JwtError("no_secret")
    ttl = max(1, min(int(ttl or DEFAULT_TTL), MAX_TTL))
    issued = int(now if now is not None else time.time())
    claims = {
        "sub": str(int(user_id)),
        "jti": secrets.token_hex(16),
        "iat": issued,
        "exp": issued + ttl,
    }
    header = {"alg": ALG, "typ": "JWT"}
    head = _b64(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode())
    body = _b64(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode())
    signing_input = f"{head}.{body}".encode("ascii")
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return {"token": f"{head}.{body}.{_b64(sig)}", "claims": claims, "expires_in": ttl}


def decode(token: str, *, secret: str, now: int | None = None) -> dict:
    """验签并返回 claims。任何一步失败抛 `JwtError`。"""
    if not secret:
        raise JwtError("no_secret")
    if not isinstance(token, str) or token.count(".") != 2:
        raise JwtError("bad_format")
    head_b64, body_b64, sig_b64 = token.split(".")

    try:
        header = json.loads(_unb64(head_b64))
    except Exception:  # noqa: BLE001
        raise JwtError("bad_header") from None
    if not isinstance(header, dict) or header.get("alg") != ALG:
        # `none`、`HS384`、缺失 一律在这里被拦下 —— 此时尚未查库
        raise JwtError("bad_alg")

    expected = hmac.new(
        secret.encode(), f"{head_b64}.{body_b64}".encode("ascii"), hashlib.sha256
    ).digest()
    try:
        actual = _unb64(sig_b64)
    except Exception:  # noqa: BLE001
        raise JwtError("bad_signature") from None
    if not hmac.compare_digest(expected, actual):
        raise JwtError("bad_signature")

    try:
        claims = json.loads(_unb64(body_b64))
    except Exception:  # noqa: BLE001
        raise JwtError("bad_payload") from None
    if not isinstance(claims, dict):
        raise JwtError("bad_payload")
    for key in ("sub", "jti", "iat", "exp"):
        if key not in claims:
            raise JwtError("missing_claim")
    stamp = int(now if now is not None else time.time())
    try:
        exp = int(claims["exp"])
        int(claims["iat"])
    except (TypeError, ValueError):
        raise JwtError("bad_payload") from None
    if exp + CLOCK_SKEW < stamp:
        raise JwtError("expired")
    return claims


# ------------------------------------------------------------------ 撤销


def revoke(jti: str, user_id: int, expires_at: int) -> None:
    from . import db

    db.execute(
        "INSERT OR REPLACE INTO token_revocations (jti, user_id, expires_at, revoked_at)"
        " VALUES (?, ?, ?, ?)",
        (str(jti), int(user_id), int(expires_at), db.utcnow()),
    )
    db.get_conn().commit()


def is_revoked(jti: str) -> bool:
    from . import db

    row = db.query_one("SELECT jti FROM token_revocations WHERE jti = ?", (str(jti),))
    return row is not None


# ------------------------------------------------------------------ 请求身份

PUBLIC_PREFIXES = (
    "/health",
    "/static/",
    "/login",
    "/api/login",
    "/api/auth/token",
    "/api/docs",
    "/api/openapi.json",
    "/mcp",          # MCP 服务用独立令牌，见 mcp_service.py
)


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PREFIXES)


def bearer_token() -> str | None:
    raw = request.headers.get("Authorization")
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def resolve_identity(app) -> None:
    """before_request：Bearer 存在则必须有效；角色与班级回库读。"""
    g.jwt_claims = None
    raw = bearer_token()
    if raw is None:
        return                      # 无 Bearer → 交给 Cookie 会话（页面链路）

    def reject(message: str = "未登录或令牌无效"):
        g.auth_reject_reason = "unauthorized"
        resp = jsonify(error="unauthorized", message=message)
        resp.status_code = 401
        resp.headers["WWW-Authenticate"] = 'Bearer realm="campusclaw"'
        return resp

    if not raw:
        g.auth_reject_reason = "bad_format"
        return reject("Authorization 头格式应为 Bearer <token>")

    try:
        claims = decode(raw, secret=secret_of(app))
    except JwtError as exc:
        g.auth_reject_reason = exc.reason
        return reject("令牌无效或已过期")

    if is_revoked(claims["jti"]):
        g.auth_reject_reason = "token_revoked"
        return reject("令牌已被撤销")

    from . import db

    row = db.get_user(int(claims["sub"]))
    if row is None:
        g.auth_reject_reason = "unknown_user"
        return reject("令牌对应的用户不存在")

    # 角色与班级只从库里取，因此改库后下一次请求立即生效
    from .auth import SESSION_KEYS

    session.clear()
    session["user_id"] = int(row["id"])
    session["username"] = row["username"]
    session["display_name"] = row["display_name"]
    session["role"] = row["role"]
    session["class_id"] = int(row["class_id"])
    session.permanent = False
    g.jwt_claims = claims
    g.identity = {key: session.get(key) for key in SESSION_KEYS}
    g.auth_mode = "bearer"
    return None


def install(app) -> None:
    app.before_request(lambda: None if _is_public(request.path) else resolve_identity(app))
