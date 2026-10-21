"""接口测试：签发 Bearer JWT。"""
import json

from _client import DEMO_PASSWORDS, endpoint

TOKEN_HOLDER: dict[str, str] = {}


def _parse(status, text, ctx):
    body = json.loads(text)
    token = body.get("token") or ""
    if token.count(".") != 2:
        return False, f"token 不是三段式：{token[:40]}"
    if body.get("token_type") != "Bearer":
        return False, f"token_type={body.get('token_type')}"
    if DEMO_PASSWORDS["teacher_a"] in text or "password_hash" in text:
        return False, "响应泄露了口令或哈希"
    TOKEN_HOLDER["token"] = token
    ctx["token"] = token
    return True, f"expires_in={body.get('expires_in')} token={token[:24]}…"


SPEC = endpoint(
    name="签发令牌",
    method="POST",
    path="/api/auth/token",
    auth=False,
    expect=(200,),
    payload={"username": "teacher_a", "password": DEMO_PASSWORDS["teacher_a"]},
    note="口令正确返回 Bearer JWT；错误口令与未知用户同形 401",
    parse=_parse,
)
