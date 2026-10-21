"""接口测试：新建会话（锁定应用）。"""
import json

from _client import endpoint


def _path(_client, _ctx):
    return "/api/conversations"


def _payload(_client, ctx):
    return {"agent_id": ctx["agent_id"]}


def _parse(_status, text, ctx):
    body = json.loads(text)
    ctx["conversation_id"] = int(body["id"])
    return True, f"id={body['id']} agent={body['agent_name']}"


SPEC = endpoint(
    name="新建会话",
    method="POST",
    path=_path,
    payload=_payload,
    expect=(201,),
    note="会话一旦建立即锁定 agent_id；后续请求体换 agent_id 无效",
    parse=_parse,
)
