"""MCP 宿主：服务器登记、工具发现、引用校验、候选集、审计。

规约依据 `add-agent-platform-mcp-tools` FR-5 / design.md §7：
  - 令牌只从环境变量读取，`mcp_servers` 表只存**变量名**；
  - 登记接口只回 `token_configured` 布尔值，绝不回传令牌；
  - 候选集只含「本次命中技能引用的工具」，不是服务器全部工具；
  - 未连接 / 越权 → `denied`，错误 → `error`，成功 → `ok`，三者都写审计。
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, current_app, jsonify, request

from . import db
from .auth import current_user, teacher_required
from .mcp_client import McpClient, McpError

log = logging.getLogger("campusclaw.mcp_host")

bp = Blueprint("mcp_host", __name__)

BUILTIN_TOOL = "knowledge_search"
DENY_CODES = ("mcp_unreachable", "mcp_unauthorized", "mcp_not_configured")


# ------------------------------------------------------------------ 基础


def _transport():
    """测试注入点：`app.config["MCP_TRANSPORT"]`。生产为 None → 走真实 HTTP。"""
    return current_app.config.get("MCP_TRANSPORT")


def list_servers(class_id: int) -> list:
    return db.query(
        "SELECT * FROM mcp_servers WHERE class_id = ? ORDER BY id", (int(class_id),)
    )


def get_server(class_id: int, server_id: int):
    return db.query_one(
        "SELECT * FROM mcp_servers WHERE class_id = ? AND id = ?", (int(class_id), int(server_id))
    )


def server_by_name(class_id: int, name: str):
    return db.query_one(
        "SELECT * FROM mcp_servers WHERE class_id = ? AND name = ?", (int(class_id), str(name))
    )


def token_for(row) -> str:
    """令牌解析：环境变量优先，其次应用配置（两者都不落库、不下发）。"""
    env_name = str(row["token_env"] or "")
    if not env_name:
        return ""
    value = os.environ.get(env_name)
    if value:
        return value
    return str(current_app.config.get(env_name) or "")


def client_for_server(row) -> McpClient:
    return McpClient(row["base_url"], token_for(row), transport=_transport())


def _serialize_server(row) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "base_url": row["base_url"],
        "token_env": row["token_env"],
        "token_configured": bool(token_for(row)),   # 只回布尔值
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


# ------------------------------------------------------------------ 发现


def probe(row, *, transport=None) -> dict:
    """执行 `initialize` + `tools/list`。失败返回可读原因，不抛。"""
    client = McpClient(row["base_url"], token_for(row), transport=transport or _transport())
    try:
        info = client.initialize() or {}
        tools = client.list_tools()
    except McpError as exc:
        return {"ok": False, "reason": exc.code, "message": str(exc), "tools": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "mcp_error", "message": type(exc).__name__, "tools": []}
    return {
        "ok": True,
        "server_info": (info or {}).get("serverInfo") or {},
        "protocol_version": (info or {}).get("protocolVersion"),
        "tools": tools,
    }


def available_tool_refs(class_id: int) -> set[str]:
    """本班已连接服务器上可用的 `工具名@服务器名` 集合（用于保存前校验）。"""
    refs: set[str] = set()
    for row in list_servers(class_id):
        if not row["enabled"]:
            continue
        result = probe(row)
        if not result["ok"]:
            continue
        for tool in result["tools"]:
            refs.add(f"{tool['name']}@{row['name']}")
    return refs


def declare_tools(class_id: int, refs: list[str]) -> tuple[list[dict], dict[str, str], list[str]]:
    """把引用解析成模型可见的 `tools` 声明。

    返回 `(declarations, name->ref 映射, denied_refs)`。
    **只有 refs 里明确列出的工具**才会出现，服务器其余工具一律不下发。
    """
    declarations: list[dict] = []
    mapping: dict[str, str] = {}
    denied: list[str] = []
    seen: set[str] = set()

    for ref in refs:
        ref = str(ref or "").strip()
        if not ref or ref == BUILTIN_TOOL:
            continue
        tool_name, _, server_name = ref.partition("@")
        if not server_name:
            denied.append(ref)
            continue
        row = server_by_name(class_id, server_name)
        if row is None or not row["enabled"]:
            denied.append(ref)
            continue
        try:
            client = client_for_server(row)
            tools = client.list_tools()
        except McpError as exc:
            log.info("工具发现失败 ref=%s reason=%s", ref, exc.code)
            denied.append(ref)
            continue

        found = next((tool for tool in tools if tool.get("name") == tool_name), None)
        if found is None:
            denied.append(ref)
            continue
        if tool_name in seen:
            continue
        seen.add(tool_name)
        mapping[tool_name] = ref
        declarations.append(
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": found.get("description") or "",
                    "parameters": found.get("inputSchema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return declarations, mapping, denied


def execute(class_id: int, ref: str, arguments: dict, *, ctx: dict | None = None,
            transport=None) -> tuple[str, str]:
    """执行一次工具调用。返回 `(status, text)`，并写一条审计。"""
    ctx = ctx or {}
    tool_name, _, server_name = str(ref or "").partition("@")
    row = server_by_name(class_id, server_name) if server_name else None
    if row is None or not row["enabled"]:
        text = "该工具所属服务器未连接，已拒绝调用"
        audit(class_id, ref, server_name or "-", "denied", text, ctx=ctx)
        return "denied", text

    client = McpClient(row["base_url"], token_for(row), transport=transport or _transport())
    try:
        is_error, text = client.call_tool(tool_name, arguments or {})
    except McpError as exc:
        status = "denied" if exc.code in DENY_CODES else "error"
        audit(class_id, ref, server_name, status, f"{exc.code}: {exc}", ctx=ctx)
        return status, str(exc)
    except Exception as exc:  # noqa: BLE001
        audit(class_id, ref, server_name, "error", type(exc).__name__, ctx=ctx)
        return "error", type(exc).__name__

    status = "error" if is_error else "ok"
    audit(class_id, ref, server_name, status, text[:200], ctx=ctx)
    return status, text


def audit(class_id: int, tool_ref: str, server: str, status: str, detail: str,
          *, ctx: dict | None = None) -> None:
    ctx = ctx or {}
    db.execute(
        "INSERT INTO mcp_tool_audit"
        " (class_id, user_id, conversation_id, round, tool_ref, server, status, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(class_id), ctx.get("user_id"), ctx.get("conversation_id"),
            int(ctx.get("round") or 0), str(tool_ref), str(server), str(status),
            str(detail or "")[:500], db.utcnow(),
        ),
    )
    db.get_conn().commit()


# ------------------------------------------------------------------ HTTP


def _payload() -> dict:
    payload: dict = {}
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        payload.update(body)
    if request.form:
        payload.update(request.form.to_dict())
    payload.update(request.args.to_dict())
    return payload


@bp.get("/api/mcp/servers")
def api_list_servers():
    user = current_user()
    if not user:
        return jsonify(error="unauthorized", message="未登录"), 401
    rows = list_servers(int(user["class_id"]))
    return jsonify(servers=[_serialize_server(row) for row in rows], total=len(rows))


@bp.post("/api/mcp/servers")
@teacher_required
def api_register_server():
    user = current_user()
    class_id = int(user["class_id"])
    payload = _payload()

    name = str(payload.get("name") or "").strip()
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    token_env = str(payload.get("token_env") or "MCP_SERVER_TOKEN").strip()
    if not name or not base_url:
        return jsonify(
            error="invalid_request", message="name 与 base_url 必填"
        ), 400
    if server_by_name(class_id, name):
        return jsonify(error="duplicate_name", message="班内已存在同名服务器"), 400

    now = db.utcnow()
    cur = db.get_conn().execute(
        "INSERT INTO mcp_servers (class_id, name, base_url, token_env, enabled,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
        (class_id, name, base_url, token_env, now, now),
    )
    db.get_conn().commit()
    return jsonify(_serialize_server(get_server(class_id, int(cur.lastrowid)))), 201


@bp.patch("/api/mcp/servers/<int:server_id>")
@teacher_required
def api_update_server(server_id: int):
    user = current_user()
    class_id = int(user["class_id"])
    row = get_server(class_id, server_id)
    if row is None:
        return jsonify(error="not_found", message="服务器不存在"), 404

    payload = _payload()
    enabled = payload.get("enabled", row["enabled"])
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "否")
    base_url = str(payload.get("base_url", row["base_url"]) or "").rstrip("/")
    token_env = str(payload.get("token_env", row["token_env"]) or "")

    db.execute(
        "UPDATE mcp_servers SET base_url = ?, token_env = ?, enabled = ?, updated_at = ?"
        " WHERE class_id = ? AND id = ?",
        (base_url, token_env, 1 if enabled else 0, db.utcnow(), class_id, server_id),
    )
    db.get_conn().commit()
    return jsonify(_serialize_server(get_server(class_id, server_id)))


@bp.post("/api/mcp/servers/<int:server_id>/probe")
@teacher_required
def api_probe(server_id: int):
    user = current_user()
    class_id = int(user["class_id"])
    row = get_server(class_id, server_id)
    if row is None:
        return jsonify(error="not_found", message="服务器不存在"), 404
    result = probe(row)
    result["server"] = _serialize_server(row)
    return jsonify(result)


@bp.get("/api/mcp/tools")
def api_tools():
    user = current_user()
    if not user:
        return jsonify(error="unauthorized", message="未登录"), 401
    class_id = int(user["class_id"])
    out = []
    for row in list_servers(class_id):
        result = probe(row)
        for tool in result["tools"]:
            out.append(
                {
                    "ref": f"{tool['name']}@{row['name']}",
                    "name": tool["name"],
                    "description": tool.get("description"),
                    "inputSchema": tool.get("inputSchema"),
                    "server": row["name"],
                    "connected": bool(result["ok"] and row["enabled"]),
                }
            )
    return jsonify(tools=out, total=len(out))


@bp.get("/api/mcp/audit")
def api_audit():
    user = current_user()
    if not user:
        return jsonify(error="unauthorized", message="未登录"), 401
    class_id = int(user["class_id"])
    rows = db.query(
        "SELECT * FROM mcp_tool_audit WHERE class_id = ? ORDER BY id DESC LIMIT 50",
        (class_id,),
    )
    return jsonify(
        audit=[
            {
                "id": int(row["id"]),
                "tool_ref": row["tool_ref"],
                "server": row["server"],
                "status": row["status"],
                "round": int(row["round"]),
                "user_id": row["user_id"],
                "conversation_id": row["conversation_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ],
        total=len(rows),
    )
