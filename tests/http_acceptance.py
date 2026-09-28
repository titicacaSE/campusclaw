"""真实 HTTP 验收测试 —— 起一个真实 werkzeug 服务，用 `http.client` 走网络请求，并用 `sqlite3`
直查数据库真实行数，逐条核对 A1–A10 与 A12。

为什么要有这一层
----------------
`test_acceptance.py` 用 test_client（进程内 WSGI 调用），能覆盖路由与业务逻辑；
但它**不经过网络栈**，看不到 Cookie 在真实 HTTP 往返中的行为，也不验证会话是否真靠
`Set-Cookie` 传递。本文件补齐这一点：全部断言基于真实 socket 上的状态码、响应头与响应体。

运行：
    python tests/http_acceptance.py

验收编号
--------
A1 会话建立 / A2 错误密码 / A3 未登录与登出 / A4 学生上传 403 / A5 教师上传 201 /
A6 上传后列表可见 / A7 跨班按 ID 404 / A8 列表仅本班 / A9 忽略客户端班级字段 /
A10 /health 免登录 / A12 失败路径 400 无脏数据。
（A11 重启留存见 `test_acceptance.py`；A13 规约校验见 `scripts/validate_spec.py`。）
"""
from __future__ import annotations

import http.client
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402
from app.seed import DEMO_PASSWORDS  # noqa: E402

SECRET = "http-acceptance-secret-key-0123456789"
CLASS_A, CLASS_B = 1, 2
COOKIE = "campusclaw_session"


# ===================================================================== 断言记录
class Checks:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, label: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  [PASS] {label}")
        else:
            self.failed += 1
            print(f"  [FAIL] {label}  {detail}")

    def section(self, aid: str, title: str) -> None:
        print(f"\n=== {aid} {title} ===")


# ===================================================================== HTTP 客户端
class Client:
    """极简带 Cookie 的 HTTP 客户端（不跟随重定向，便于断言 302）。"""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, body=None, ctype=None):
        headers = {}
        if ctype:
            headers["Content-Type"] = ctype
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())

        conn = http.client.HTTPConnection(self.host, self.port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            for sc in resp.headers.get_all("Set-Cookie") or []:
                name, _, rest = sc.partition("=")
                value = rest.split(";")[0]
                if value in ("", '""'):
                    self.cookies.pop(name.strip(), None)
                else:
                    self.cookies[name.strip()] = value
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            conn.close()

    def post_form(self, path: str, fields: dict):
        body = "&".join(f"{k}={v}" for k, v in fields.items()).encode()
        return self.request("POST", path, body, "application/x-www-form-urlencoded")

    def post_multipart(self, path: str, fields: dict, files: dict):
        boundary = "----campusclaw" + uuid.uuid4().hex
        buf = io.BytesIO()
        for key, value in fields.items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            buf.write(f"{value}\r\n".encode())
        for key, (filename, content) in files.items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode()
            )
            buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
            buf.write(content + b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        return self.request("POST", path, buf.getvalue(), f"multipart/form-data; boundary={boundary}")


def json_of(data: bytes):
    return json.loads(data.decode("utf-8"))


# ===================================================================== 主流程
def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="campusclaw-http-"))
    db_path = tmp / "app.db"
    upload_dir = tmp / "uploads"

    app = create_app(
        SECRET_KEY=SECRET, DATABASE=str(db_path), UPLOAD_DIR=str(upload_dir), TESTING=False
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    host, port = "127.0.0.1", server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def scalar(query, params=()):
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(query, params).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def file_count():
        if not upload_dir.exists():
            return 0
        return sum(1 for p in upload_dir.rglob("*") if p.is_file())

    ck = Checks()
    print(f"CampusClaw 真实 HTTP 验收 | 服务地址 http://{host}:{port} | 库 {db_path}")

    try:
        # ------------------------------------------------------------------ A10
        ck.section("A10", "/health 免登录")
        c0 = Client(host, port)
        st, hdr, body = c0.request("GET", "/health")
        ck.check("无 Cookie 访问 /health 返回 200", st == 200, f"got {st}")
        ck.check("响应为 JSON 且 status=ok", json_of(body).get("status") == "ok", body[:120])
        ck.check("未发生到登录页的重定向", "location" not in hdr, hdr.get("location", ""))
        ck.check("未下发会话 Cookie", COOKIE not in c0.cookies, str(c0.cookies))

        # ------------------------------------------------------------------ A3
        ck.section("A3", "未登录访问受保护资源")
        c = Client(host, port)
        st, hdr, body = c.request("GET", "/materials")
        ck.check("页面请求返回 302", st == 302, f"got {st}")
        ck.check("重定向目标为 /login", "/login" in hdr.get("location", ""), hdr.get("location", ""))

        st, _, body = c.request("GET", "/api/materials")
        ck.check("API 请求返回 401", st == 401, f"got {st}")
        ck.check("401 响应不含班级材料数据", "讲义" not in body.decode("utf-8"), body[:120])

        # ------------------------------------------------------------------ A1
        ck.section("A1", "会话建立（教师 / 学生）")
        teacher = Client(host, port)
        st, hdr, _ = teacher.post_form("/login", {"username": "teacher_a", "password": DEMO_PASSWORDS["teacher_a"]})
        ck.check("教师登录返回 302", st == 302, f"got {st}")
        ck.check("登录后下发会话 Cookie", COOKIE in teacher.cookies, str(teacher.cookies))
        st, _, body = teacher.request("GET", "/api/materials")
        payload = json_of(body)
        ck.check("教师会话 role=teacher", payload.get("role") == "teacher", str(payload.get("role")))
        ck.check("教师会话 class_id=班级 A", payload.get("class_id") == CLASS_A, str(payload.get("class_id")))

        student = Client(host, port)
        student.post_form("/login", {"username": "student_a1", "password": DEMO_PASSWORDS["student_a1"]})
        st, _, body = student.request("GET", "/api/materials")
        payload = json_of(body)
        ck.check("学生会话 role=student", payload.get("role") == "student", str(payload.get("role")))
        ck.check("学生会话 class_id=班级 A", payload.get("class_id") == CLASS_A, str(payload.get("class_id")))

        # ------------------------------------------------------------------ A2
        ck.section("A2", "错误密码登录失败")
        bad = Client(host, port)
        st, hdr, body = bad.post_form("/login", {"username": "teacher_a", "password": "wrong-password"})
        ck.check("错误口令返回 401", st == 401, f"got {st}")
        ck.check("不下发会话 Cookie", COOKIE not in bad.cookies, str(bad.cookies))
        text = body.decode("utf-8", "ignore").lower()
        ck.check("响应不含 bcrypt 哈希", "$2" not in text, body[:120])
        st, _, _ = bad.request("GET", "/api/materials")
        ck.check("错误口令后仍为未登录（401）", st == 401, f"got {st}")

        # ------------------------------------------------------------------ A4
        ck.section("A4", "学生上传被拒绝")
        m0, k0, f0 = scalar("SELECT COUNT(*) FROM materials"), scalar("SELECT COUNT(*) FROM knowledge_entries"), file_count()
        st, _, body = student.post_multipart(
            "/api/materials/upload", {}, {"file": ("student-try.md", "学生尝试上传".encode())}
        )
        ck.check("学生上传返回 403", st == 403, f"got {st}")
        ck.check("403 消息含「无权限」", "无权限" in body.decode("utf-8"), body[:160])
        ck.check("materials 行数不变", scalar("SELECT COUNT(*) FROM materials") == m0)
        ck.check("knowledge_entries 行数不变", scalar("SELECT COUNT(*) FROM knowledge_entries") == k0)
        ck.check("上传目录无新文件", file_count() == f0)

        # ------------------------------------------------------------------ A5
        ck.section("A5", "教师上传（落盘 → 解析 → 同事务双表写入）")
        ma, ka = scalar("SELECT COUNT(*) FROM materials WHERE class_id=?", (CLASS_A,)), scalar(
            "SELECT COUNT(*) FROM knowledge_entries WHERE class_id=?", (CLASS_A,)
        )
        st, _, body = teacher.post_multipart(
            "/api/materials/upload",
            {"title": "HTTP 验收讲义", "subject": "数学"},
            {"file": ("http-acceptance.md", "真实 HTTP 上传内容：勾股定理 a²+b²=c²".encode())},
        )
        ck.check("教师上传返回 201", st == 201, f"got {st}")
        payload = json_of(body) if st == 201 else {}
        mid = payload.get("material_id")
        ck.check("响应含 material_id", isinstance(mid, int), str(payload))
        ck.check("材料归属班级 A", scalar("SELECT class_id FROM materials WHERE id=?", (mid,)) == CLASS_A)
        ck.check(
            "知识库条目归属班级 A",
            scalar("SELECT class_id FROM knowledge_entries WHERE material_id=?", (mid,)) == CLASS_A,
        )
        ck.check("班级 A materials +1", scalar("SELECT COUNT(*) FROM materials WHERE class_id=?", (CLASS_A,)) == ma + 1)
        ck.check(
            "班级 A knowledge_entries +1",
            scalar("SELECT COUNT(*) FROM knowledge_entries WHERE class_id=?", (CLASS_A,)) == ka + 1,
        )

        # ------------------------------------------------------------------ A6
        ck.section("A6", "上传后列表可见（教师 + 学生）")
        for who, cli in (("教师", teacher), ("学生", student)):
            st, _, body = cli.request("GET", "/api/materials")
            titles = [m["title"] for m in json_of(body)["materials"]]
            ck.check(f"{who}列表可见新上传材料", "HTTP 验收讲义" in titles, str(titles))

        # ------------------------------------------------------------------ A7
        ck.section("A7", "跨班按 ID 访问被拒绝")
        b_id = scalar("SELECT id FROM materials WHERE class_id=? ORDER BY id LIMIT 1", (CLASS_B,))
        st, _, body = teacher.request("GET", f"/api/materials/{b_id}")
        ck.check("跨班详情返回 404", st == 404, f"got {st}")
        text = body.decode("utf-8")
        ck.check("响应不含 B 班标题", "B 班" not in text, body[:160])
        ck.check("响应不含 B 班正文片段", "一元二次方程" not in text, body[:160])
        st, _, _ = teacher.request("GET", f"/api/materials/{b_id}/download")
        ck.check("跨班下载返回 404", st == 404, f"got {st}")
        st, _, _ = teacher.request("GET", "/api/materials/999999")
        ck.check("不存在材料同样 404（不泄露存在性）", st == 404, f"got {st}")

        # ------------------------------------------------------------------ A8
        ck.section("A8", "列表仅含本班记录")
        for who, cli in (("学生 A1", student), ("教师 A", teacher)):
            st, _, body = cli.request("GET", "/api/materials")
            items = json_of(body)["materials"]
            ck.check(f"{who}看到的每条记录均属班级 A", all(m["class_id"] == CLASS_A for m in items))
            ck.check(f"{who}列表无「B 班」标题", all("B 班" not in m["title"] for m in items), str([m["title"] for m in items]))
        db_a = scalar("SELECT COUNT(*) FROM materials WHERE class_id=?", (CLASS_A,))
        ck.check("列表条数等于库内本班条数", len(items) == db_a, f"api={len(items)} db={db_a}")

        # ------------------------------------------------------------------ A9
        ck.section("A9", "上传忽略客户端班级字段")
        st, _, body = teacher.post_multipart(
            "/api/materials/upload",
            {"class_id": str(CLASS_B), "class": "班级 B"},
            {"file": ("inject.md", "试图指定 B 班".encode())},
        )
        inject_id = json_of(body).get("material_id") if st == 201 else None
        ck.check("注入 class 字段的上传仍 201", st == 201, f"got {st}")
        ck.check(
            "材料仍写入会话班级（班级 A）",
            scalar("SELECT class_id FROM materials WHERE id=?", (inject_id,)) == CLASS_A,
        )
        ck.check(
            "班级 B 行数未因该请求增加",
            scalar("SELECT COUNT(*) FROM materials WHERE class_id=?", (CLASS_B,)) == 2,
        )

        # ------------------------------------------------------------------ A12
        ck.section("A12", "失败路径不产生脏数据")
        for name, content, desc in (
            ("bad.exe", b"MZ\x90\x00", "不支持格式"),
            ("empty.txt", b"", "空文件"),
            ("broken.md", b"\xff\xfe\x00\x01", "非 UTF-8 损坏文件"),
        ):
            mb, kb, fb = scalar("SELECT COUNT(*) FROM materials"), scalar("SELECT COUNT(*) FROM knowledge_entries"), file_count()
            st, _, _ = teacher.post_multipart("/api/materials/upload", {}, {"file": (name, content)})
            ck.check(f"{desc} 返回 400", st == 400, f"got {st}")
            ck.check(f"{desc} 无 materials 残留", scalar("SELECT COUNT(*) FROM materials") == mb)
            ck.check(f"{desc} 无 knowledge_entries 残留", scalar("SELECT COUNT(*) FROM knowledge_entries") == kb)
            ck.check(f"{desc} 无孤立文件", file_count() == fb)

        # ------------------------------------------------------------------ A3 收尾：登出
        ck.section("A3", "登出清空会话")
        st, _, _ = teacher.request("GET", "/logout")
        ck.check("登出返回 302", st in (302, 200), f"got {st}")
        st, _, _ = teacher.request("GET", "/api/materials")
        ck.check("登出后受保护 API 返回 401", st == 401, f"got {st}")

    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    total = ck.passed + ck.failed
    print(f"\n{'=' * 60}\nHTTP 验收结果：{ck.passed}/{total} 通过，{ck.failed} 失败\n{'=' * 60}")
    return 0 if ck.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
