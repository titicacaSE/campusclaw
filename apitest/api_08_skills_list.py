"""接口测试：技能列表（含三项内置技能）。"""
import json

from _client import endpoint


def _parse(_status, text, ctx):
    body = json.loads(text)
    skills = body.get("skills") or []
    ctx["skill_id"] = int(skills[0]["id"]) if skills else None
    builtin = [s["name"] for s in skills if s.get("is_builtin")]
    return True, f"total={body.get('total')} builtin={builtin}"


SPEC = endpoint(
    name="技能列表",
    method="GET",
    path="/api/skills",
    expect=(200,),
    note="每班三项内置技能（解题引导 / 作业批改 / 错题讲解），可被教师停用",
    parse=_parse,
)
