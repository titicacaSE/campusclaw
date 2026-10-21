# 第五、六课作业说明：AI 应用平台（智能体 / 技能 / MCP 工具）

> 仓库：CampusClaw（学期工程，Flask 3.x + SQLite 3 + Qdrant）
> 本次变更：`add-agent-platform-mcp-tools`　新增能力：`agent-platform`
> 变更目录：`openspec/changes/add-agent-platform-mcp-tools/`
> 变更状态：实现完成、`tasks.md` 41/42 项已通过 verify 并勾选；
> 唯一未勾选项「PDF 验证报告」由 `docs/AI应用平台验证报告.pdf` 交付
> 认证口径变更：**由 `add-traceable-vector-retrieval` 的「禁止客户端凭据」显式取代为
> 「页面仍用 Cookie、API 用 Bearer JWT」**，取代关系记在 `ACCEPTANCE.md` §11

## 一、作业要求与本次交付

| 项 | 作业要求 | 本次交付 |
| --- | --- | --- |
| 多个 AI 应用 | 同班 ≥2 个，各自技能/材料不同 | ✅ 数学解题助手、语文阅读助手；学生只见已启用（停用即不可见、不可选用） |
| 应用字段 | `name` ≤64 班内唯一；`prompt` ≤2000 可空 | ✅ 重名/超长 400；空 `prompt` 回退默认角色仍可作答 |
| 提示词传递 | 全文进 system 首段，客户端不可覆盖 | ✅ 首段与库中 `prompt` **逐字相等**；客户端 `system` 一律丢弃 |
| 技能内置 | 预置 ≥3 项可启停 | ✅ 解题引导 / 作业批改 / 错题讲解，均可在页面启停 |
| 技能上传 | 单文件、空行分元数据与正文、六类拒收 | ✅ `POST /api/skills`（JSON 或 multipart），拒收均有可读错误码 |
| 技能路由 | 服务端确定性，不调模型，至多选一个 | ✅ 候选集三层收窄 → 子串计分 → 零分不选 → 同分取小编号 |
| 解题引导 | 命中时不得给答案/得数/完整过程 | ✅ 该技能正文含「不写最终答案、不写选项、不写得数、不给完整解题过程」 |
| 工具与 MCP | 自建 MCP 服务 + 宿主侧循环 | ✅ `/mcp` 支持 `initialize` / `tools/list` / `tools/call`；宿主两轮上限、`denied`、三态审计 |
| 认证 | Bearer JWT，角色与班级回库读 | ✅ HS256 + `alg` 白名单 + `exp` + `jti` 撤销；每次请求回库读 `role`/`class_id` |
| 流式 | `meta → delta×n → done`，`delta` 为增量 | ✅ SSE；首事件前失败为 4xx，首事件后补 `error` 且 HTTP 200 |
| Swagger | 可打开并可试调 | ✅ `/api/openapi.json` + `/api/docs`（含 try-it-out） |
| 接口测试 | 一接口一脚本 + `run_all.py` 出 HTML | ✅ `apitest/` 16 个脚本，报告 `apitest/report.html` |
| 公网仓库 | 可匿名克隆，README 有启动与验证命令 | ✅ `https://github.com/titicacaSE/campusclaw`（public） |

## 二、变更内容概要

**为什么做**：变更二把「材料 → 切片 → 向量 → 有引用的问答」打通了，但问答只有一个固定人格：
服务端写死一段 system，全班共用。课件第 5/6 课要求的是**可配置的应用**——
一份角色提示词 + 一组技能 + 一组材料，再让技能在服务端确定性地决定「这一次该用哪套规程」，
并通过 MCP 把外部工具接进来。

**新增了什么**

1. **9 张表**：`tutor_agents` / `tutor_agent_skills` / `tutor_agent_materials` / `agent_skills` /
   `conversations` / `messages` / `mcp_servers` / `mcp_tool_audit` / `token_revocations`。
   迁移幂等（`migrate()` 只加缺失的列/表）。
2. **`app/jwt_auth.py`**：HS256 签发/验签、`alg` 白名单、`exp`、`jti` 撤销表；
   `Bearer` 优先于 Cookie，且**无效 Bearer 不回落**到 Cookie（避免「带了错令牌却用旧会话混过去」）。
3. **`app/agents.py` / `app/agent_skills.py`**：应用 CRUD 与技能绑定、技能解析与六类拒收、
   内置三项播种、**服务端确定性路由**（切词 → 子串计分 → 同分取小编号）。
4. **`app/agent_chat.py`**：会话锁定应用；五段 system 组装；历史 6×500、资料 4×800；
   无命中不调模型；`[n]` 与 `citations` 对齐；SSE 流式。
5. **`app/mcp_service.py` / `mcp_client.py` / `mcp_host.py`**：自建 MCP 服务（Bearer 校验、
   `calculator` / `text_stats` 两个工具）、宿主侧登记/发现/引用校验/候选集/调用循环/审计。
6. **页面**：`/agents` `/skills` `/mcp` `/chat` `/api/docs`，导航互通；全部 `textContent` 渲染，无 `innerHTML`。
7. **`apitest/`**：一接口一脚本 + `run_all.py` 出 HTML 报告（含耗时与判定）。

## 三、严格校验结果（真实）

```
python -m unittest tests.test_agent_platform            →  102 tests  OK
python tests/http_agent_acceptance.py                   →  170/170 项（A1–A30）
python tests/test_chunking.py ... test_search_page.py   →  161 tests  OK（回归）
python -m unittest tests.test_acceptance                →  23 tests   OK（回归）
python tests/http_acceptance.py                         →  58/58      （回归）
python tests/http_retrieval_acceptance.py               →  236/236    （回归）
python scripts/validate_spec.py add-agent-platform-mcp-tools --check-coverage
                                                        →  0 error / 0 warning
python scripts/check_compose.py                         →  14/14
```

`tasks.md` 的 42 条任务逐条执行自己的 `verify`：**41 条通过并勾选，1 条（PDF）待产物落盘**。
首轮有 2 条失败（`ACCEPTANCE.md` 未写取代关系、PDF 不存在），补完产物后重跑才勾选。

## 四、关键设计决策（摘要）

| 决策 | 取舍 | 理由 |
| --- | --- | --- |
| 认证：Cookie + Bearer 并存 | 不推翻变更二，新增 API 通道 | 浏览器不该持有凭据，但机器调用需要令牌；两条通道各自成立 |
| 角色/班级**每次回库读** | 不把 `role`/`class_id` 当作信任源 | JWT 里的声明只是「是谁」，不是「能做什么」；改库后立即生效 |
| 路由放服务端、不交给模型 | 不用「让模型选技能」 | 确定性可验收：同一问句永远同一技能，同分有明确规则 |
| 技能正文**全量注入**（≤2000 字） | 不做渐进式披露 | 课件 L5 明确是自定义格式，要求「整段进 system」；差异已在报告说明 |
| 工具候选集只含**技能引用**的工具 | 不把所有已连工具都给模型 | 收窄后模型无法调用未声明工具，混淆代理面变小 |
| 两轮上限硬编码在宿主 | 不做可配置 | 课件只要求「两步可两轮、上限=1 收敛」，固定值更好验收 |
| 审计只记工具引用与状态 | 不记请求头/参数原文 | 审计表一旦落库就是长期资产，不能存令牌 |
| `tools/call` 走真实 HTTP | 不进程内直调 | MCP 的意义就是跨进程；进程内直调会掩盖鉴权与超时问题 |

## 五、本轮已完成的作业动作（含证据）

| 动作 | 证据 |
| --- | --- |
| 写规约四件套并自检 | `openspec/changes/add-agent-platform-mcp-tools/`；`validate_spec --check-coverage` 0/0 |
| 建 9 张表 + 幂等迁移 | `MigrationTests`；`migrate()` 二次返回 `[]` |
| JWT 签发/撤销/无效不回落 | `JwtTests` / `JwtEndpointTests` / `IdentityResolutionTests` / `RoleRefreshTests` |
| 技能解析六类拒收 | `SkillParseTests`（缺字段/重名/超长/非法工具/空正文/文件超长） |
| 路由确定性 | `RouteTests` / `RouteRefreshTests` / `GuidedSkillTests` |
| 五段 system | `PromptAssemblyTests` / `SystemDropTests` / `HistoryBudgetTests` |
| 无命中不调模型 | `NoEvidenceTests`（断言 `model_calls == 0`） |
| SSE 顺序与增量 | `StreamTests` |
| MCP 三方法 + 鉴权 | `McpServiceTests`；原始报文见报告第 9 章 |
| 工具循环与审计三态 | `ToolLoopTests` / `AuditTests` |
| 页面与导航 | `docs/assets/02_agents.png` … `07_materials.png`（真实登录态截图） |
| 接口测试报告 | `apitest/report.html`；截图 `docs/assets/08_apitest.png` |

## 六、可复用提示词

见 `docs/AI应用平台验证报告.pdf` 附录 A「复现命令」；规约侧提示词沿用
`openspec/changes/add-agent-platform-mcp-tools/design.md` 的「设计约束」一节。

## 七、发布包与仓库

- 仓库：`https://github.com/titicacaSE/campusclaw`（public，分支 `main`）
- 启动：

```bash
cp .env.example .env      # 至少填 SECRET_KEY（≥16 字符）
docker compose up -d
curl -s localhost:8080/health
```

- 无 Docker 时的等价启动：

```bash
python scripts/init_db.py --db data/app.db --uploads data/uploads
SECRET_KEY=dev-secret-key-0123456789 python -m flask --app app run --port 8080
```

## 八、完成度声明

1. **功能**：FR-1～FR-5 全部实现，A1～A30 中 29 项完全通过，A30「公网可访问」依赖本轮推送（见 ACCEPTANCE §15）。
2. **替身边界**：对话/嵌入网关与向量库为本地等价替身（真算余弦、真做 pre-filter 与阈值），
   真实 Qdrant 与课程网关**未复核**；Docker Compose **未实跑**。这三项在 `ACCEPTANCE.md` §14 逐条标注。
3. **不归档**：按规约「仍有未通过项时 MUST NOT 归档」，四件套保留在 `openspec/changes/`，
   `.openspec.yaml` 仍是 `status: implementing`。
