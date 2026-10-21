"""接口测试：应用列表（学生只见已启用）。"""
import json

from _client import endpoint


def _parse(status, text, ctx):
    body = json.loads(text)
    agents = body.get("agents") or []
    if "total" not in body:
        return False, "缺少 total"
    ctx["agent_id"] = int(agents[0]["id"]) if agents else None
    names = [a["name"] for a in agents]
    return True, f"total={body['total']} names={names}"


SPEC = endpoint(
    name="应用列表",
    method="GET",
    path="/api/agents",
    expect=(200,),
    note="教师见本班全部；学生只见 enabled=1",
    parse=_parse,
)
