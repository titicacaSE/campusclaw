"""CampusClaw 应用工厂（Flask 3.x）

规约依据：
  - NFR-01 S3：未提供会话签名密钥时，应用启动 MUST 失败（禁止不安全默认值）。
  - NFR-02 S1/S2：标准启动为 Docker Compose；`GET /health` 免登录且不重定向。
  - FR-01 S3：未登录访问受保护资源 —— 页面 302 至 `/login`，API 返回 401 JSON。
  - `add-traceable-vector-retrieval` T1：嵌入网关与向量库配置**缺失时不得阻止启动**，
    读接口按降级语义响应（`keyword` 照常可用，`vector`/`hybrid` 返回 503）。
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify

BASE_DIR = Path(__file__).resolve().parent.parent

MIN_SECRET_KEY_LEN = 16

# 网关与向量库相关配置：只从环境变量读取，缺失时留空并降级（密钥绝不下发浏览器）
UPSTREAM_CONFIG_KEYS = (
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "CHAT_BASE_URL",
    "CHAT_API_KEY",
    "CHAT_MODEL",
    "QDRANT_URL",
    "QDRANT_COLLECTION",
    "QDRANT_API_KEY",
    # add-agent-platform-mcp-tools：JWT 与 MCP 令牌同样只走环境变量
    "JWT_SECRET",
    "JWT_TTL",
    "MCP_SERVER_TOKEN",
    "MCP_BASE_URL",
    "MAX_TOOL_ROUNDS",
    "AGENT_HISTORY_LIMIT",
)


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


def _upstream_config(overrides: dict) -> dict:
    """网关/向量库配置：空值一律忽略，不覆盖已注入的值（T1：缺失不阻止启动）。"""
    cfg: dict = {}
    for key in UPSTREAM_CONFIG_KEYS:
        value = overrides.get(key) or os.environ.get(key)
        if value is not None and str(value).strip():
            cfg[key] = str(value)
    return cfg


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
    app.config.update(_upstream_config(overrides))
    # 测试注入点：传输层可替换（不落环境变量、不进镜像）
    for key in ("CHAT_TRANSPORT", "EMBED_TRANSPORT", "MCP_TRANSPORT", "QDRANT_TRANSPORT"):
        if key in overrides:
            app.config[key] = overrides[key]
    app.json.ensure_ascii = False

    from . import db

    # 容器/本地首次启动自动建表+种子（NFR-02：data/app.db 不存在则自动 init_db）
    db.ensure_ready(app)
    app.teardown_appcontext(db.close_conn)

    from .api_auth import bp as api_auth_bp
    from .auth import bp as auth_bp
    from .main import bp as main_bp
    from .materials import bp as materials_bp
    from .search import bp as search_bp

    # Bearer JWT：Bearer 优先于 Cookie，Bearer 无效不回落（design.md §2.2）
    from . import jwt_auth

    jwt_auth.install(app)

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_auth_bp)
    app.register_blueprint(materials_bp)
    app.register_blueprint(search_bp)

    # 智能体 / 技能 / 会话 / MCP / 文档
    from .agents import bp as agents_bp
    from .agent_skills import bp as agent_skills_bp
    from .agent_chat import bp as agent_chat_bp
    from .mcp_host import bp as mcp_host_bp
    from .mcp_service import bp as mcp_service_bp
    from .openapi import bp as openapi_bp
    from .pages import bp as pages_bp

    for module_bp in (
        agents_bp, agent_skills_bp, agent_chat_bp, mcp_host_bp, mcp_service_bp,
        openapi_bp, pages_bp,
    ):
        app.register_blueprint(module_bp)

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
