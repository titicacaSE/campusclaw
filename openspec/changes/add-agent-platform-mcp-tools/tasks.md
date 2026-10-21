# 任务清单：AI 应用平台（智能体 / 技能 / MCP 工具）

> 勾选规则：只有 `verify` 行**真实执行通过**才允许改成 `[x]`，`[x]` 必须能指向证据
> （测试输出 / 命令回显 / 审计表查询）。未通过的一律保持 `[ ]`。

## T1 规约与基线

- [x] 新建本变更四件套并在 `openspec/config.yaml` 登记
  - verify: `python scripts/validate_spec.py add-agent-platform-mcp-tools --check-coverage`
- [x] 在 `ACCEPTANCE.md` 记录对 `add-traceable-vector-retrieval` 认证条款的取代关系
  - verify: `python -c "import pathlib;t=pathlib.Path('ACCEPTANCE.md').read_text(encoding='utf-8');assert '取代' in t and 'Bearer' in t;print('取代关系已记录')"`

## T2 数据模型

- [x] 新增 9 张表（`tutor_agents` / `tutor_agent_skills` / `tutor_agent_materials` / `agent_skills` / `conversations` / `messages` / `mcp_servers` / `mcp_tool_audit` / `token_revocations`）
  - verify: `python -c "import app.db as d, tempfile, os; p=os.path.join(tempfile.mkdtemp(),'a.db'); d.init_database(p, quiet=True); import sqlite3; c=sqlite3.connect(p); names={r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")}; need={'tutor_agents','agent_skills','conversations','messages','mcp_servers','mcp_tool_audit','token_revocations'}; assert need<=names, need-names; print('新表齐备')"`
- [x] 迁移幂等：对已有库重复执行 `migrate()` 不报错、不重复建列
  - verify: `python -c "import app.db as d, tempfile, os; p=os.path.join(tempfile.mkdtemp(),'a.db'); d.init_database(p, quiet=True); a=d.migrate(p); b=d.migrate(p); assert b==[], b; print('迁移幂等')"`

## T3 Bearer JWT

- [x] 实现 `jwt_auth.py`：HS256 签发、验签、`alg` 白名单、`exp`、`jti` 撤销
  - verify: `python -m unittest tests.test_agent_platform.JwtTests -v`
- [x] `POST /api/auth/token` 签发；`POST /api/auth/revoke` 撤销；`GET /api/auth/me` 回身份
  - verify: `python -m unittest tests.test_agent_platform.JwtEndpointTests -v`
- [x] 请求身份解析：Bearer 优先于 Cookie，无效 Bearer 不回落
  - verify: `python -m unittest tests.test_agent_platform.IdentityResolutionTests -v`
- [x] 角色/班级每次回库读：改库后同令牌立即生效
  - verify: `python -m unittest tests.test_agent_platform.RoleRefreshTests -v`

## T4 智能体

- [x] 实现 `agents.py`：列表（学生只见 enabled）、详情、新建、修改、停用、绑定技能/材料
  - verify: `python -m unittest tests.test_agent_platform.AgentCrudTests -v`
- [x] 名称 ≤64 且班内唯一；`prompt` ≤2000；非法值 400
  - verify: `python -m unittest tests.test_agent_platform.AgentValidationTests -v`
- [x] 跨班按编号直取与「不存在」同形 404；绑他班技能/材料 404
  - verify: `python -m unittest tests.test_agent_platform.AgentIsolationTests -v`
- [x] 学生写接口 403，响应含 `required_role` / `your_role`
  - verify: `python -m unittest tests.test_agent_platform.AgentRoleTests -v`

## T5 技能

- [x] 实现 `agent_skills.py`：解析（空行分元数据/正文）、六类拒收、内置三项播种
  - verify: `python -m unittest tests.test_agent_platform.SkillParseTests -v`
- [x] 工具字段校验：仅 `knowledge_search` 或合法 `tool@server`
  - verify: `python -m unittest tests.test_agent_platform.ToolRefFormatTests -v`
- [x] 路由：候选集三层收窄、切词、子串计分、同分取小编号、零分不选
  - verify: `python -m unittest tests.test_agent_platform.RouteTests -v`
- [x] 生效时机：改绑定/停用后下一次提问立即生效
  - verify: `python -m unittest tests.test_agent_platform.RouteRefreshTests -v`
- [x] 解题引导：命中时 system 追加「不写得数 / 不给完整过程」硬性要求，停用后回到直答
  - verify: `python -m unittest tests.test_agent_platform.GuidedSkillTests -v`

## T6 检索收窄

- [x] `retrieve()` 支持 `material_ids`，且 `class_id` 仍是 SQL 第一条谓词
  - verify: `python -m unittest tests.test_agent_platform.ScopedRetrievalTests -v`
- [x] 未绑定材料的词 → 空 hits；绑定材料内正常命中
  - verify: `python -m unittest tests.test_agent_platform.BoundMaterialTests -v`

## T7 五段提示词与问答

- [x] system 五段组装、单条消息、`prompt` 空回退、客户端 `system` 丢弃
  - verify: `python -m unittest tests.test_agent_platform.PromptAssemblyTests -v`
- [x] 历史 6×500、资料 4×800、空正文先剔除再编号、`[n]` 对齐
  - verify: `python -m unittest tests.test_agent_platform.ContextBudgetTests -v`
- [x] 会话锁定 agent，请求体换 `agent_id` 无效
  - verify: `python -m unittest tests.test_agent_platform.ConversationLockTests -v`
- [x] 无命中不调模型，返回固定文案 + 空 citations + 200
  - verify: `python -m unittest tests.test_agent_platform.NoEvidenceTests -v`
- [x] 请求参数 `temperature=0.2` / `max_tokens=500` / 超时 50s；失败 503
  - verify: `python -m unittest tests.test_agent_platform.GatewayParamTests -v`

## T8 流式

- [x] `POST /api/ask/stream` 输出 `meta → delta×n → done`，`delta` 为增量
  - verify: `python -m unittest tests.test_agent_platform.SseTests -v`
- [x] 首事件前 401/404/400 为 HTTP 错误；首事件后失败补 `error` 且 HTTP 200
  - verify: `python -m unittest tests.test_agent_platform.SseFailFastTests -v`

## T9 MCP 服务端

- [x] `POST /mcp` 支持 `initialize` / `tools/list` / `tools/call`，Bearer 校验
  - verify: `python -m unittest tests.test_agent_platform.McpServiceTests -v`
- [x] 工具 `calculator` / `text_stats` 可调用；参数不合 schema → `isError`
  - verify: `python -m unittest tests.test_agent_platform.McpToolsTests -v`

## T10 MCP 宿主

- [x] 服务器登记 / 列表 / 停用；响应只回 `token_configured`
  - verify: `python -m unittest tests.test_agent_platform.McpRegistryTests -v`
- [x] 工具发现与引用校验：清单外工具 400 `unknown_tool`
  - verify: `python -m unittest tests.test_agent_platform.ToolRefValidationTests -v`
- [x] 调用循环：候选集只含引用工具、两轮上限、`role=tool` 追加
  - verify: `python -m unittest tests.test_agent_platform.ToolLoopTests -v`
- [x] 断开 → `denied` + 审计；检索不受影响
  - verify: `python -m unittest tests.test_agent_platform.DeniedTests -v`
- [x] 审计三态
  - verify: `python -m unittest tests.test_agent_platform.AuditTests -v`
- [x] 密钥边界：令牌只在服务端环境变量；响应只回 `token_configured`；全仓搜索无令牌原文
  - verify: `python -m unittest tests.test_agent_platform.SecretBoundaryTests -v`

## T11 文档与接口测试

- [x] `/api/openapi.json` + `/api/docs` 可试调
  - verify: `python -m unittest tests.test_agent_platform.OpenApiTests -v`
- [x] `apitest/` 一接口一脚本 + `run_all.py` 生成 HTML
  - verify: `python apitest/run_all.py --base-url http://127.0.0.1:9 --dry-run`

## T12 页面

- [x] 智能体配置页、技能页、MCP 页、对话页；导航互通
  - verify: `python -m unittest tests.test_agent_platform.PageTests -v`

## T13 验收

- [x] A1–A30 真实 HTTP 验收全部通过
  - verify: `python tests/http_agent_acceptance.py`
- [x] 回归：既有 161 单元 + 23 场景 + 58 HTTP + 236 检索 HTTP 无回归
  - verify: `python tests/test_acceptance.py`
- [x] 规约校验 0 error / 0 warning
  - verify: `python scripts/validate_spec.py add-agent-platform-mcp-tools --check-coverage`

## T14 交付

- [x] README / HOMEWORK / ACCEPTANCE / `.env.example` / `docker-compose.yml` 更新
  - verify: `python scripts/check_compose.py`
- [x] PDF 验证报告（按规约第 6 章结构，附实跑证据）
  - verify: `python -c "import pathlib;t=pathlib.Path('docs/AI应用平台验证报告.pdf').read_bytes();assert t[:5]==b'%PDF-';print('PDF 存在',len(t),'字节')"`
