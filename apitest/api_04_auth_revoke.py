"""接口测试：撤销令牌（自包含 —— 用一次性令牌，不影响其余用例）。"""
from __future__ import annotations

import json
import time

from _client import ApiClient, DEMO_PASSWORDS

SPEC = {
    "name": "撤销令牌",
    "method": "POST",
    "path": "/api/auth/revoke",
    "expect": (200,),
    "note": "撤销后同一枚令牌立即 401；用一次性令牌执行，避免影响其它用例",
}


def run(client: ApiClient, ctx: dict) -> tuple[bool, str, int]:
    started = time.perf_counter()
    # 独立客户端：单独签发一枚一次性令牌
    probe = ApiClient(f"http://{client.host}:{client.port}")
    status, _h, text = probe.request(
        "POST", "/api/auth/token",
        {"username": "student_a1", "password": DEMO_PASSWORDS["student_a1"]},
    )
    if status != 200:
        return False, f"一次性令牌签发失败：{status}", 0
    probe.token = json.loads(text)["token"]

    before, _h, _t = probe.request("GET", "/api/agents")
    revoke, _h, revoke_body = probe.request("POST", "/api/auth/revoke", {})
    after, _h, _t = probe.request("GET", "/api/agents")
    elapsed = int((time.perf_counter() - started) * 1000)

    ok = before == 200 and revoke == 200 and after == 401
    detail = f"撤销前 {before} / 撤销 {revoke} / 撤销后 {after}"
    if not ok:
        detail += f" :: {revoke_body[:120]}"
    return ok, detail, elapsed
