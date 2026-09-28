"""首页与健康检查（规约 NFR-02）"""
from __future__ import annotations

from flask import Blueprint, jsonify, redirect, session, url_for

from . import db

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("materials.page"))
    return redirect(url_for("auth.login"))


@bp.get("/health")
def health():
    """免登录；可选检测 SQLite 可读性，失败返回 503（NFR-02 S2）。"""
    db_ok = db.database_ok()
    payload = {
        "status": "ok" if db_ok else "error",
        "service": "campusclaw",
        "database": "ok" if db_ok else "unreadable",
    }
    return jsonify(payload), (200 if db_ok else 503)
