"""登录、会话与密码（规约 FR-01 / FR-02 / NFR-01 / D2 / D3）

规约依据：
  - FR-01：账号密码登录；登录成功 MUST 建立服务端会话；未登录访问受保护页面 MUST 被引导到登录页，
    且 MUST NOT 泄露任何业务数据。
  - FR-01 S3：页面请求 → 重定向登录页；API 请求 → HTTP 401。
  - FR-02：按会话中的角色授权；学生调用上传或管理接口 MUST 被拒绝（403）。
  - NFR-01：使用 bcrypt 单向哈希；登录走库提供的 verify，比较具备恒定时间语义。
  - D2：Flask 服务端 signed cookie session（HttpOnly、SameSite=Lax）。
"""
from __future__ import annotations

from functools import wraps

import bcrypt
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from . import db

bp = Blueprint("auth", __name__)

# 会话 payload 至少含 user_id、role、class_id（规约 §3）
SESSION_KEYS = ("user_id", "username", "display_name", "role", "class_id")


# --------------------------------------------------------------------- 密码（NFR-01）

def hash_password(plain: str) -> str:
    """bcrypt 单向哈希；数据库中不出现明文。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    """bcrypt 提供的校验，比较具备恒定时间语义。"""
    if not plain or not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


# --------------------------------------------------------------------- 会话

def current_user() -> dict | None:
    """从会话读取当前用户；未登录返回 None。"""
    if not session.get("user_id"):
        return None
    return {
        "user_id": session.get("user_id"),
        "username": session.get("username"),
        "display_name": session.get("display_name"),
        "role": session.get("role"),
        "class_id": session.get("class_id"),
    }


def login_session(row) -> None:
    """登录成功后写入会话：user_id、role、class_id 一个都不能少。"""
    session.clear()
    session["user_id"] = int(row["id"])
    session["username"] = row["username"]
    session["display_name"] = row["display_name"]
    session["role"] = row["role"]
    session["class_id"] = int(row["class_id"])
    session.permanent = False


def is_api_request() -> bool:
    return request.path.startswith("/api/")


def unauthorized_response():
    """FR-01 S3：API → 401 JSON；页面 → 302 到登录页。"""
    if is_api_request():
        return jsonify(error="unauthorized", message="未登录或会话已失效"), 401
    return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return unauthorized_response()
        return view(*args, **kwargs)

    return wrapper


def teacher_required(view):
    """先校验登录态，再校验角色：学生 MUST 被拒绝（403，FR-02）。"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return unauthorized_response()
        if session.get("role") != "teacher":
            return (
                jsonify(
                    error="forbidden",
                    message="无权限：仅教师账号可上传或管理班级材料",
                    required_role="teacher",
                    your_role=session.get("role"),
                ),
                403,
            )
        return view(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------- 路由

@bp.get("/login")
def login():
    if session.get("user_id"):
        return redirect(url_for("materials.page"))
    return render_template("login.html", error=None, next=request.args.get("next") or "")


@bp.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = request.form.get("next") or request.args.get("next") or ""

    row = db.get_user_by_username(username)
    # 用户不存在与口令错误返回同一提示，避免账号枚举；响应不含任何哈希信息（FR-01 S2）
    if row is None or not verify_password(password, row["password_hash"]):
        return (
            render_template(
                "login.html",
                error="账号或密码不正确，请重新输入。",
                next=next_url,
                username=username,
            ),
            401,
        )

    login_session(row)
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("materials.page"))


@bp.post("/logout")
@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
