"""自建 MCP 服务（第 6 课 T6）：JSON-RPC 2.0 over HTTP。

只实现课堂要求的三个方法：

  - `initialize`  → 协议版本与能力声明
  - `tools/list`  → 工具声明（`name / description / inputSchema`）
  - `tools/call`  → 执行工具，返回 `result.content[]` 与 `isError`

令牌由 `MCP_SERVER_TOKEN` 注入，**只存在于服务端**；不匹配即 401，且响应不回显任何片段。
"""
from __future__ import annotations

import ast
import hmac
import operator

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("mcp_service", __name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "campusclaw-mcp"
SERVER_VERSION = "1.0.0"


# ------------------------------------------------------------------ 工具实现

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node):
    """只允许数字与四则运算，杜绝 `__import__` 之类的注入。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("只允许数字常量")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError("不支持的运算符")
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("指数过大")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError("不支持的一元运算符")
        return op(_safe_eval(node.operand))
    raise ValueError("表达式含不允许的语法")


def tool_calculator(args: dict) -> str:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        raise ValueError("缺少 expression")
    if len(expression) > 200:
        raise ValueError("expression 过长")
    value = _safe_eval(ast.parse(expression, mode="eval"))
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{expression} = {value}"


def tool_text_stats(args: dict) -> str:
    text = str(args.get("text") or "")
    if not text:
        raise ValueError("缺少 text")
    sentences = sum(text.count(ch) for ch in "。！？!?")
    chars = len(text.replace("\n", "").replace(" ", ""))
    return f"字符数 {chars}，句数 {sentences}，行数 {len(text.splitlines())}"


TOOLS = (
    {
        "name": "calculator",
        "description": "计算一个只含数字与四则运算的表达式，例如 12*(3+4)。",
        "handler": tool_calculator,
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "四则运算表达式"},
            },
            "required": ["expression"],
        },
    },
    {
        "name": "text_stats",
        "description": "统计一段文本的字符数、句数与行数。",
        "handler": tool_text_stats,
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "待统计文本"}},
            "required": ["text"],
        },
    },
)

_BY_NAME = {tool["name"]: tool for tool in TOOLS}


# ------------------------------------------------------------------ 协议


def _expected_token() -> str:
    value = current_app.config.get("MCP_SERVER_TOKEN")
    return str(value or "")


def _authorized() -> bool:
    expected = _expected_token()
    if not expected:
        return False
    raw = request.headers.get("Authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), expected)


def _rpc_result(rpc_id, result):
    return jsonify(jsonrpc="2.0", id=rpc_id, result=result)


def _rpc_error(rpc_id, code: int, message: str, *, status: int = 200):
    payload = jsonify(jsonrpc="2.0", id=rpc_id, error={"code": code, "message": message})
    payload.status_code = status
    return payload


def _validate_args(tool: dict, args: dict) -> list[str]:
    schema = tool["inputSchema"]
    problems: list[str] = []
    for name in schema.get("required", []):
        if name not in args:
            problems.append(f"缺少必填参数 {name}")
    for name, decl in (schema.get("properties") or {}).items():
        if name in args and decl.get("type") == "string" and not isinstance(args[name], str):
            problems.append(f"参数 {name} 必须是字符串")
    return problems


@bp.post("/mcp")
def rpc():
    if not _authorized():
        return jsonify(error="unauthorized", message="MCP 令牌无效"), 401

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _rpc_error(None, -32700, "请求体必须是 JSON 对象")

    rpc_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}

    if method == "initialize":
        return _rpc_result(
            rpc_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "ping":
        return _rpc_result(rpc_id, {})

    if method == "tools/list":
        return _rpc_result(
            rpc_id,
            {
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool["description"],
                        "inputSchema": tool["inputSchema"],
                    }
                    for tool in TOOLS
                ]
            },
        )

    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        tool = _BY_NAME.get(name)
        if tool is None:
            return _rpc_error(rpc_id, -32602, f"未知工具：{name}")
        problems = _validate_args(tool, args)
        if problems:
            return _rpc_result(
                rpc_id,
                {
                    "content": [{"type": "text", "text": "；".join(problems)}],
                    "isError": True,
                },
            )
        try:
            text = tool["handler"](args)
        except Exception as exc:  # noqa: BLE001
            return _rpc_result(
                rpc_id,
                {"content": [{"type": "text", "text": f"工具执行失败：{exc}"}], "isError": True},
            )
        return _rpc_result(
            rpc_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )

    return _rpc_error(rpc_id, -32601, f"未实现的方法：{method}")


@bp.get("/mcp/health")
def health():
    """自检端点：不校验令牌，只回是否已配置（不回显令牌）。"""
    return jsonify(
        ok=True,
        server=SERVER_NAME,
        protocolVersion=PROTOCOL_VERSION,
        token_configured=bool(_expected_token()),
        tools=[tool["name"] for tool in TOOLS],
    )
