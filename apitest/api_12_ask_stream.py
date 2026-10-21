"""接口测试：流式提问（SSE：meta → delta×n → done）。"""
from _client import endpoint


def _path(_client, ctx):
    return f"/api/conversations/{ctx['conversation_id']}/stream"


def _parse(status, text, _ctx):
    if status == 503:
        return True, "503 upstream_error：本机未配置对话网关（接入后才会 200）"
    if "event: meta" not in text:
        return False, "首事件不是 meta"
    names = [line[len("event: "):] for line in text.splitlines() if line.startswith("event: ")]
    if not names or names[-1] not in ("done", "error"):
        return False, f"末事件异常：{names[-3:]}"
    deltas = [line for line in text.splitlines() if line.startswith("data: ") and '"text"' in line]
    return True, f"events={len(names)} deltas={len(deltas)} 末事件={names[-1]}"


SPEC = endpoint(
    name="提问（SSE 流式）",
    method="POST",
    path=_path,
    payload={"q": "倒叙有什么作用？"},
    expect=(200, 503),
    note="首事件前失败为 4xx/5xx；delta.text 是新增片段而非累计全文",
    parse=_parse,
)
