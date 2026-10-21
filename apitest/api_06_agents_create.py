"""接口测试：新建应用（教师限定）。"""
import json
import time

from _client import endpoint


def _parse(status, text, ctx):
    body = json.loads(text)
    ctx["created_agent_id"] = int(body["id"])
    return True, f"id={body['id']} name={body['name']} skills={len(body.get('skills') or [])}"


def _payload(_client, _ctx):
    # 名称带时间戳：报告可反复跑，不会因为上次留下的同名应用变成 400
    return {
        "name": f"apitest 临时应用 {int(time.time())}",
        "prompt": "由接口测试创建，用于验证 201 与字段回显。",
    }


SPEC = endpoint(
    name="新建应用",
    method="POST",
    path="/api/agents",
    expect=(201,),
    payload=_payload,
    note="name ≤64 且班内唯一；学生调用为 403",
    parse=_parse,
)
