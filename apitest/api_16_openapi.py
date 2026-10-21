"""接口测试：OpenAPI 描述与可试调文档页。"""
import json

from _client import endpoint


def _parse(_status, text, _ctx):
    body = json.loads(text)
    paths = body.get("paths") or {}
    required = ["/api/auth/token", "/api/agents", "/api/ask/stream", "/mcp"]
    missing = [p for p in required if p not in paths]
    if missing:
        return False, f"缺少路径：{missing}"
    schemes = (body.get("components") or {}).get("securitySchemes") or {}
    if "bearerAuth" not in schemes:
        return False, "未声明 bearerAuth"
    return True, f"openapi={body.get('openapi')} paths={len(paths)} version={body['info']['version']}"


SPEC = endpoint(
    name="OpenAPI 描述",
    method="GET",
    path="/api/openapi.json",
    auth=False,
    expect=(200,),
    note="公开可读；/api/docs 读同一份描述渲染可试调页面",
    parse=_parse,
)
