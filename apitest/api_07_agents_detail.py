"""接口测试：应用详情（跨班与不存在同形 404）。"""
import json

from _client import endpoint


def _path(_client, ctx):
    return f"/api/agents/{ctx['agent_id']}"


def _parse(_status, text, _ctx):
    body = json.loads(text)
    if "id" not in body:
        return False, "缺少 id"
    return True, f"id={body['id']} name={body['name']} enabled={body['enabled']}"


SPEC = endpoint(
    name="应用详情",
    method="GET",
    path=_path,
    expect=(200,),
    note="按编号直取；他班编号与不存在返回同形 404",
    parse=_parse,
)
