"""配置与对话页面（服务端渲染外壳 + 同源 fetch）。

页面链路仍走 Cookie 会话（第 3 课不回退）；Bearer 只用于 API 客户端与接口测试。
所有动态内容用 `textContent` 写入，不做 `innerHTML` 拼接。
"""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, session, url_for

from . import db
from .auth import login_required

bp = Blueprint("pages", __name__)


def _context(template: str, title: str):
    klass = db.get_class(int(session["class_id"]))
    return render_template(
        template,
        title=title,
        klass=klass["name"] if klass else "-",
        role=session["role"],
        display_name=session["display_name"],
    )


@bp.get("/agents")
@login_required
def agents_page():
    return _context("agents.html", "AI 应用")


@bp.get("/skills")
@login_required
def skills_page():
    return _context("skills.html", "技能")


@bp.get("/mcp")
@login_required
def mcp_page():
    return _context("mcp.html", "MCP 工具")


@bp.get("/chat")
@login_required
def chat_page():
    return _context("chat.html", "对话")


@bp.get("/console")
def console():
    return redirect(url_for("openapi.api_docs"))
