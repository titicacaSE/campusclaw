"""接口测试：当前身份（角色与班级来自库，不来自令牌）。"""
import json

from _client import endpoint


def _parse(status, text, _ctx):
    body = json.loads(text)
    for key in ("user_id", "role", "class_id"):
        if key not in body:
            return False, f"缺少字段 {key}"
    if "token" in body:
        return False, "身份响应不应回传令牌"
    return True, f"role={body['role']} class_id={body['class_id']}"


SPEC = endpoint(
    name="当前身份",
    method="GET",
    path="/api/auth/me",
    expect=(200,),
    note="Bearer 有效即回身份；角色与班级每次回库读",
    parse=_parse,
)
