"""接口测试：本班可用工具（tool@server 引用）。"""
import json

from _client import endpoint


def _parse(_status, text, _ctx):
    body = json.loads(text)
    tools = body.get("tools") or []
    refs = [t.get("ref") for t in tools]
    if tools and not all("@" in str(ref) for ref in refs):
        return False, f"引用格式异常：{refs}"
    return True, f"total={body.get('total')} refs={refs}"


SPEC = endpoint(
    name="MCP 可用工具",
    method="GET",
    path="/api/mcp/tools",
    expect=(200,),
    note="工具名来自服务器 tools/list，引用形如 calculator@campusclaw",
    parse=_parse,
)
