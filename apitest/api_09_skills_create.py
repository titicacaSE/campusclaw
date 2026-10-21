"""接口测试：上传技能文件（空行分元数据 / 正文）。"""
import json
import time

from _client import endpoint

TEXT = """名称: apitest 技能 {ts}
适用条件: 接口测试, 自动化
资料:
工具: knowledge_search
启用: 是

这是由接口测试上传的技能说明正文，用来验证「空行以上元数据、空行以下正文」的解析。
"""


def _payload(_client, _ctx):
    return {"text": TEXT.format(ts=int(time.time()))}


def _parse(_status, text, _ctx):
    body = json.loads(text)
    triggers = body.get("triggers_list") or []
    if triggers != ["接口测试", "自动化"]:
        return False, f"触发词切分异常：{triggers}"
    if body.get("is_builtin") is not False:
        return False, "上传的技能不应标记为内置"
    return True, f"id={body['id']} triggers={triggers}"


SPEC = endpoint(
    name="上传技能",
    method="POST",
    path="/api/skills",
    expect=(201,),
    payload=_payload,
    note="六类不合规（空文件/缺正文/坏行/未知字段/重复字段/缺必填）返回 400",
    parse=_parse,
)
