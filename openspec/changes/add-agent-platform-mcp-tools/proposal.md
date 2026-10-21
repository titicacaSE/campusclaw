# 变更提案：AI 应用平台（智能体 / 技能 / MCP 工具）

## Why（为什么）

第 5 / 6 课把学期项目从「知识库问答」推进到**AI 应用平台**：一个班级里要有多个
**智能体（应用）**，每个应用 = 一份角色提示词 + 一组技能绑定 + 一组材料绑定；
技能由服务端按**确定性词法计分**路由，至多命中一个，命中即把**整段说明**注入 system；
工具通过 **MCP** 接入，宿主只把「本次技能引用的工具」交给模型，形成有上限的调用循环，
并把每次调用写入审计。

现有仓库已经具备：班级隔离、切片与向量检索（keyword/vector/hybrid + RRF）、
可溯源问答（`[n]` ↔ `citations`）、索引生命周期。它**不具备**：智能体配置、
技能上传与路由、MCP 工具接入、流式输出、Bearer 鉴权、OpenAPI 文档与接口测试。

## What Changes（变更内容）

| # | 能力 | 落地物 |
| --- | --- | --- |
| 1 | 内置多个 AI 应用 | `tutor_agents` + 两张绑定表；学生只见 `enabled=1`；会话锁定 agent |
| 2 | 提示词模板 | `tutor_agents.prompt`（≤2000 字），进 system 第 1 段；空则回退默认角色 |
| 3 | 知识库检索 | 检索范围收窄到**该 agent 绑定的材料**；沿用 hybrid + RRF(`k=60`) + 阈值 0.35 |
| 4 | 技能：内置 + 上传 | `agent_skills`；内置「解题引导 / 作业批改 / 错题讲解」；自定义文本文件解析 + 六类拒收 |
| 5 | 外部工具与 MCP | 自建 MCP HTTP 服务（`initialize` / `tools/list` / `tools/call`）+ 宿主侧登记、发现、引用校验、调用循环、三态审计 |

## Capabilities（能力清单）

- `agent-config`：班级限定的智能体配置（名称 / 提示词 / 启用 / 技能绑定 / 材料绑定）。
- `agent-chat`：会话锁定智能体，五段 system + 4×800 资料 + 6×500 历史，`[n]` 可溯源。
- `skill-routing`：技能文件解析、六类拒收、服务端确定性词法路由、命中的整段说明注入。
- `mcp-tools`：自建 MCP 服务、宿主登记与发现、引用校验、有上限的调用循环、三态审计。
- `streaming`：SSE `meta → delta×n → done`，首事件前 4xx、之后恒 200。
- `api-auth`：Bearer JWT（HS256）+ 撤销；Cookie 通道保留给浏览器页面。
- `api-docs`：OpenAPI 3.0 描述 + 可试调文档页 + `apitest/` 一接口一脚本。

### 与既有规约的冲突及处理

`add-traceable-vector-retrieval` 中有一条硬约束：

> MUST 沿用服务端会话 Cookie 认证，MUST NOT 引入客户端持有凭据的登录方案；
> 响应体与 `localStorage` 中不得出现用于后续鉴权的 bearer token。

本次需求的基线约束 **B-1** 与之正面冲突：

> MUST 用 `Authorization: Bearer <JWT>`；HS256；载荷含 `sub/jti/iat/exp`；
> 角色与班级不写进令牌，每次回库读。

处理方式（不改写历史，只做**显式取代**）：

1. 本变更的 `specs/agent-platform/spec.md` 用 `## MODIFIED Requirements` 段落重述
   认证与令牌存储要求；
2. 浏览器页面仍可用会话 Cookie（第 3 课链路不回退），但 **API 一律支持并要求 Bearer**；
3. 新增 `/api/auth/token` 签发 JWT，`/api/login` 保持原语义（只设 Cookie、不回传令牌），
   因此旧断言 `no-token-in-storage` 仍然成立；
4. `ACCEPTANCE.md` 增列「取代关系」小节，说明哪条旧要求被哪条新要求覆盖。

## Impact（影响面）

- 新增表 9 张、新增模块 8 个、新增页面 4 个、新增测试 2 套、新增 `apitest/` 一套；
- 检索函数增加 `material_ids` 收窄参数（`class_id` 仍是第一条谓词，班级闸门不放松）；
- `/api/ask` 语义升级为「按会话选定的 agent 作答」，旧调用（不传会话）走默认 agent；
- 不引入新的第三方依赖：JWT 用标准库 `hmac` + `base64url` 实现。

## Non-goals（明确不做）

- 不实现真实课程网关的流式代理（无凭证）；SSE 的 `delta` 由服务端按增量切片下发，
  并在 `design.md` 标注为「分片下发，非上游透传」，不冒充上游流式；
- 不实现 MCP 的 stdio 传输与 OAuth 授权流程，只做 HTTP + Bearer；
- 不把密钥写进数据库：MCP 令牌只从环境变量读取，登记接口只回「是否已配令牌」布尔值。
