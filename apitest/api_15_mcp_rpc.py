"""接口测试：MCP JSON-RPC 服务（initialize / tools/list / tools/call）。"""
from __future__ import annotations

import json
import os
import time

from _client import ApiClient

SPEC = {
    "name": "MCP JSON-RPC",
    "method": "POST",
    "path": "/mcp",
    "expect": (200, 401),
    "note": "独立令牌（MCP_SERVER_TOKEN），不是用户 JWT；未配置时 401 属预期",
}


def run(client: ApiClient, ctx: dict) -> tuple[bool, str, int]:
    started = time.perf_counter()
    token = ctx.get("mcp_token") or os.environ.get("MCP_SERVER_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    def rpc(method, params=None, extra=None):
        return client.request(
            "POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            headers={**headers, **(extra or {})},
        )

    init, _h, init_body = rpc("initialize")
    listed, _h, list_body = rpc("tools/list")
    called, _h, call_body = rpc(
        "tools/call", {"name": "calculator", "arguments": {"expression": "12*(3+4)"}}
    )
    anonymous_client = ApiClient(f"http://{client.host}:{client.port}")
    anonymous, _h, anon_body = anonymous_client.request(
        "POST", "/mcp", {"jsonrpc": "2.0", "id": 9, "method": "initialize"}
    )
    elapsed = int((time.perf_counter() - started) * 1000)

    if not token:
        ok = init == 401 and anonymous == 401
        return ok, "未设置 MCP_SERVER_TOKEN：有/无令牌均 401（预期）", elapsed

    tools = (json.loads(list_body).get("result") or {}).get("tools") or []
    result = json.loads(call_body).get("result") or {}
    text = (result.get("content") or [{}])[0].get("text", "")
    ok = (
        init == 200
        and anonymous == 401
        and [t["name"] for t in tools] == ["calculator", "text_stats"]
        and result.get("isError") is False
        and "84" in text
    )
    ctx["mcp_tool_result"] = text
    detail = (
        f"initialize={init} 匿名={anonymous} tools={[t['name'] for t in tools]} "
        f"calculator={text}"
    )
    if not ok:
        detail += f" :: {anon_body[:80]}"
    return ok, detail, elapsed
