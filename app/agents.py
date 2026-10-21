"""智能体（应用）：班级限定的配置单位 —— 一份角色提示词 + 技能绑定 + 材料绑定。

规约依据 `add-agent-platform-mcp-tools` FR-1：
  - `name` ≤64 字且班内唯一；`prompt` ≤2000 字、可为空；
  - `enabled=0` 时学生列表不可见、不可新建会话，但**已存在的会话不受影响**；
  - 跨班按编号直取与「不存在」同形 404；
  - 绑定只接受本班技能与本班材料，否则 404。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import db
from .agent_skills import ensure_builtins, get_skill
from .auth import current_user, teacher_required

bp = Blueprint("agents", __name__)

NAME_MAX = 64
PROMPT_MAX = 2000

# 提示词为空时回退的角色设定（FR-2 S2-2：不因空 prompt 拒绝请求）
DEFAULT_ROLE = (
    "你是本班的学习助手。依据提供的资料回答问题；"
    "资料不足时直接说明，不要编造。回答简短，引用资料时标注编号。"
)


class AgentError(ValueError):
    def __init__(self, code: str, message: str, field: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict:
        payload = {"error": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        return payload


def _payload() -> dict:
    payload: dict = {}
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        payload.update(body)
    if request.form:
        payload.update(request.form.to_dict())
    payload.update(request.args.to_dict())
    return payload


# ------------------------------------------------------------------ 查询


def get_agent(class_id: int, agent_id: int):
    return db.query_one(
        "SELECT * FROM tutor_agents WHERE class_id = ? AND id = ?",
        (int(class_id), int(agent_id)),
    )


def bound_skill_rows(class_id: int, agent_id: int) -> list:
    return db.query(
        "SELECT s.* FROM agent_skills s"
        " JOIN tutor_agent_skills b ON b.skill_id = s.id"
        " WHERE s.class_id = ? AND b.agent_id = ? AND b.class_id = ?"
        " ORDER BY s.id",
        (int(class_id), int(agent_id), int(class_id)),
    )


def bound_material_rows(class_id: int, agent_id: int) -> list:
    return db.query(
        "SELECT m.id, m.title, m.subject, m.ext FROM materials m"
        " JOIN tutor_agent_materials b ON b.material_id = m.id"
        " WHERE m.class_id = ? AND b.agent_id = ? AND b.class_id = ?"
        " ORDER BY m.id",
        (int(class_id), int(agent_id), int(class_id)),
    )


def bound_material_ids(class_id: int, agent_id: int) -> list[int]:
    return [int(row["id"]) for row in bound_material_rows(class_id, agent_id)]


def effective_prompt(row) -> str:
    prompt = str(row["prompt"] or "").strip()
    return prompt or DEFAULT_ROLE


def _serialize(row, *, class_id: int) -> dict:
    skills = bound_skill_rows(int(class_id), int(row["id"]))
    materials = bound_material_rows(int(class_id), int(row["id"]))
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "prompt": row["prompt"],
        "prompt_chars": len(row["prompt"] or ""),
        "effective_prompt": effective_prompt(row),
        "enabled": bool(row["enabled"]),
        "skills": [
            {"id": int(s["id"]), "name": s["name"], "enabled": bool(s["enabled"])}
            for s in skills
        ],
        "materials": [
            {"id": int(m["id"]), "title": m["title"], "subject": m["subject"]}
            for m in materials
        ],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_agents(class_id: int, *, only_enabled: bool) -> list:
    sql = "SELECT * FROM tutor_agents WHERE class_id = ?"
    if only_enabled:
        sql += " AND enabled = 1"
    sql += " ORDER BY id"
    return db.query(sql, (int(class_id),))


# ------------------------------------------------------------------ 校验


def _validate_new(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise AgentError("invalid_request", "缺少名称", "name")
    if len(name) > NAME_MAX:
        raise AgentError("name_too_long", f"名称不得超过 {NAME_MAX} 字", "name")

    prompt = payload.get("prompt")
    prompt = "" if prompt is None else str(prompt)
    if len(prompt) > PROMPT_MAX:
        raise AgentError("prompt_too_long", f"提示词不得超过 {PROMPT_MAX} 字", "prompt")

    enabled = payload.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "否")
    return {"name": name, "prompt": prompt, "enabled": bool(enabled)}


def _bind_skills(class_id: int, agent_id: int, skill_ids) -> None:
    """整体替换绑定。任一编号不属于本班 → 404 且不改动任何绑定。"""
    ids = [int(x) for x in (skill_ids or [])]
    for skill_id in ids:
        if get_skill(class_id, skill_id) is None:
            raise AgentError("not_found", f"技能不存在：{skill_id}", "skill_ids")
    conn = db.get_conn()
    conn.execute(
        "DELETE FROM tutor_agent_skills WHERE class_id = ? AND agent_id = ?",
        (class_id, agent_id),
    )
    for skill_id in dict.fromkeys(ids):
        conn.execute(
            "INSERT INTO tutor_agent_skills (agent_id, skill_id, class_id) VALUES (?, ?, ?)",
            (agent_id, skill_id, class_id),
        )
    conn.commit()


def _bind_materials(class_id: int, agent_id: int, material_ids) -> None:
    ids = [int(x) for x in (material_ids or [])]
    for material_id in ids:
        if db.get_material(material_id, class_id) is None:
            raise AgentError("not_found", f"材料不存在：{material_id}", "material_ids")
    conn = db.get_conn()
    conn.execute(
        "DELETE FROM tutor_agent_materials WHERE class_id = ? AND agent_id = ?",
        (class_id, agent_id),
    )
    for material_id in dict.fromkeys(ids):
        conn.execute(
            "INSERT INTO tutor_agent_materials (agent_id, material_id, class_id)"
            " VALUES (?, ?, ?)",
            (agent_id, material_id, class_id),
        )
    conn.commit()


# ------------------------------------------------------------------ HTTP


@bp.get("/api/agents")
def api_list():
    user = current_user()
    if not user:
        return jsonify(error="unauthorized", message="未登录"), 401
    class_id = int(user["class_id"])
    ensure_builtins(class_id)
    only_enabled = user["role"] != "teacher"
    rows = list_agents(class_id, only_enabled=only_enabled)
    return jsonify(
        agents=[_serialize(row, class_id=class_id) for row in rows],
        total=len(rows),
        role=user["role"],
    )


@bp.get("/api/agents/<int:agent_id>")
def api_detail(agent_id: int):
    user = current_user()
    if not user:
        return jsonify(error="unauthorized", message="未登录"), 401
    class_id = int(user["class_id"])
    row = get_agent(class_id, agent_id)
    if row is None or (user["role"] != "teacher" and not row["enabled"]):
        # 跨班、不存在、学生看已停用 —— 三者同形
        return jsonify(error="not_found", message="应用不存在"), 404
    return jsonify(_serialize(row, class_id=class_id))


@bp.post("/api/agents")
@teacher_required
def api_create():
    user = current_user()
    class_id = int(user["class_id"])
    payload = _payload()
    try:
        fields = _validate_new(payload)
    except AgentError as exc:
        return jsonify(exc.to_dict()), 400

    if db.query_one(
        "SELECT id FROM tutor_agents WHERE class_id = ? AND name = ?",
        (class_id, fields["name"]),
    ):
        return jsonify(
            error="duplicate_name", message=f"班内已存在同名应用：{fields['name']}", field="name"
        ), 400

    now = db.utcnow()
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO tutor_agents (class_id, name, prompt, enabled, created_by, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            class_id, fields["name"], fields["prompt"],
            1 if fields["enabled"] else 0, int(user["user_id"]), now, now,
        ),
    )
    agent_id = int(cur.lastrowid)
    conn.commit()

    for key, binder in (("skill_ids", _bind_skills), ("material_ids", _bind_materials)):
        if key in payload:
            try:
                binder(class_id, agent_id, payload.get(key) or [])
            except AgentError as exc:
                # 绑定失败 → 整体回滚，不留下半成品应用
                conn.execute("DELETE FROM tutor_agents WHERE id = ?", (agent_id,))
                conn.commit()
                return jsonify(exc.to_dict()), 404

    return jsonify(_serialize(get_agent(class_id, agent_id), class_id=class_id)), 201


@bp.patch("/api/agents/<int:agent_id>")
@teacher_required
def api_update(agent_id: int):
    user = current_user()
    class_id = int(user["class_id"])
    row = get_agent(class_id, agent_id)
    if row is None:
        return jsonify(error="not_found", message="应用不存在"), 404

    payload = _payload()
    name = str(payload.get("name", row["name"]) or "").strip()
    prompt = payload.get("prompt", row["prompt"])
    prompt = "" if prompt is None else str(prompt)

    if not name:
        return jsonify(error="invalid_request", message="缺少名称", field="name"), 400
    if len(name) > NAME_MAX:
        return jsonify(error="name_too_long", message=f"名称不得超过 {NAME_MAX} 字"), 400
    if len(prompt) > PROMPT_MAX:
        return jsonify(
            error="prompt_too_long", message=f"提示词不得超过 {PROMPT_MAX} 字", field="prompt"
        ), 400
    if name != row["name"] and db.query_one(
        "SELECT id FROM tutor_agents WHERE class_id = ? AND name = ? AND id <> ?",
        (class_id, name, agent_id),
    ):
        return jsonify(error="duplicate_name", message="班内已存在同名应用"), 400

    enabled = payload.get("enabled", row["enabled"])
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "否")

    db.execute(
        "UPDATE tutor_agents SET name = ?, prompt = ?, enabled = ?, updated_at = ?"
        " WHERE class_id = ? AND id = ?",
        (name, prompt, 1 if enabled else 0, db.utcnow(), class_id, agent_id),
    )
    db.get_conn().commit()

    for key, binder, field in (
        ("skill_ids", _bind_skills, "skill_ids"),
        ("material_ids", _bind_materials, "material_ids"),
    ):
        if key in payload:
            try:
                binder(class_id, agent_id, payload.get(key) or [])
            except AgentError as exc:
                return jsonify({**exc.to_dict(), "field": field}), 404

    return jsonify(_serialize(get_agent(class_id, agent_id), class_id=class_id))


@bp.put("/api/agents/<int:agent_id>/skills")
@teacher_required
def api_bind_skills(agent_id: int):
    user = current_user()
    class_id = int(user["class_id"])
    if get_agent(class_id, agent_id) is None:
        return jsonify(error="not_found", message="应用不存在"), 404
    try:
        _bind_skills(class_id, agent_id, _payload().get("skill_ids") or [])
    except AgentError as exc:
        return jsonify(exc.to_dict()), 404
    return jsonify(_serialize(get_agent(class_id, agent_id), class_id=class_id))


@bp.put("/api/agents/<int:agent_id>/materials")
@teacher_required
def api_bind_materials(agent_id: int):
    user = current_user()
    class_id = int(user["class_id"])
    if get_agent(class_id, agent_id) is None:
        return jsonify(error="not_found", message="应用不存在"), 404
    try:
        _bind_materials(class_id, agent_id, _payload().get("material_ids") or [])
    except AgentError as exc:
        return jsonify(exc.to_dict()), 404
    return jsonify(_serialize(get_agent(class_id, agent_id), class_id=class_id))
