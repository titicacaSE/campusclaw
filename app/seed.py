"""预置核心数据（FR-05）

规约依据：
  - FR-05 S1：初始化或 Compose 首次启动 → 存在班级 A、班级 B；教师 A（A 班）、
    学生 A1（A 班）、学生 B1（B 班）；各用户具备可登录用户名与已哈希密码。
  - FR-05 S2：至少一条标题含「A 班」与一条含「B 班」的材料；六类核心表已创建（可各含 0~1 条占位行）。

说明：在规约要求的三名用户之外，额外预置「教师 B（班级 B）」。原因是 FR-04 要求材料与知识库条目
必须关联「上传者所属班级」——若 B 班样本材料由 A 班教师署名，种子数据自身就会破坏该不变量；
同时教师 B 可用于验证「B 班教师既不能上传到 A 班，也不能查看 A 班材料」。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .auth import hash_password
from .db import connect, utcnow

# 演示口令：仅用于课程演示环境（README 中公开说明）；生产环境必须替换（NFR-01）
DEMO_PASSWORDS = {
    "teacher_a": "TeacherA@2026",
    "teacher_b": "TeacherB@2026",
    "student_a1": "StudentA1@2026",
    "student_b1": "StudentB1@2026",
}

CLASSES = [
    ("班级 A", "初一"),
    ("班级 B", "初一"),
]

# (username, display_name, role, class_name)
USERS = [
    ("teacher_a", "教师 A", "teacher", "班级 A"),
    ("teacher_b", "教师 B", "teacher", "班级 B"),
    ("student_a1", "学生 A1", "student", "班级 A"),
    ("student_b1", "学生 B1", "student", "班级 B"),
]

# (class_name, uploader, title, subject, body)
# 标题显式含「A 班」/「B 班」以便 A8/FR-05 S2 直接判定
MATERIALS = [
    (
        "班级 A",
        "teacher_a",
        "A 班语文讲义：记叙文阅读方法（第 1 讲）",
        "语文",
        "A 班语文讲义 · 第 1 讲：记叙文阅读方法\n\n"
        "一、要素梳理。阅读记叙文先抓六要素：时间、地点、人物、起因、经过、结果。"
        "先用一句话概括事件，再进入细节分析。\n\n"
        "二、线索与顺序。常见线索有人物、事件、物品、情感；顺序分为顺叙、倒叙、插叙。"
        "倒叙的作用是设置悬念，插叙的作用是补充背景、丰满人物。\n\n"
        "三、人物形象。从正面描写（外貌、语言、动作、神态、心理）与侧面描写两条路径归纳人物性格，"
        "答题时用「特征 + 依据」的结构，做到有据可依。\n\n"
        "四、语言赏析。三步走：判断手法、解释内容、写出效果与情感。\n\n"
        "五、本讲作业。完成《秋天的怀念》第 1—5 题，用「特征 + 依据」的格式写出母亲的形象。",
    ),
    (
        "班级 A",
        "teacher_a",
        "A 班数学讲义：有理数运算易错点（第 2 讲）",
        "数学",
        "A 班数学讲义 · 第 2 讲：有理数运算易错点\n\n"
        "一、符号问题。减去一个数等于加上它的相反数，改写减法时括号内的符号必须一起变。\n\n"
        "二、乘除符号法则。两数相乘同号得正、异号得负；多因数连乘先数负因数个数，奇数个为负。\n\n"
        "三、运算顺序。先乘方、再乘除、最后加减；注意 -2² 与 (-2)² 的区别。\n\n"
        "四、练习建议。每天 10 道混合运算限时 8 分钟，错题按「符号错、顺序错、法则错」分类记录。",
    ),
    (
        "班级 B",
        "teacher_b",
        "B 班数学讲义：一元二次方程解法（第 1 讲）",
        "数学",
        "B 班数学讲义 · 第 1 讲：一元二次方程的四种解法\n\n"
        "一、直接开平方法。适用于形如 (x+m)² = n（n ≥ 0）的方程，解为 x = -m ± √n。\n\n"
        "二、配方法。二次项系数化为 1、常数项移项、两边加上一次项系数一半的平方、开方。\n\n"
        "三、公式法。x = (-b ± √(b²-4ac)) / 2a，判别式 Δ = b²-4ac："
        "Δ>0 两个不等实根，Δ=0 两个相等实根，Δ<0 无实数根。\n\n"
        "四、因式分解法。能分解为 (x-m)(x-n)=0 时，直接得 x=m 或 x=n。\n\n"
        "五、本讲作业。课本第 42 页第 3、5、7 题，每题注明选用哪种解法及理由。",
    ),
    (
        "班级 B",
        "teacher_b",
        "B 班英语讲义：Unit 1 核心词汇与句型（第 2 讲）",
        "英语",
        "B 班英语讲义 · 第 2 讲：Unit 1 核心词汇与句型\n\n"
        "一、必背单词。name, meet, friend, class, grade, student, teacher, welcome, family, subject。\n\n"
        "二、重点短语。be from, be good at, a lot of, look after, get up, after school。\n\n"
        "三、核心句型。What's your name? / Where are you from? / What class are you in? / Do you like music?\n\n"
        "四、语法要点。一般现在时主语为第三人称单数时动词加 -s 或 -es；have 的第三人称单数是 has。",
    ),
]

ASSIGNMENTS = {"班级 A": "A 班第 1 周作业", "班级 B": "B 班第 1 周作业"}


def _stable_suffix(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def seed_database(conn, upload_dir: Path, quiet: bool = False) -> bool:
    """写入种子数据；已存在用户则跳过（幂等）。返回是否实际写入。"""
    existing = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if existing:
        if not quiet:
            print("[seed] 已存在用户数据，跳过种子写入")
        return False

    now = utcnow()
    upload_dir = Path(upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    class_id: dict[str, int] = {}
    for name, grade in CLASSES:
        cur = conn.execute(
            "INSERT INTO classes (name, grade, created_at) VALUES (?, ?, ?)", (name, grade, now)
        )
        class_id[name] = cur.lastrowid

    user_id: dict[str, int] = {}
    for username, display_name, role, class_name in USERS:
        cur = conn.execute(
            """
            INSERT INTO users (username, display_name, role, class_id, password_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                display_name,
                role,
                class_id[class_name],
                hash_password(DEMO_PASSWORDS[username]),
                now,
            ),
        )
        user_id[username] = cur.lastrowid

    knowledge_count = 0
    for class_name, uploader, title, subject, body in MATERIALS:
        cid = class_id[class_name]
        safe = "".join(ch if ch not in '/\\:*?"<>|' else "_" for ch in title)
        dest_dir = upload_dir / str(cid)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"seed_{_stable_suffix(title)}_{safe}.md"
        dest.write_text(body, encoding="utf-8")

        cur = conn.execute(
            """
            INSERT INTO materials (title, class_id, file_path, original_filename, ext,
                                   size_bytes, subject, uploaded_by, parse_status, created_at)
            VALUES (?, ?, ?, ?, '.md', ?, ?, ?, 'ok', ?)
            """,
            (
                title,
                cid,
                str(dest),
                f"{title}.md",
                len(body.encode("utf-8")),
                subject,
                user_id[uploader],
                now,
            ),
        )
        material_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO knowledge_entries
              (material_id, class_id, ord, body_text, char_count, created_at)
            VALUES (?, ?, 0, ?, ?, ?)
            """,
            (material_id, cid, body, len(body), now),
        )
        knowledge_count += 1

        # 六类核心结构：讲义占位行（关联到刚入库的材料）
        conn.execute(
            """
            INSERT INTO handouts (class_id, title, subject, material_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cid, f"{title}（讲义）", subject, material_id, now),
        )

    for class_name, title in ASSIGNMENTS.items():
        conn.execute(
            """
            INSERT INTO assignments (class_id, title, due_date, created_by, created_at)
            VALUES (?, ?, NULL, ?, ?)
            """,
            (class_id[class_name], title, user_id["teacher_a"], now),
        )

    # 六类核心结构：助手、技能各一条占位行
    cur = conn.execute(
        """
        INSERT INTO assistants (name, description, scope, class_id, owner_id, created_at)
        VALUES (?, ?, 'class', ?, ?, ?)
        """,
        ("班级教研助手", "面向本班的材料问答助手（MVP 占位，不实现对话）", class_id["班级 A"], user_id["teacher_a"], now),
    )
    assistant_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO skills (name, description, assistant_id, enabled, created_at)
        VALUES (?, ?, ?, 1, ?)
        """,
        ("讲义要点提炼", "从讲义中抽取要点（MVP 占位，不实现运行时）", assistant_id, now),
    )

    conn.commit()

    if not quiet:
        print(
            f"[seed] 完成：{len(CLASSES)} 个班级 / {len(USERS)} 个账号 / "
            f"{len(MATERIALS)} 份材料 / {knowledge_count} 条知识库记录 / 六类核心表均已建"
        )
        for username, password in DEMO_PASSWORDS.items():
            print(f"[seed]   演示账号 {username} / {password}")
    return True


def reseed(db_path, upload_dir: Path, quiet: bool = False) -> bool:
    """清空业务表并重新写入种子数据（脚本 --force 使用）。

    删除顺序 MUST 是「先子表后父表」，否则在 `PRAGMA foreign_keys = ON` 下会触发
    FOREIGN KEY constraint failed。关键依赖：
      knowledge_entries.material_id → materials
      handouts.material_id         → materials
      skills.assistant_id          → assistants
      materials.uploaded_by / assignments.created_by / assistants.owner_id → users
      users.class_id               → classes
    """
    conn = connect(db_path)
    try:
        for table in (
            "knowledge_entries",  # 依赖 materials / classes，先删
            "skills",             # 依赖 assistants
            "handouts",           # 依赖 materials / classes
            "assignments",        # 依赖 classes / users
            "materials",          # 此时 knowledge_entries 与 handouts 已清空，可安全删除
            "assistants",         # 此时 skills 已清空
            "users",              # 此时 materials / assignments / assistants 已清空
            "classes",            # 最后删（被上述所有表引用）
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
        return seed_database(conn, Path(upload_dir), quiet=quiet)
    finally:
        conn.close()


__all__ = ["seed_database", "reseed", "DEMO_PASSWORDS"]
