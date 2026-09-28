"""CampusClaw 应用工厂（Flask 3.x）

规约依据：
  - NFR-01 S3：未提供会话签名密钥时，应用启动 MUST 失败（禁止不安全默认值）。
  - NFR-02 S1/S2：标准启动为 Docker Compose；`GET /health` 免登录且不重定向。
  - FR-01 S3：未登录访问受保护资源 —— 页面 302 至 `/login`，API 返回 401 JSON。
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify

BASE_DIR = Path(__file__).resolve().parent.parent

MIN_SECRET_KEY_LEN = 16


class ConfigError(RuntimeError):
    """配置缺失/不安全 —— 直接阻止应用启动。"""


def _secret_key(overrides: dict) -> str:
    """SECRET_KEY 只允许来自环境变量（NFR-01）。"""
    value = overrides.get("SECRET_KEY") or os.environ.get("SECRET_KEY")
    if value is None or not str(value).strip():
        raise ConfigError(
            "缺少环境变量 SECRET_KEY：会话签名密钥仅允许通过服务端环境变量注入，"
            "禁止源码写死或不安全默认值（规约 NFR-01）。\n"
            "  本地运行：export SECRET_KEY=$(python -c \"import secrets;print(secrets.token_urlsafe(48))\")\n"
            "  Compose ：cp .env.example .env 并填写 SECRET_KEY"
        )
    value = str(value)
    if len(value) < MIN_SECRET_KEY_LEN:
        raise ConfigError(
            f"SECRET_KEY 长度不足 {MIN_SECRET_KEY_LEN} 字符，属于不安全默认值（规约 NFR-01）。"
        )
    return value


def create_app(**overrides) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    app.config.update(
        SECRET_KEY=_secret_key(overrides),
        DATABASE=str(
            overrides.get("DATABASE")
            or os.environ.get("DATABASE_PATH")
            or (BASE_DIR / "data" / "app.db")
        ),
        UPLOAD_DIR=str(
            overrides.get("UPLOAD_DIR") or os.environ.get("UPLOAD_DIR") or (BASE_DIR / "uploads")
        ),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_BYTES", 5 * 1024 * 1024)),
        # 会话 Cookie：HttpOnly + SameSite=Lax（规约 D2）
        SESSION_COOKIE_NAME="campusclaw_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH="/",
        TESTING=bool(overrides.get("TESTING", False)),
    )
    app.json.ensure_ascii = False

    from . import db

    # 容器/本地首次启动自动建表+种子（NFR-02：data/app.db 不存在则自动 init_db）
    db.ensure_ready(app)
    app.teardown_appcontext(db.close_conn)

    from .auth import bp as auth_bp
    from .main import bp as main_bp
    from .materials import bp as materials_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(materials_bp)

    @app.errorhandler(413)
    def too_large(_err):
        return (
            jsonify(error="payload_too_large", message="上传文件超过大小上限"),
            413,
        )

    @app.errorhandler(404)
    def not_found(_err):
        from flask import request

        if request.path.startswith("/api/"):
            return jsonify(error="not_found", message="资源不存在"), 404
        return "404 Not Found", 404

    return app
