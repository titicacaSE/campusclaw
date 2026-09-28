"""场景级验收测试 —— 逐条覆盖 `openspec/.../specs/auth-upload/spec.md` 的 Requirement / Scenario。

对应需求：FR-01 登录与会话、FR-02 角色权限、FR-03 班级隔离、FR-04 上传与入库、FR-05 预置数据、
          NFR-01 密码与密钥、NFR-02 部署与可观测、NFR-03 可验证性与可追溯。

运行：
    python tests/test_acceptance.py -v

设计要点
--------
1. 每个用例使用独立的临时 `DATABASE` 与 `UPLOAD_DIR`；`create_app` 会自动建表并写入种子数据，
   因此用例之间互不污染，可重复运行。
2. 走 Flask 官方 `test_client`，覆盖完整路由栈（蓝图 + 装饰器 + 错误处理）。
3. 关键断言不只相信 HTTP 响应，还用 `sqlite3` 直查数据库真实行数 —— 负路径的「无脏数据」
   必须以落库结果为准，而不是以状态码为准。
4. 规约「唯一标准是能否直接判定通过与否」，故所有断言都是确定性的等值/包含判断。
"""
from __future__ import annotations

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ConfigError, create_app  # noqa: E402
from app.seed import DEMO_PASSWORDS  # noqa: E402

SECRET = "unit-test-secret-key-0123456789abcdef"
CLASS_A, CLASS_B = 1, 2  # 种子写入顺序决定：班级 A = 1，班级 B = 2


class Base(unittest.TestCase):
    """提供临时库、应用工厂与若干断言辅助方法。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="campusclaw-test-"))
        self.db_path = self.tmp / "app.db"
        self.upload_dir = self.tmp / "uploads"
        self.app = self._make_app()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 工厂
    def _make_app(self):
        return create_app(
            SECRET_KEY=SECRET,
            DATABASE=str(self.db_path),
            UPLOAD_DIR=str(self.upload_dir),
            TESTING=True,
        )

    # ---------------------------------------------------------------- 辅助
    def client(self):
        return self.app.test_client()

    def login(self, c, username, password=None):
        return c.post(
            "/login",
            data={"username": username, "password": password or DEMO_PASSWORDS[username]},
        )

    def sql(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def scalar(self, query, params=()):
        rows = self.sql(query, params)
        return rows[0][0] if rows else None

    def upload(self, c, filename, content: bytes, **form):
        data = {"file": (io.BytesIO(content), filename)}
        for key, value in form.items():
            data[key] = value
        return c.post("/api/materials/upload", data=data, content_type="multipart/form-data")

    def count_materials(self, class_id=None):
        if class_id is None:
            return self.scalar("SELECT COUNT(*) FROM materials")
        return self.scalar("SELECT COUNT(*) FROM materials WHERE class_id = ?", (class_id,))

    def count_entries(self, class_id=None):
        if class_id is None:
            return self.scalar("SELECT COUNT(*) FROM knowledge_entries")
        return self.scalar("SELECT COUNT(*) FROM knowledge_entries WHERE class_id = ?", (class_id,))

    def stored_file_count(self):
        if not self.upload_dir.exists():
            return 0
        return sum(1 for p in self.upload_dir.rglob("*") if p.is_file())


# ===================================================================== FR-01
class FR01LoginSession(Base):
    """Requirement: 用户登录与会话建立"""

    def test_s1_teacher_and_student_login_succeed(self):
        """Scenario: 教师与学生登录成功 —— 建立会话且记录 user_id / role / class_id。"""
        with self.client() as c:
            r = self.login(c, "teacher_a")
            self.assertEqual(r.status_code, 302)
            self.assertIn("/materials", r.headers.get("Location", ""))
            me = c.get("/api/materials").get_json()
            self.assertEqual(me["role"], "teacher")
            self.assertEqual(me["class_id"], CLASS_A)

        with self.client() as c:
            r = self.login(c, "student_a1")
            self.assertEqual(r.status_code, 302)
            me = c.get("/api/materials").get_json()
            self.assertEqual(me["role"], "student")
            self.assertEqual(me["class_id"], CLASS_A)

    def test_s2_wrong_password_rejected_without_leak(self):
        """Scenario: 错误密码登录失败 —— 不建会话、不下发 Cookie、不泄露哈希。"""
        with self.client() as c:
            r = self.login(c, "teacher_a", password="definitely-wrong")
            self.assertEqual(r.status_code, 401)
            self.assertNotIn("campusclaw_session", r.headers.get("Set-Cookie", ""))
            body = r.get_data(as_text=True).lower()
            self.assertNotIn("$2", body)          # bcrypt 哈希前缀
            self.assertNotIn("password_hash", body)
            # 落库校验：仍未登录 → 受保护 API 返回 401
            self.assertEqual(c.get("/api/materials").status_code, 401)

    def test_s3_anonymous_page_302_api_401_without_data(self):
        """Scenario: 未登录访问受保护资源 —— 页面 302，API 401 且不含任何班级数据。"""
        with self.client() as c:
            page = c.get("/materials")
            self.assertEqual(page.status_code, 302)
            self.assertIn("/login", page.headers.get("Location", ""))

            api = c.get("/api/materials")
            self.assertEqual(api.status_code, 401)
            raw = api.get_data(as_text=True)
            self.assertNotIn("讲义", raw)
            self.assertNotIn("A 班", raw)
            self.assertNotIn(".md", raw)


# ===================================================================== FR-02
class FR02RolePermission(Base):
    """Requirement: 角色权限"""

    def test_s1_student_upload_rejected_403_no_side_effect(self):
        """Scenario: 学生上传被拒绝 —— 403 且两表行数不变、无新落盘文件。"""
        before_m, before_k = self.count_materials(), self.count_entries()
        before_files = self.stored_file_count()
        with self.client() as c:
            self.login(c, "student_a1")
            r = self.upload(c, "student-try.md", "学生尝试上传".encode("utf-8"))
            self.assertEqual(r.status_code, 403)
            self.assertIn("无权限", r.get_data(as_text=True))

        self.assertEqual(self.count_materials(), before_m)
        self.assertEqual(self.count_entries(), before_k)
        self.assertEqual(self.stored_file_count(), before_files)

    def test_s2_teacher_upload_allowed_201_same_class(self):
        """Scenario: 教师上传被允许 —— 201 且含 material_id，两表均归属教师所属班级。"""
        with self.client() as c:
            self.login(c, "teacher_a")
            r = self.upload(c, "三角函数讲义.md", "三角函数：正弦、余弦、正切。".encode("utf-8"))
            self.assertEqual(r.status_code, 201)
            payload = r.get_json()
            self.assertIn("material_id", payload)
            self.assertEqual(payload["class_id"], CLASS_A)

            mid = payload["material_id"]
            self.assertEqual(self.scalar("SELECT class_id FROM materials WHERE id = ?", (mid,)), CLASS_A)
            self.assertEqual(
                self.scalar("SELECT class_id FROM knowledge_entries WHERE material_id = ?", (mid,)),
                CLASS_A,
            )

    def test_s3_student_readonly_list_no_write_entry(self):
        """Scenario: 学生仅只读本班列表 —— 可见列表，但页面无上传入口、写接口仍拒绝。"""
        with self.client() as c:
            self.login(c, "student_a1")
            self.assertEqual(c.get("/api/materials").status_code, 200)
            html = c.get("/materials").get_data(as_text=True)
            self.assertNotIn('action="/materials/upload"', html)
            self.assertNotIn('enctype="multipart/form-data"', html)
            # 服务端仍拒绝学生写接口
            r = self.upload(c, "x.md", b"x")
            self.assertEqual(r.status_code, 403)


# ===================================================================== FR-03
class FR03ClassIsolation(Base):
    """Requirement: 班级隔离 —— 班级是数据边界，全部在服务端完成。"""

    def test_s1_cross_class_by_id_returns_404_without_leak(self):
        """Scenario: 跨班按 ID 访问材料被拒绝 —— 404，且响应不含 B 班任何内容。"""
        b_id = self.scalar("SELECT id FROM materials WHERE class_id = ? ORDER BY id LIMIT 1", (CLASS_B,))
        self.assertIsNotNone(b_id)

        with self.client() as c:
            self.login(c, "teacher_a")
            detail = c.get(f"/api/materials/{b_id}")
            self.assertEqual(detail.status_code, 404)
            raw = detail.get_data(as_text=True)
            self.assertNotIn("B 班", raw)
            self.assertNotIn("一元二次方程", raw)
            self.assertNotIn(".md", raw)

            dl = c.get(f"/api/materials/{b_id}/download")
            self.assertEqual(dl.status_code, 404)

    def test_s2_cross_class_enumeration_leaks_nothing(self):
        """Scenario: 跨班枚举列表不得泄露 B 班 —— 逐条归属班级 A，且无「B 班」标题。"""
        with self.client() as c:
            self.login(c, "student_a1")
            data = c.get("/api/materials").get_json()
            self.assertTrue(data["materials"])
            for item in data["materials"]:
                self.assertEqual(item["class_id"], CLASS_A)
                self.assertNotIn("B 班", item["title"])

    def test_s3_teacher_list_only_own_class_from_db(self):
        """Scenario: 材料列表仅含本班记录 —— 数据来自带班级过滤的查询。"""
        with self.client() as c:
            self.login(c, "teacher_a")
            data = c.get("/api/materials").get_json()
            db_titles = {
                r["title"]
                for r in self.sql("SELECT title FROM materials WHERE class_id = ?", (CLASS_A,))
            }
            api_titles = {m["title"] for m in data["materials"]}
            self.assertEqual(api_titles, db_titles)
            self.assertTrue(all("B 班" not in t for t in api_titles))

    def test_s4_upload_ignores_client_supplied_class(self):
        """Scenario: 上传时忽略客户端班级字段 —— 仍写入会话所属班级（无「代传」通道）。"""
        with self.client() as c:
            self.login(c, "teacher_a")
            r = self.upload(
                c,
                "跨班注入测试.md",
                "试图指定 B 班".encode("utf-8"),
                class_id=str(CLASS_B),
                **{"class": "班级 B"},
            )
            self.assertEqual(r.status_code, 201)
            payload = r.get_json()
            self.assertEqual(payload["class_id"], CLASS_A)
            self.assertEqual(
                self.scalar("SELECT class_id FROM materials WHERE id = ?", (payload["material_id"],)),
                CLASS_A,
            )


# ===================================================================== FR-04
class FR04UploadIngest(Base):
    """Requirement: 材料上传与知识库入库"""

    def test_s1_upload_then_listed_and_ingested(self):
        """Scenario: 上传后知识库与列表可查 —— 两表各 +1（同事务），列表出现该标题。"""
        m0, k0 = self.count_materials(CLASS_A), self.count_entries(CLASS_A)
        title = "勾股定理讲义"
        with self.client() as c:
            self.login(c, "teacher_a")
            r = self.upload(c, f"{title}.md", "a²+b²=c²".encode("utf-8"))
            self.assertEqual(r.status_code, 201)

            self.assertEqual(self.count_materials(CLASS_A), m0 + 1)
            self.assertEqual(self.count_entries(CLASS_A), k0 + 1)

            titles = [m["title"] for m in c.get("/api/materials").get_json()["materials"]]
            self.assertIn(title, titles)

    def test_s2_upload_visible_to_student_readonly(self):
        """Scenario: 上传后学生本班可见只读 —— 学生能看到，且仍无法写。"""
        title = "英语听力练习"
        with self.client() as tc:
            self.login(tc, "teacher_a")
            self.assertEqual(self.upload(tc, f"{title}.md", "listening".encode("utf-8")).status_code, 201)

        with self.client() as sc:
            self.login(sc, "student_a1")
            titles = [m["title"] for m in sc.get("/api/materials").get_json()["materials"]]
            self.assertIn(title, titles)
            # 学生无法修改/删除：写接口一律被拒（上传 403），且不存在 DELETE 路由（404/405）
            self.assertEqual(self.upload(sc, "modify.md", b"x").status_code, 403)
            some_id = sc.get("/api/materials").get_json()["materials"][0]["id"]
            self.assertIn(sc.delete(f"/api/materials/{some_id}").status_code, (404, 405))

    def test_s3_failures_produce_no_dirty_data(self):
        """Scenario: 上传失败时不产生脏数据 —— 400 且无残留行、无孤立文件。"""
        cases = [
            ("virus.exe", b"MZ..."),          # 不支持格式
            ("empty.txt", b""),                # 空文件
            ("broken.md", b"\xff\xfe\x00\x01"),  # 非 UTF-8 损坏文件
        ]
        with self.client() as c:
            self.login(c, "teacher_a")
            for name, content in cases:
                with self.subTest(file=name):
                    m0, k0 = self.count_materials(), self.count_entries()
                    f0 = self.stored_file_count()
                    r = self.upload(c, name, content)
                    self.assertEqual(r.status_code, 400)
                    self.assertEqual(self.count_materials(), m0, f"{name}: materials 不应新增")
                    self.assertEqual(self.count_entries(), k0, f"{name}: knowledge_entries 不应新增")
                    self.assertEqual(self.stored_file_count(), f0, f"{name}: 不应留下孤立文件")


# ===================================================================== FR-05
class FR05SeedData(Base):
    """Requirement: 预置核心数据"""

    def test_s1_seed_classes_and_users(self):
        """Scenario: 种子数据满足双班与用户 —— 班级 A/B 与所需账号齐全，密码已哈希。"""
        names = {r["name"] for r in self.sql("SELECT name FROM classes")}
        self.assertEqual(names, {"班级 A", "班级 B"})

        users = {r["username"]: r for r in self.sql("SELECT * FROM users")}
        for username in ("teacher_a", "student_a1", "student_b1"):
            self.assertIn(username, users)
            self.assertTrue(users[username]["password_hash"])
            self.assertFalse(users[username]["password_hash"] == DEMO_PASSWORDS[username])

        # 角色与班级归属
        self.assertEqual(users["teacher_a"]["role"], "teacher")
        self.assertEqual(users["student_a1"]["role"], "student")
        self.assertEqual(users["student_a1"]["class_id"], CLASS_A)
        self.assertEqual(users["student_b1"]["class_id"], CLASS_B)

    def test_s2_two_class_titles_distinguishable_and_six_tables(self):
        """Scenario: 两班材料标题可区分 —— 含「A 班」「B 班」且归属正确；六类核心表已建。"""
        rows = self.sql("SELECT c.name AS cls, m.title AS title FROM materials m JOIN classes c ON c.id = m.class_id")
        a_titles = [r["title"] for r in rows if r["cls"] == "班级 A"]
        b_titles = [r["title"] for r in rows if r["cls"] == "班级 B"]
        self.assertTrue(any("A 班" in t for t in a_titles))
        self.assertTrue(any("B 班" in t for t in b_titles))

        tables = {r[0] for r in self.sql("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("classes", "users", "handouts", "assignments", "assistants", "skills"):
            self.assertIn(table, tables, f"六类核心表缺少 {table}")


# ===================================================================== NFR-01
class NFR01Secrets(Base):
    """Requirement: 密码与密钥安全"""

    def test_s1_no_plaintext_password_bcrypt_format(self):
        """Scenario: 库中无明文密码 —— 不等于明文且为 bcrypt 格式。"""
        for row in self.sql("SELECT username, password_hash FROM users"):
            self.assertTrue(row["password_hash"].startswith("$2"), row["username"])
            self.assertNotEqual(row["password_hash"], DEMO_PASSWORDS.get(row["username"]))

        cols = {r[1] for r in self.sql("PRAGMA table_info(users)")}
        self.assertIn("password_hash", cols)
        self.assertNotIn("password", cols)

    def test_s2_login_uses_hash_comparison(self):
        """Scenario: 登录校验使用哈希比较 —— 正确口令建会话，错误口令失败。"""
        with self.client() as c:
            self.assertEqual(self.login(c, "teacher_a").status_code, 302)
        with self.client() as c:
            self.assertEqual(self.login(c, "teacher_a", password="wrong").status_code, 401)

    def test_s3_missing_secret_key_fails_startup(self):
        """Scenario: 缺少会话密钥时的启动行为 —— 启动失败，不落入不安全默认值。"""
        clean_env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
        with patch.dict(os.environ, clean_env, clear=True):
            with self.assertRaises(ConfigError):
                create_app(DATABASE=str(self.db_path), UPLOAD_DIR=str(self.upload_dir))
        # 过短的密钥同样被拒（不安全默认值）
        with self.assertRaises(ConfigError):
            create_app(SECRET_KEY="short", DATABASE=str(self.db_path), UPLOAD_DIR=str(self.upload_dir))


# ===================================================================== NFR-02
class NFR02DeployHealth(Base):
    """Requirement: 部署与可观测"""

    def test_s1_health_returns_200_json(self):
        """Scenario: Compose 启动后可访问 —— /health 返回 200 且表明服务可用（DB 层）。"""
        with self.client() as c:
            r = c.get("/health")
            self.assertEqual(r.status_code, 200)
            payload = r.get_json()
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["database"], "ok")

    def test_s2_health_ignores_login_state(self):
        """Scenario: health 不依赖登录态 —— 无 Cookie 仍 200 JSON 且不重定向。"""
        with self.client() as c:
            r = c.get("/health")
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("Location", r.headers)
            self.assertEqual(r.headers["Content-Type"].split(";")[0], "application/json")

    def test_s2b_health_503_when_db_unreadable(self):
        """Scenario 补充：数据库不可读时 /health 返回 503。"""
        with patch("app.db.database_ok", return_value=False):
            with self.client() as c:
                r = c.get("/health")
                self.assertEqual(r.status_code, 503)
                self.assertEqual(r.get_json()["status"], "error")

    def test_s3_data_persists_across_restart(self):
        """Scenario: 重建容器后数据仍在 —— 同一 DATABASE/UPLOAD_DIR 重建应用后仍可查询。"""
        title = "持久化验证讲义"
        with self.client() as c:
            self.login(c, "teacher_a")
            r = self.upload(c, f"{title}.md", "重启后应仍可读".encode("utf-8"))
            self.assertEqual(r.status_code, 201)
            mid = r.get_json()["material_id"]

        restarted = self._make_app()  # 等价于 down/up 且未删 volume
        with restarted.test_client() as c2:
            self.login(c2, "teacher_a")
            titles = [m["title"] for m in c2.get("/api/materials").get_json()["materials"]]
            self.assertIn(title, titles)
            detail = c2.get(f"/api/materials/{mid}")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("重启后应仍可读", detail.get_json()["knowledge_entries"][0]["body_text"])


# ===================================================================== NFR-03
class NFR03Traceability(Base):
    """Requirement: 可验证性与可追溯"""

    def test_s2_spec_is_source_of_truth(self):
        """Scenario: 新会话凭规约复现 —— 仅读 openspec/ 即可回答三条硬性要求。"""
        root = Path(__file__).resolve().parent.parent
        spec = (root / "openspec/changes/add-auth-rbac-class-knowledge/specs/auth-upload/spec.md").read_text("utf-8")
        # 三条硬性要求的关键句必须存在于规约原文
        self.assertIn("302 重定向至 `/login`", spec)          # 登录引导
        self.assertIn("A 班用户 MUST NOT 读取", spec)          # 班级隔离
        self.assertIn("学生调用上传或管理接口 MUST 被拒绝", spec)  # 教师专属上传
        # 四件套齐全
        base = root / "openspec/changes/add-auth-rbac-class-knowledge"
        for name in ("proposal.md", "design.md", "tasks.md"):
            self.assertTrue((base / name).exists(), f"缺少规约文件 {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
