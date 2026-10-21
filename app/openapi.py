"""OpenAPI 3.0 描述 + 可试调文档页（A28）。

不依赖任何 CDN：`/api/docs` 是一个自带渲染与「Try it」的轻量控制台，
读 `/api/openapi.json` 生成表单，因此离线也能按文档试调主要接口。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template

bp = Blueprint("openapi", __name__)

SECURITY = [{"bearerAuth": []}]


def _ok(desc: str, schema: dict | None = None) -> dict:
    return {"description": desc, "content": {"application/json": {"schema": schema or {"type": "object"}}}}


def build_spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "CampusClaw AI 应用平台 API",
            "version": "1.0.0",
            "description": (
                "智能体 / 技能 / 知识库检索 / MCP 工具 / 流式问答。"
                "鉴权：`Authorization: Bearer <JWT>`，令牌由 `POST /api/auth/token` 签发。"
            ),
        },
        "servers": [{"url": "/", "description": "当前实例"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
            }
        },
        "paths": {
            "/health": {
                "get": {
                    "summary": "健康检查",
                    "security": [],
                    "responses": {200: _ok("服务与数据库可用")},
                }
            },
            "/api/auth/token": {
                "post": {
                    "summary": "签发 Bearer JWT（HS256）",
                    "security": [],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["username", "password"],
                                    "properties": {
                                        "username": {"type": "string"},
                                        "password": {"type": "string"},
                                        "ttl": {"type": "integer", "example": 3600},
                                    },
                                },
                                "example": {"username": "teacher_a", "password": "TeacherA@2026"},
                            }
                        },
                    },
                    "responses": {
                        200: _ok("token / expires_in / claims / user"),
                        401: _ok("凭据错误（同形体）"),
                    },
                }
            },
            "/api/auth/revoke": {
                "post": {
                    "summary": "撤销当前令牌",
                    "security": SECURITY,
                    "responses": {200: _ok("已撤销"), 400: _ok("非 Bearer 调用")},
                }
            },
            "/api/auth/me": {
                "get": {"summary": "当前身份（角色与班级来自库）", "security": SECURITY,
                        "responses": {200: _ok("user / auth_mode / claims")}}
            },
            "/api/agents": {
                "get": {
                    "summary": "应用列表（学生只见已启用）",
                    "security": SECURITY,
                    "responses": {200: _ok("agents[]")},
                },
                "post": {
                    "summary": "新建应用（教师）",
                    "security": SECURITY,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string", "maxLength": 64},
                                        "prompt": {"type": "string", "maxLength": 2000},
                                        "enabled": {"type": "boolean"},
                                        "skill_ids": {"type": "array", "items": {"type": "integer"}},
                                        "material_ids": {"type": "array", "items": {"type": "integer"}},
                                    },
                                },
                                "example": {
                                    "name": "数学解题助手",
                                    "prompt": "你是数学解题助手，先判断卡在哪一步。",
                                    "skill_ids": [1],
                                    "material_ids": [2],
                                },
                            }
                        },
                    },
                    "responses": {201: _ok("已创建"), 400: _ok("名称重复或超长"), 403: _ok("学生禁止")},
                },
            },
            "/api/agents/{agent_id}": {
                "get": {"summary": "应用详情", "security": SECURITY,
                        "parameters": [{"name": "agent_id", "in": "path", "required": True,
                                        "schema": {"type": "integer"}}],
                        "responses": {200: _ok("应用"), 404: _ok("不存在或跨班（同形）")}},
                "patch": {"summary": "修改应用（教师）", "security": SECURITY,
                          "parameters": [{"name": "agent_id", "in": "path", "required": True,
                                          "schema": {"type": "integer"}}],
                          "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                          "responses": {200: _ok("已更新"), 400: _ok("校验失败"), 403: _ok("学生禁止")}},
            },
            "/api/agents/{agent_id}/skills": {
                "put": {"summary": "替换技能绑定", "security": SECURITY,
                        "parameters": [{"name": "agent_id", "in": "path", "required": True,
                                        "schema": {"type": "integer"}}],
                        "requestBody": {"content": {"application/json": {
                            "example": {"skill_ids": [1, 2]}}}},
                        "responses": {200: _ok("已绑定"), 404: _ok("技能不存在或跨班")}},
            },
            "/api/agents/{agent_id}/materials": {
                "put": {"summary": "替换材料绑定", "security": SECURITY,
                        "parameters": [{"name": "agent_id", "in": "path", "required": True,
                                        "schema": {"type": "integer"}}],
                        "requestBody": {"content": {"application/json": {
                            "example": {"material_ids": [1, 2]}}}},
                        "responses": {200: _ok("已绑定"), 404: _ok("材料不存在或跨班")}},
            },
            "/api/skills": {
                "get": {"summary": "技能列表（含三项内置）", "security": SECURITY,
                        "responses": {200: _ok("skills[]")}},
                "post": {
                    "summary": "上传技能（教师）",
                    "security": SECURITY,
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                            "example": {
                                "text": "名称: 因式分解引导\n适用条件: 因式分解,提公因式\n"
                                        "资料: 数学\n工具: knowledge_search\n启用: 是\n\n"
                                        "先判断能否提公因式，再考虑公式法。"
                            },
                        }},
                    },
                    "responses": {201: _ok("已创建"), 400: _ok("缺字段 / 重复键 / 超长 / 非法工具")},
                },
            },
            "/api/conversations": {
                "get": {"summary": "我的会话", "security": SECURITY, "responses": {200: _ok("conversations[]")}},
                "post": {"summary": "新建会话（锁定应用）", "security": SECURITY,
                         "requestBody": {"content": {"application/json": {"example": {"agent_id": 1}}}},
                         "responses": {201: _ok("会话"), 404: _ok("应用不存在/停用/跨班")}},
            },
            "/api/conversations/{conversation_id}/ask": {
                "post": {
                    "summary": "非流式提问",
                    "security": SECURITY,
                    "parameters": [{"name": "conversation_id", "in": "path", "required": True,
                                    "schema": {"type": "integer"}}],
                    "requestBody": {
                        "content": {"application/json": {
                            "example": {"q": "倒叙有什么作用？", "include_messages": True},
                        }}
                    },
                    "responses": {200: _ok("answer / status / citations / meta"),
                                  404: _ok("会话不存在"), 503: _ok("对话网关失败")},
                }
            },
            "/api/conversations/{conversation_id}/stream": {
                "post": {
                    "summary": "流式提问（SSE）",
                    "description": "事件顺序 `meta → delta×n → done`；`delta.text` 是新增片段。",
                    "security": SECURITY,
                    "parameters": [{"name": "conversation_id", "in": "path", "required": True,
                                    "schema": {"type": "integer"}}],
                    "requestBody": {"content": {"application/json": {"example": {"q": "一元二次方程怎么解？"}}}},
                    "responses": {200: {"description": "text/event-stream"}, 401: _ok("未登录"),
                                  404: _ok("会话不存在")},
                }
            },
            "/api/ask": {
                "post": {
                    "summary": "一问一答（不落会话，课件契约别名）",
                    "description": "与 `/api/conversations/{id}/ask` 同一条链路的无会话入口。",
                    "security": SECURITY,
                    "requestBody": {"content": {"application/json": {
                        "example": {"q": "倒叙有什么作用？", "mode": "hybrid"}}}},
                    "responses": {200: _ok("answer / status / citations"), 503: _ok("对话网关失败")},
                }
            },
            "/api/ask/stream": {
                "post": {
                    "summary": "流式提问（不落会话）",
                    "description": "事件顺序 `meta → delta×n → done`；首事件前失败为 4xx/5xx。",
                    "security": SECURITY,
                    "requestBody": {"content": {"application/json": {
                        "example": {"q": "倒叙有什么作用？", "agent_id": 1}}}},
                    "responses": {200: {"description": "text/event-stream"},
                                  401: _ok("未登录"), 503: _ok("对话网关失败")},
                }
            },
            "/api/search": {
                "post": {
                    "summary": "三模式检索（keyword / vector / hybrid）",
                    "security": SECURITY,
                    "requestBody": {"content": {"application/json": {
                        "example": {"q": "倒叙", "mode": "hybrid", "limit": 4}}}},
                    "responses": {200: _ok("hits[] / status"), 503: _ok("向量或嵌入不可用")},
                }
            },
            "/api/materials": {
                "get": {"summary": "材料列表（分页）", "security": SECURITY,
                        "parameters": [
                            {"name": "page", "in": "query", "schema": {"type": "integer"}},
                            {"name": "page_size", "in": "query", "schema": {"type": "integer"}},
                        ],
                        "responses": {200: _ok("materials[] / total")}}
            },
            "/api/mcp/servers": {
                "get": {"summary": "MCP 服务器列表（只回 token_configured）", "security": SECURITY,
                        "responses": {200: _ok("servers[]")}},
                "post": {"summary": "登记 MCP 服务器（教师）", "security": SECURITY,
                         "requestBody": {"content": {"application/json": {
                             "example": {"name": "campusclaw", "base_url": "http://127.0.0.1:5000",
                                         "token_env": "MCP_SERVER_TOKEN"}}}},
                         "responses": {201: _ok("已登记，不含令牌"), 400: _ok("重名或参数缺失")}},
            },
            "/api/mcp/servers/{server_id}/probe": {
                "post": {"summary": "连接发现（initialize + tools/list）", "security": SECURITY,
                         "parameters": [{"name": "server_id", "in": "path", "required": True,
                                         "schema": {"type": "integer"}}],
                         "responses": {200: _ok("ok / tools[] / message")}}
            },
            "/api/mcp/tools": {
                "get": {"summary": "本班可用工具（含 tool@server 引用）", "security": SECURITY,
                        "responses": {200: _ok("tools[]")}}
            },
            "/api/mcp/audit": {
                "get": {"summary": "工具调用审计（最近 50 条）", "security": SECURITY,
                        "responses": {200: _ok("audit[]")}}
            },
            "/mcp": {
                "post": {
                    "summary": "MCP JSON-RPC 服务（initialize / tools/list / tools/call）",
                    "description": "使用独立令牌，不是用户 JWT。",
                    "security": SECURITY,
                    "requestBody": {"content": {"application/json": {
                        "example": {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}}}},
                    "responses": {200: _ok("JSON-RPC result"), 401: _ok("令牌无效")},
                }
            },
        },
    }


@bp.get("/api/openapi.json")
def api_openapi():
    return jsonify(build_spec())


@bp.get("/api/docs")
def api_docs():
    return render_template("api_docs.html")
