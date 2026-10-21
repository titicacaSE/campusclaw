"""接口测试的最小客户端与脚本骨架（零第三方依赖）。

一个接口一个脚本，脚本只声明 `SPEC`；`run_all.py` 负责按序执行、计时、
生成 HTML 报告。这样加接口只需新增一个文件，不用改运行器。
"""
from __future__ import annotations

import http.client
import json
import time
from urllib.parse import urlsplit

DEMO_PASSWORDS = {
    "teacher_a": "TeacherA@2026",
    "teacher_b": "TeacherB@2026",
    "student_a1": "StudentA1@2026",
    "student_b1": "StudentB1@2026",
}


class ApiClient:
    """极简客户端：Bearer 优先，另外保留 Cookie（页面链路用）。"""

    def __init__(self, base_url: str, token: str | None = None, timeout: int = 15):
        parts = urlsplit(base_url.rstrip("/"))
        self.scheme = parts.scheme or "http"
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.token = token
        self.cookies: dict[str, str] = {}
        self.timeout = timeout

    def request(self, method: str, path: str, payload=None, headers=None, raw=False):
        body = None
        hdrs = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if self.token and "Authorization" not in hdrs:
            hdrs["Authorization"] = f"Bearer {self.token}"
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())

        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, self.prefix + path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            for cookie in resp.headers.get_all("Set-Cookie") or []:
                name, _, rest = cookie.partition("=")
                self.cookies[name.strip()] = rest.split(";")[0]
            text = data.decode("utf-8", "replace")
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, text
        finally:
            conn.close()

    def login(self, username: str = "teacher_a") -> str:
        status, _headers, text = self.request(
            "POST", "/api/auth/token",
            {"username": username, "password": DEMO_PASSWORDS[username]},
        )
        if status != 200:
            raise RuntimeError(f"签发失败 {username}: {status} {text[:120]}")
        self.token = json.loads(text)["token"]
        return self.token


def endpoint(*, name, method, path, auth=True, expect=(200,), payload=None,
             note="", parse=None):
    return {
        "name": name, "method": method, "path": path, "auth": auth,
        "expect": tuple(expect), "payload": payload, "note": note, "parse": parse,
    }


def run_spec(spec: dict, client: ApiClient, ctx: dict) -> tuple[bool, str, int]:
    path = spec["path"](client, ctx) if callable(spec["path"]) else spec["path"]
    payload = spec["payload"]
    if callable(payload):
        payload = payload(client, ctx)

    started = time.perf_counter()
    status, _headers, text = client.request(spec["method"], path, payload)
    elapsed = int((time.perf_counter() - started) * 1000)

    ok = status in spec["expect"]
    note = ""
    if ok and spec["parse"]:
        try:
            ok, note = spec["parse"](status, text, ctx)
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"解析失败：{type(exc).__name__}: {exc}"
    detail = note or text[:220].replace("\n", " ")
    return ok, detail, elapsed
