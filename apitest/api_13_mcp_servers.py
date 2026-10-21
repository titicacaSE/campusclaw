"""接口测试：MCP 服务器列表（只回 token_configured）。"""
import json

from _client import DEMO_PASSWORDS, endpoint

SENTINEL = "Bearer "


def _parse(_status, text, ctx):
    body = json.loads(text)
    servers = body.get("servers") or []
    if not servers:
        return False, "本班尚未登记 MCP 服务器"
    ctx["mcp_server_id"] = int(servers[0]["id"])
    for server in servers:
        if "token" in server and server.get("token") is not None:
            return False, "响应不得包含令牌字段"
        if not isinstance(server.get("token_configured"), bool):
            return False, "缺少 token_configured 布尔值"
    return True, f"total={body['total']} token_configured={servers[0]['token_configured']}"


SPEC = endpoint(
    name="MCP 服务器列表",
    method="GET",
    path="/api/mcp/servers",
    expect=(200,),
    note="令牌只在服务端环境变量；接口只回 token_configured 布尔值",
    parse=_parse,
)
