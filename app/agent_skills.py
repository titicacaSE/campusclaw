"""技能：文件解析、拒收、内置播种、候选集与确定性路由。

规约依据 `add-agent-platform-mcp-tools`：
  - FR-4.2 技能以单一文本文件定义：空行以上元数据，空行以下说明正文；
  - FR-4.3 路由在服务端完成、不调模型：候选集三层收窄 → 切词 → 子串计分 → 取最高分，
    同分保留编号较小者，零分不选；
  - FR-4.4 绑定与启用状态不随会话快照固化，每次提问重读。

刻意**不**做语义匹配：路由必须可复现，课堂要能用「把触发词改成高频虚词」来证明
它是字面计分而不是模型判断。
"""
from __future__ import annotations

import re

from flask import Blueprint, jsonify, request

from . import db
from .auth import current_user, teacher_required

bp = Blueprint("agent_skills", __name__)

NAME_MAX = 32
TRIGGER_MAX = 120
MATERIAL_FILTER_MAX = 32
BODY_MAX = 2000
FILE_MAX = 8000

TRIGGER_SEPARATORS = re.compile(r"[，,、;；|]+")
META_RE = re.compile(r"^([^:：]{1,20})[:：]\s*(.*)$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

META_KEYS = ("名称", "适用条件", "资料", "工具", "启用")
REQUIRED_KEYS = ("名称", "适用条件", "启用")

# 本阶段只接受内置检索工具；第 6 课扩展为 `工具名@服务器名`
BUILTIN_TOOL = "knowledge_search"

# 命中「解题引导」时追加的硬性要求（FR-4.1）
GUIDED_ANSWER_RULE = (
    "【硬性要求】本技能是解题引导：不得写出最终答案、选项、得数或完整解题过程；"
    "只指出错在哪一步、下一步该检查什么。"
)

BUILTIN_SKILLS = (
    {
        "name": "解题引导",
        "triggers": "解题,怎么做,不会做,思路,步骤,卡住",
        "material_filter": "",
        "tools": BUILTIN_TOOL,
        "body": (
            "你正在做解题引导，不是给答案。\n"
            "一、先确认题目条件：让学生复述已知与所求，指出是否漏读条件。\n"
            "二、定位卡点：把解法拆成若干步，判断学生停在哪一步，并说出判断依据。\n"
            "三、只给下一步：用提问的方式引导，例如「这一步应该先处理符号还是先合并同类项？」\n"
            "四、禁止给结论：不写最终答案、不写选项、不写得数、不给完整解题过程。\n"
            "五、若学生坚持要答案，仍然只给下一步提示，并说明「直接给答案会削弱你的判断力」。"
        ),
    },
    {
        "name": "作业批改",
        "triggers": "批改,检查作业,对不对,评分,订正",
        "material_filter": "",
        "tools": BUILTIN_TOOL,
        "body": (
            "你正在批改作业。\n"
            "一、逐题给出「正确 / 错误 / 部分正确」三档判定，并说明判据来自哪条资料。\n"
            "二、错误题必须指出错因类型：概念错、符号错、顺序错、计算错、审题错。\n"
            "三、给出可执行的改法，一条即可，不要长篇大论。\n"
            "四、最后给一个总体建议，不超过两句话。"
        ),
    },
    {
        "name": "错题讲解",
        "triggers": "错题,做错,订正,为什么错,复盘",
        "material_filter": "",
        "tools": BUILTIN_TOOL,
        "body": (
            "你正在讲错题。\n"
            "一、先复述学生的错误做法，确认你理解的是同一个错误。\n"
            "二、指出这个错误属于哪一类：概念理解、规则记忆、步骤遗漏、粗心计算。\n"
            "三、给一个同类变式题，让学生自己判断关键差异。\n"
            "四、总结一条可复用的检查清单，不超过三条。"
        ),
    },
)


class SkillError(ValueError):
    """技能不合规。`code` 直接进响应，`field` 指明是哪一项。"""

    def __init__(self, code: str, message: str, field: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict:
        payload = {"error": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        if self.code == "missing_fields":
            payload["missing_fields"] = [self.field]
        return payload


# ------------------------------------------------------------------ 校验


def split_triggers(text: str) -> list[str]:
    """按中英文逗号、顿号、分号、竖线切词；去空白、转小写、丢弃空串（保序去重）。"""
    words: list[str] = []
    for raw in TRIGGER_SEPARATORS.split(text or ""):
        word = raw.strip().lower()
        if word and word not in words:
            words.append(word)
    return words


def is_tool_ref(value: str) -> bool:
    """合法工具字段：`knowledge_search` 或 `工具名@服务器名`。"""
    value = (value or "").strip()
    if value == BUILTIN_TOOL:
        return True
    if "@" not in value:
        return False
    tool, _, server = value.partition("@")
    return bool(TOOL_NAME_RE.match(tool) and SERVER_NAME_RE.match(server))


def parse_skill_text(text: str) -> dict:
    """解析技能文件。返回 `{name, triggers, material_filter, tools, enabled, body}`。"""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        raise SkillError("empty_file", "技能文件为空", "文件")
    if len(raw) > FILE_MAX:
        raise SkillError("file_too_long", f"文件整体不得超过 {FILE_MAX} 字", "文件")

    parts = re.split(r"\n[ \t]*\n", raw, maxsplit=1)
    if len(parts) != 2:
        raise SkillError("missing_body", "空行以下必须有技能说明正文", "正文")
    meta_block, body = parts[0], parts[1].strip()

    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        line = line.strip()
        if not line:
            continue
        match = META_RE.match(line)
        if not match:
            raise SkillError("bad_meta_line", f"元数据行格式应为「键: 值」：{line[:20]}", "元数据")
        key, value = match.group(1).strip(), match.group(2).strip()
        if key not in META_KEYS:
            raise SkillError("unknown_field", f"未知元数据字段：{key}", key)
        if key in meta:
            raise SkillError("duplicate_field", f"元数据字段重复：{key}", key)
        meta[key] = value

    for key in REQUIRED_KEYS:
        if not meta.get(key, "").strip():
            raise SkillError("missing_fields", f"缺少必填字段：{key}", key)

    name = meta["名称"].strip()
    if len(name) > NAME_MAX:
        raise SkillError("name_too_long", f"名称不得超过 {NAME_MAX} 字", "名称")

    triggers = meta["适用条件"].strip()
    if len(triggers) > TRIGGER_MAX:
        raise SkillError("triggers_too_long", f"适用条件不得超过 {TRIGGER_MAX} 字", "适用条件")
    if not split_triggers(triggers):
        raise SkillError("missing_fields", "适用条件至少要有一个可切分的词", "适用条件")

    material_filter = meta.get("资料", "").strip()
    if len(material_filter) > MATERIAL_FILTER_MAX:
        raise SkillError(
            "material_filter_too_long", f"资料不得超过 {MATERIAL_FILTER_MAX} 字", "资料"
        )

    tools = meta.get("工具", BUILTIN_TOOL).strip() or BUILTIN_TOOL
    if not is_tool_ref(tools):
        raise SkillError(
            "invalid_tool",
            "工具字段只接受 knowledge_search 或「工具名@服务器名」"
            "（工具名小写、无连字符；服务器名可含连字符）",
            "工具",
        )

    enabled_raw = meta["启用"].strip()
    if enabled_raw in ("是", "true", "True", "TRUE", "1", "yes"):
        enabled = True
    elif enabled_raw in ("否", "false", "False", "FALSE", "0", "no"):
        enabled = False
    else:
        raise SkillError("invalid_enabled", "启用只接受「是」或「否」", "启用")

    if not body:
        raise SkillError("missing_body", "空行以下必须有技能说明正文", "正文")
    if len(body) > BODY_MAX:
        raise SkillError("body_too_long", f"说明正文不得超过 {BODY_MAX} 字", "正文")

    return {
        "name": name,
        "triggers": triggers,
        "material_filter": material_filter,
        "tools": tools,
        "enabled": enabled,
        "body": body,
    }


# ------------------------------------------------------------------ 内置技能


def ensure_builtins(class_id: int, created_by: int | None = None) -> None:
    """幂等播种三项内置技能（已存在则跳过，不覆盖教师的修改）。"""
    now = db.utcnow()
    for spec in BUILTIN_SKILLS:
        db.execute(
            "INSERT OR IGNORE INTO agent_skills"
            " (class_id, name, triggers, material_filter, tools, body, source_text,"
            "  is_builtin, enabled, created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
            (
                int(class_id), spec["name"], spec["triggers"], spec["material_filter"],
                spec["tools"], spec["body"], "", created_by, now, now,
            ),
        )
    db.get_conn().commit()


def list_skills(class_id: int) -> list:
    ensure_builtins(class_id)
    return db.query(
        "SELECT * FROM agent_skills WHERE class_id = ? ORDER BY id",
        (int(class_id),),
    )


def get_skill(class_id: int, skill_id: int):
    return db.query_one(
        "SELECT * FROM agent_skills WHERE class_id = ? AND id = ?",
        (int(class_id), int(skill_id)),
    )


# ------------------------------------------------------------------ 路由


def candidates(class_id: int, agent_id: int) -> list:
    """候选集：本班 → 该智能体已绑定 → 已启用。`class_id` 是第一条谓词。"""
    return db.query(
        "SELECT s.* FROM agent_skills s"
        " JOIN tutor_agent_skills b ON b.skill_id = s.id"
        " WHERE s.class_id = ? AND b.agent_id = ? AND b.class_id = ? AND s.enabled = 1"
        " ORDER BY s.id ASC",
        (int(class_id), int(agent_id), int(class_id)),
    )


def score_skill(row, question: str) -> int:
    haystack = (question or "").lower()
    return sum(1 for word in split_triggers(row["triggers"]) if word in haystack)


def route_skill(class_id: int, agent_id: int, question: str):
    """返回命中的技能行；零分返回 None；同分保留编号较小者。"""
    best = None
    best_score = 0
    for row in candidates(class_id, agent_id):        # 已按 id 升序
        score = score_skill(row, question)
        if score > best_score:                        # 严格大于 → 同分不替换
            best, best_score = row, score
    return best if best_score > 0 else None


def is_guided(row) -> bool:
    return row is not None and "解题引导" in str(row["name"])


# ------------------------------------------------------------------ HTTP


def _payload() -> dict:
    payload: dict = {}
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        payload.update(body)
    if request.form:
        payload.update(request.form.to_dict())
    payload.update(request.args.to_dict())
    return payload


def _serialize(row) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "triggers": row["triggers"],
        "triggers_list": split_triggers(row["triggers"]),
        "material_filter": row["material_filter"],
        "tools": [t.strip() for t in str(row["tools"]).split(",") if t.strip()],
        "body": row["body"],
        "is_builtin": bool(row["is_builtin"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@bp.get("/api/skills")
def api_list_skills():
    user = current_user()
    if not user:
        return jsonify(error="unauthorized", message="未登录"), 401
    rows = list_skills(int(user["class_id"]))
    return jsonify(skills=[_serialize(row) for row in rows], total=len(rows))


@bp.get("/api/skills/<int:skill_id>")
def api_skill(skill_id: int):
    user = current_user()
    if not user:
        return jsonify(error="unauthorized", message="未登录"), 401
    row = get_skill(int(user["class_id"]), skill_id)
    if row is None:
        return jsonify(error="not_found", message="技能不存在"), 404
    return jsonify(_serialize(row))


@bp.post("/api/skills")
@teacher_required
def api_create_skill():
    """上传技能：JSON 的 `text`，或 multipart 文件字段。"""
    user = current_user()
    class_id = int(user["class_id"])

    text = ""
    uploaded = request.files.get("file")
    if uploaded is not None:
        raw = uploaded.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", "replace")
    if not text:
        text = str(_payload().get("text") or "")

    try:
        parsed = parse_skill_text(text)
    except SkillError as exc:
        return jsonify(exc.to_dict()), 400

    if db.query_one(
        "SELECT id FROM agent_skills WHERE class_id = ? AND name = ?", (class_id, parsed["name"])
    ):
        return jsonify(
            error="duplicate_name", message=f"班内已存在同名技能：{parsed['name']}", field="名称"
        ), 400

    invalid = _invalid_tool_refs(class_id, parsed["tools"])
    if invalid:
        return jsonify(
            error="unknown_tool",
            message=f"服务器未提供该工具：{invalid[0]}",
            field="工具",
        ), 400

    now = db.utcnow()
    cur = db.get_conn().execute(
        "INSERT INTO agent_skills"
        " (class_id, name, triggers, material_filter, tools, body, source_text,"
        "  is_builtin, enabled, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (
            class_id, parsed["name"], parsed["triggers"], parsed["material_filter"],
            parsed["tools"], parsed["body"], text, 1 if parsed["enabled"] else 0,
            int(user["user_id"]), now, now,
        ),
    )
    db.get_conn().commit()
    row = get_skill(class_id, int(cur.lastrowid))
    return jsonify(_serialize(row)), 201


@bp.patch("/api/skills/<int:skill_id>")
@teacher_required
def api_update_skill(skill_id: int):
    user = current_user()
    class_id = int(user["class_id"])
    row = get_skill(class_id, skill_id)
    if row is None:
        return jsonify(error="not_found", message="技能不存在"), 404

    payload = _payload()
    if "enabled" in payload:
        db.execute(
            "UPDATE agent_skills SET enabled = ?, updated_at = ? WHERE class_id = ? AND id = ?",
            (1 if _truthy(payload["enabled"]) else 0, db.utcnow(), class_id, skill_id),
        )
        db.get_conn().commit()
    if "text" in payload and str(payload["text"]).strip():
        try:
            parsed = parse_skill_text(str(payload["text"]))
        except SkillError as exc:
            return jsonify(exc.to_dict()), 400
        if parsed["name"] != row["name"] and db.query_one(
            "SELECT id FROM agent_skills WHERE class_id = ? AND name = ?",
            (class_id, parsed["name"]),
        ):
            return jsonify(error="duplicate_name", message="班内已存在同名技能"), 400
        db.execute(
            "UPDATE agent_skills SET name = ?, triggers = ?, material_filter = ?, tools = ?,"
            " body = ?, source_text = ?, enabled = ?, updated_at = ?"
            " WHERE class_id = ? AND id = ?",
            (
                parsed["name"], parsed["triggers"], parsed["material_filter"], parsed["tools"],
                parsed["body"], str(payload["text"]), 1 if parsed["enabled"] else 0,
                db.utcnow(), class_id, skill_id,
            ),
        )
        db.get_conn().commit()

    return jsonify(_serialize(get_skill(class_id, skill_id)))


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "是")


def _invalid_tool_refs(class_id: int, tools_field: str) -> list[str]:
    """`knowledge_search` 总是合法；其余引用必须能在本班已连接的服务器上发现。"""
    refs = [t.strip() for t in str(tools_field).split(",") if t.strip()]
    external = [ref for ref in refs if ref != BUILTIN_TOOL]
    if not external:
        return []
    from . import mcp_host

    available = mcp_host.available_tool_refs(class_id)
    return [ref for ref in external if ref not in available]
