"""智能体平台的进程内验收夹具（离线可用）。

为什么要单独一层
----------------
第 5/6 课的验收要求「真实 HTTP 语义、真实 SQLite、真实 JSON-RPC」，但课堂上
没有课程网关凭证、也没有 Qdrant。这里用两个**行为等价替身**：

  - `FakeChat`：对话网关替身。记录每次请求体（messages/tools/温度/超时），
    可按脚本返回 `tool_calls`，也可模拟「网关不可达」。断言的是「我们下发了什么」，
    而不是模型回了什么 —— 这正是可复核的部分。
  - `_mcp_transport`：把 JSON-RPC 直接打到本应用的 `/mcp`，走真实路由、
    真实令牌校验、真实工具实现，只是不经 TCP。

其余全部真实：Flask 路由、Cookie/Bearer 解析、SQLite 表与索引、种子数据。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789")
os.environ.setdefault("MCP_SERVER_TOKEN", "mcp-test-token")

from app import create_app          # noqa: E402
from app import db as app_db        # noqa: E402

MCP_TOKEN = "mcp-test-token"

PASSWORDS = {
    "teacher_a": "TeacherA@2026",
    "teacher_b": "TeacherB@2026",
    "student_a1": "StudentA1@2026",
    "student_b1": "StudentB1@2026",
}

CLASS_A = 1
CLASS_B = 2

# 班级 A 语文讲义里只有这一段含「倒叙」；班级 A 数学讲义不含
A_CN_PHRASE = "倒叙的作用是设置悬念"
# 只在班级 B 材料里出现的词
B_ONLY = "一元二次方程"


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class FakeChat:
    """可脚本化的对话网关替身。"""

    def __init__(self, reply: str = "依据资料 [1]，倒叙的作用是设置悬念。", script=None):
        self.reply = reply
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.timeouts: list[int] = []
        self.down = False

    def __call__(self, url, headers, payload, timeout):
        self.calls.append(json.loads(json.dumps(payload, ensure_ascii=False)))
        self.timeouts.append(timeout)
        if self.down:
            raise RuntimeError("gateway down")
        if self.script:
            step = self.script.pop(0)
            if step is not None:
                return {"choices": [{"message": {"content": "", "tool_calls": step}}]}
        return {"choices": [{"message": {"content": self.reply}}]}

    # ---------------------------------------------------------------- 读法
    @property
    def count(self) -> int:
        return len(self.calls)

    @property
    def last(self) -> dict:
        return self.calls[-1] if self.calls else {}

    def messages(self, index: int = -1) -> list[dict]:
        if not self.calls:
            return []
        return self.calls[index].get("messages") or []

    def system_of(self, index: int = -1) -> str:
        for message in self.messages(index):
            if message.get("role") == "system":
                return str(message.get("content") or "")
        return ""

    def user_of(self, index: int = -1) -> str:
        for message in reversed(self.messages(index)):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    def tools_of(self, index: int = -1) -> list[dict]:
        if not self.calls:
            return []
        return self.calls[index].get("tools") or []


class Platform:
    """一个隔离的临时实例：独立 DB、独立上传目录、独立替身。

    为什么复用「种子模板」
    ----------------------
    每个用例都从空库建表 + 播种，代价主要在 bcrypt（4 个演示账号，各 ~0.3s）。
    100 多个用例就会把测试拖到两分半。这里先建**一份**种子库，之后每个用例
    复制文件即可 —— SQLite 默认 rollback journal，提交后主文件即完整快照。
    隔离性不受影响：每个用例仍各有自己的库与上传目录。
    """

    _template: Path | None = None

    @classmethod
    def template_db(cls) -> Path:
        if cls._template is None:
            base = Path(tempfile.mkdtemp(prefix="campusclaw-seed-"))
            create_app(
                DATABASE=str(base / "app.db"),
                UPLOAD_DIR=str(base / "uploads"),
                TESTING=True,
            )
            cls._template = base / "app.db"
        return cls._template

    def __init__(self, *, chat=None, app_config: dict | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="campusclaw-agent-"))
        shutil.copyfile(self.template_db(), self.dir / "app.db")
        self.chat = chat if chat is not None else FakeChat()
        self.app = create_app(
            DATABASE=str(self.dir / "app.db"),
            UPLOAD_DIR=str(self.dir / "uploads"),
            TESTING=True,
            CHAT_BASE_URL="http://gateway.local/v1",
            CHAT_API_KEY="stub-key",
            CHAT_MODEL="stub-model",
            MCP_SERVER_TOKEN=MCP_TOKEN,
            MCP_BASE_URL="http://mcp.local",
            CHAT_TRANSPORT=self.chat,
            **(app_config or {}),
        )
        self.client = self.app.test_client()
        # 宿主发现工具时也走进程内传输，保证令牌校验仍真实发生
        self.app.config["MCP_TRANSPORT"] = self._mcp_transport
        self._tokens: dict[str, str] = {}

    # ---------------------------------------------------------------- 基础设施
    def _mcp_transport(self, url, headers, payload, timeout):
        resp = self.client.post("/mcp", json=payload, headers=headers)
        return resp.status_code, resp.get_json()

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---------------------------------------------------------------- 身份
    def token(self, username: str = "student_a1") -> str:
        if username not in self._tokens:
            resp = self.client.post(
                "/api/auth/token",
                json={"username": username, "password": PASSWORDS[username]},
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)
            self._tokens[username] = resp.get_json()["token"]
        return self._tokens[username]

    def auth(self, username: str = "student_a1") -> dict:
        return {"Authorization": f"Bearer {self.token(username)}"}

    def forget(self, username: str) -> None:
        self._tokens.pop(username, None)

    def login_cookie(self, username: str = "teacher_a"):
        """页面链路用 Cookie 会话（第 3 课口径不退回退）。"""
        return self.client.post(
            "/api/login", json={"username": username, "password": PASSWORDS[username]}
        )

    # ---------------------------------------------------------------- HTTP
    def get(self, path: str, username: str = "student_a1", **kwargs):
        headers = {**self.auth(username), **(kwargs.pop("headers", None) or {})}
        return self.client.get(path, headers=headers, **kwargs)

    def post(self, path: str, username: str = "student_a1", **kwargs):
        headers = {**self.auth(username), **(kwargs.pop("headers", None) or {})}
        return self.client.post(path, headers=headers, **kwargs)

    def patch(self, path: str, username: str = "teacher_a", **kwargs):
        headers = {**self.auth(username), **(kwargs.pop("headers", None) or {})}
        return self.client.patch(path, headers=headers, **kwargs)

    def put(self, path: str, username: str = "teacher_a", **kwargs):
        headers = {**self.auth(username), **(kwargs.pop("headers", None) or {})}
        return self.client.put(path, headers=headers, **kwargs)

    # ---------------------------------------------------------------- 数据
    def rows(self, sql: str, params=()) -> list[dict]:
        with self.app.app_context():
            return [dict(row) for row in app_db.query(sql, params)]

    def row(self, sql: str, params=()):
        got = self.rows(sql, params)
        return got[0] if got else None

    def exec(self, sql: str, params=()) -> None:
        with self.app.app_context():
            app_db.execute(sql, params)
            app_db.get_conn().commit()

    # ---------------------------------------------------------------- 业务便捷
    def agents(self, username: str = "teacher_a") -> list[dict]:
        resp = self.get("/api/agents", username)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return resp.get_json()["agents"]

    def agent_id(self, name: str, username: str = "teacher_a") -> int:
        for agent in self.agents(username):
            if agent["name"] == name:
                return int(agent["id"])
        raise AssertionError(f"找不到应用：{name}")

    def skills(self, username: str = "teacher_a") -> list[dict]:
        resp = self.get("/api/skills", username)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return resp.get_json()["skills"]

    def skill_id(self, name: str, username: str = "teacher_a") -> int:
        for skill in self.skills(username):
            if skill["name"] == name:
                return int(skill["id"])
        raise AssertionError(f"找不到技能：{name}")

    def new_conversation(self, agent_id: int, username: str = "student_a1") -> int:
        resp = self.post("/api/conversations", username, json={"agent_id": int(agent_id)})
        assert resp.status_code == 201, resp.get_data(as_text=True)
        return int(resp.get_json()["id"])

    def ask(self, conversation_id: int, question: str, username: str = "student_a1",
            **extra) -> dict:
        resp = self.post(
            f"/api/conversations/{int(conversation_id)}/ask",
            username,
            json={"q": question, **extra},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return resp.get_json()

    def upload_skill(self, text: str, username: str = "teacher_a"):
        return self.post("/api/skills", username, json={"text": text})

    def audit(self, username: str = "teacher_a") -> list[dict]:
        resp = self.get("/api/mcp/audit", username)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return resp.get_json()["audit"]

    # ---------------------------------------------------------------- SSE
    @staticmethod
    def parse_sse(raw: str) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            lines = block.splitlines()
            name = ""
            data = ""
            for line in lines:
                if line.startswith("event: "):
                    name = line[len("event: "):]
                elif line.startswith("data: "):
                    data = line[len("data: "):]
            events.append((name, json.loads(data) if data else {}))
        return events


SKILL_TEMPLATE = """名称: {name}
适用条件: {triggers}
资料: {material}
工具: {tools}
启用: {enabled}

{body}
"""


def skill_text(name="测试技能", triggers="测试", material="", tools="knowledge_search",
               enabled="是", body="这是一段技能说明正文，用于验收解析与路由。"):
    return SKILL_TEMPLATE.format(
        name=name, triggers=triggers, material=material, tools=tools,
        enabled=enabled, body=body,
    )
