"""接口测试：非流式提问（先检索、后生成）。"""
import json

from _client import endpoint


def _path(_client, ctx):
    return f"/api/conversations/{ctx['conversation_id']}/ask"


def _parse(status, text, _ctx):
    if status == 503:
        return True, "503 upstream_error：本机未配置对话网关（接入后才会 200）"
    body = json.loads(text)
    if body.get("status") not in ("ok", "no_evidence"):
        return False, f"status={body.get('status')}"
    citations = body.get("citations") or []
    if body["status"] == "no_evidence":
        if citations or body.get("answer") != "资料中未找到相关内容":
            return False, "无命中时必须返回固定文案与空 citations"
        return True, "no_evidence（固定文案 + 空 citations）"
    meta = body.get("meta") or {}
    return True, (
        f"status=ok cites={len(citations)} skill={meta.get('skill')} "
        f"model_calls={meta.get('model_calls')}"
    )


SPEC = endpoint(
    name="提问（非流式）",
    method="POST",
    path=_path,
    payload={"q": "倒叙有什么作用？"},
    expect=(200, 503),
    note="无命中不调对话网关；[n] 与 citations 下标一一对应",
    parse=_parse,
)
