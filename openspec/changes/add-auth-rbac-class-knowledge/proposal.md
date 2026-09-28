# Proposal: add-auth-rbac-class-knowledge

> 来源规约：《互联网软件开发技术与实践》第 2 课《SDD 与 CampusClaw 规约》需求规约文档 v1.0
> 新增能力：`auth-upload`　变更名：`add-auth-rbac-class-knowledge`
> 位置：`openspec/changes/add-auth-rbac-class-knowledge/`

## Why

CampusClaw 是面向中小学的教研智能体，覆盖学情分析、备课、命题、课堂与教研协同，教研材料与知识库是其能力底座。当前痛点：

- 教学材料分散在群文件与网盘中，教师难以检索与复用；
- 班级之间缺少数据边界，材料归属不清；
- 尚不具备身份与角色体系，无法区分「谁能看、谁能传」。

本变更的目标：先建立**身份、角色与班级隔离**，让教师登录后上传并管理本班材料，学生仅可查看本班内容，为后续检索与智能问答打基础。

## What Changes

1. **用户登录**：教师、学生两类角色；账号密码登录；未登录访问受保护页面时引导至登录页。
2. **角色权限**：教师可上传与管理本班材料；学生对本班材料只读；学生调用上传接口 MUST 被拒绝（HTTP 403）。
3. **班级隔离**：班级为数据边界；所有业务查询在服务端按会话中的班级过滤；A 班用户不得读取 B 班材料（含直接访问与列表越权）。
4. **材料上传与知识库入库**：教师上传后解析并写入知识库，材料关联本班；本班材料列表查库展示，上传后可见新记录。
5. **预置样本数据**：班级 A/B、教师 A、学生 A1/B1，以及两班可区分的材料标题；建立班级、用户、讲义、作业、助手、技能六类核心数据结构并写入种子数据。
6. **安全与运行**：密码 MUST 哈希存储，禁止明文；会话/签名密钥仅通过服务端环境变量注入；标准启动方式为 Docker Compose；应用提供 `GET /health`。
7. **文档**：README 三行说明（价值、场景、不做）与 Docker 启动方式；新增 `docker-compose.yml`、`.env.example`。

## Non-goals

| 类别 | 不做的内容 |
| --- | --- |
| 检索与问答 | RAG、全文检索、智能问答、基于知识库的对话 |
| 对话助手 | 聊天界面、Agent 编排、技能包运行时 |
| 作业流程 | 作业提交、批改、成绩、审计与错题本 |
| 身份与企业集成 | 多校/多租户、OAuth/OIDC、SSO、短信/邮箱验证码登录、注册与找回密码 |
| 生产运维 | K8s、多副本高可用、完整 CI/CD 流水线（标准启动为 Docker Compose 本地/演示环境） |
| 前端替代服务端安全 | 不以隐藏按钮、禁用表单作为权限或班级隔离的唯一手段 |

表结构可预留字段，但上述内容本轮不验收——这是防止范围膨胀的第一道闸。

## Capabilities

- `auth-upload`：登录、会话、角色权限、班级隔离、材料上传与知识库入库。
- 依赖的既有能力：无（本变更在空仓库上新建）。

## Impact

- 新建 Web 应用与 API：`/login`、`/materials`、`/api/materials`、`/api/materials/<id>`、`/api/materials/upload`、`/health`。
- 新建 SQLite 持久层：`classes`、`users`、`materials`、`knowledge_entries`，以及 `handouts`、`assignments`、`assistants`、`skills` 六类域表中的其余四类，附种子数据脚本 `scripts/init_db.py`。
- 项目根新增 `Dockerfile`、`docker-compose.yml`、`entrypoint.sh`、`.env.example`、`requirements.txt`。
- 验收方式见 `openspec/changes/add-auth-rbac-class-knowledge/tasks.md` 的 `verify:` 命令与 `ACCEPTANCE.md`。
