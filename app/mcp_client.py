"""MCP 客户端：JSON-RPC over HTTP，零第三方依赖（标准库 urllib）。

传输层可注入（`transport=`），便于离线验收：真实调用走 `http_transport`，
测试注入一个进程内实现，**行为等价**（同样的方法名、同样的错误语义）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 8


class McpError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def http_transport(url: str, headers: dict, payload: dict, timeout: int):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return exc.code, {"error": "http_error"}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise McpError("mcp_unreachable", f"MCP 不可达：{type(exc).__name__}") from exc


class McpClient:
    def __init__(self, base_url: str, token: str = "", *, timeout: int = DEFAULT_TIMEOUT,
                 transport=None):
        self.base_url = str(base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self._send = transport or http_transport
        self._seq = 0

    # ------------------------------------------------------------ 底层

    def _post(self, method: str, params: dict):
        if not self.base_url:
            raise McpError("mcp_not_configured", "服务器地址为空")
        self._seq += 1
        payload = {"jsonrpc": "2.0", "id": self._seq, "method": method, "params": params}
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        status, body = self._send(f"{self.base_url}/mcp", headers, payload, self.timeout)

        if status == 401:
            raise McpError("mcp_unauthorized", "MCP 令牌无效")
        if status >= 400:
            raise McpError("mcp_http_error", f"MCP 返回 HTTP {status}")
        if not isinstance(body, dict):
            raise McpError("mcp_bad_response", "MCP 返回非 JSON 对象")
        if body.get("error"):
            error = body["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise McpError("mcp_rpc_error", str(message))
        return body.get("result")

    # ------------------------------------------------------------ 三方法

    def initialize(self) -> dict:
        return self._post(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "campusclaw-host", "version": "1.0.0"},
            },
        )

    def list_tools(self) -> list[dict]:
        result = self._post("tools/list", {})
        tools = (result or {}).get("tools") or []
        return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]

    def call_tool(self, name: str, arguments: dict) -> tuple[bool, str]:
        result = self._post("tools/call", {"name": name, "arguments": arguments}) or {}
        content = result.get("content") or []
        texts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return bool(result.get("isError")), "\n".join(texts)
