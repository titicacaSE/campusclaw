# auth-upload 能力规约（delta）

> 本文件是 `add-auth-rbac-class-knowledge` 的增量规约（delta），来源为需求规约文档 §4 功能需求与 §5 非功能需求。
> 每条需求使用 Requirement + Scenario 结构，Scenario 以 WHEN / THEN / AND 书写，**唯一标准是能否直接判定通过与否**。

## ADDED Requirements

### Requirement: 用户登录与会话建立

系统 MUST 提供教师、学生两类角色的账号密码登录；登录成功后 MUST 建立服务端会话；未登录用户访问受保护页面 MUST 被引导到登录页，且 MUST NOT 泄露任何业务数据。

#### Scenario: 教师与学生登录成功
- **WHEN** 教师 A 或学生 A1 在 `/login` 提交正确账号与口令
- **THEN** 登录成功并建立服务端会话，响应下发会话 Cookie
- **AND** 会话中记录用户标识、角色与所属班级（`user_id`、`role`、`class_id`）

#### Scenario: 错误密码登录失败
- **WHEN** 用户名存在但提交的口令错误
- **THEN** 登录失败且不建立会话（不下发会话 Cookie）
- **AND** 响应不得返回密码哈希等敏感信息

#### Scenario: 未登录访问受保护资源
- **WHEN** 未携带有效会话访问受保护资源
- **THEN** 页面请求返回 302 重定向至 `/login`
- **AND** API 请求返回 HTTP 401，且响应不得含任何班级材料标题、正文或文件路径

### Requirement: 角色权限

系统 MUST 按会话中的角色授权：教师可上传与管理本班材料；学生对本班材料只读；学生调用上传或管理接口 MUST 被拒绝。

#### Scenario: 学生上传被拒绝
- **WHEN** 学生 A1 向 `POST /api/materials/upload` 提交文件
- **THEN** 返回 HTTP 403
- **AND** `materials` 与 `knowledge_entries` 行数不变，上传目录无因该请求产生的新文件

#### Scenario: 教师上传被允许
- **WHEN** 教师 A 提交支持的文件（`.txt` / `.md`）
- **THEN** 执行入库流程并返回 HTTP 201，响应含新材料标识 `material_id`
- **AND** 材料与知识库条目均归属教师 A 所属班级

#### Scenario: 学生仅只读本班列表
- **WHEN** 学生 A1 打开 `/materials` 或 `GET /api/materials`
- **THEN** 可见本班材料列表
- **AND** 页面与 API 均不提供学生可用的上传或删除入口，且服务端仍拒绝学生的写接口

### Requirement: 班级隔离

班级是数据边界。所有材料、知识库及相关业务查询 MUST 在服务端按会话中的班级标识过滤；A 班用户 MUST NOT 读取、修改或删除 B 班材料；前端隐藏按钮 MUST NOT 作为满足本要求的唯一手段。

#### Scenario: 跨班按 ID 访问材料被拒绝
- **WHEN** A 班用户通过 URL 路径、路径参数或请求体指定 B 班材料 ID 访问材料或下载文件
- **THEN** 返回 403 或 404（本实现固定为 404，见 README 与 design.md）
- **AND** 响应不得含 B 班标题、正文片段、文件路径或存储键

#### Scenario: 跨班枚举列表不得泄露 B 班
- **WHEN** 学生 A1 请求材料列表（含分页/搜索为空或任意合法值）
- **THEN** 每条记录均归属班级 A
- **AND** 列表不得出现含「B 班」的可区分标题

#### Scenario: 材料列表仅含本班记录
- **WHEN** 教师 A 打开 `/materials` 或 `GET /api/materials`
- **THEN** 每条记录归属班级 A
- **AND** 数据来自带班级过滤的数据库查询（`WHERE class_id = :session_class_id`）

#### Scenario: 上传时忽略客户端班级字段
- **WHEN** 教师在请求体中额外传入 `class_id`、`class` 等字段（例如试图指定 B 班）
- **THEN** 服务端忽略这些字段，材料仍写入会话所属班级
- **AND** 系统不存在「代传 B 班」的 API

### Requirement: 材料上传与知识库入库

教师上传教学材料后，系统 MUST 将文件落盘、解析文本并写入知识库记录；材料与知识库条目 MUST 关联上传者所属班级；本班材料列表 MUST 从数据库查询展示；上传成功后刷新列表 MUST 可见新记录。

#### Scenario: 上传后知识库与列表可查
- **WHEN** 教师 A 上传合法 `.md` / `.txt` 且解析成功
- **THEN** `materials` 新增 1 行、`knowledge_entries` 新增关联班级 A 的记录（同事务）
- **AND** 刷新本班列表可见该标题

#### Scenario: 上传后学生本班可见只读
- **WHEN** 学生 A1 刷新 `/materials`
- **THEN** 可见教师 A 刚上传的材料
- **AND** 仍为只读，学生无法通过 API 修改或删除该材料

#### Scenario: 上传失败时不产生脏数据
- **WHEN** 上传不支持格式（如 `.exe`）、空文件或解析失败（非 UTF-8 损坏文件）
- **THEN** 返回 400
- **AND** `materials` 与 `knowledge_entries` 无残留记录，且不保留孤立未关联文件

### Requirement: 预置核心数据

系统 MUST 建立班级、用户、讲义、作业、助手、技能六类核心数据结构，并预置可验收的样本数据。

#### Scenario: 种子数据满足双班与用户
- **WHEN** 执行初始化脚本或 Compose 首次启动
- **THEN** 存在班级 A、班级 B，以及教师 A（A 班）、学生 A1（A 班）、学生 B1（B 班）
- **AND** 各用户具备可登录用户名与已哈希密码

#### Scenario: 两班材料标题可区分
- **WHEN** 查看预置材料
- **THEN** 至少一条材料标题含「A 班」且归属班级 A，至少一条含「B 班」且归属班级 B
- **AND** 六类核心表（`classes`、`users`、`handouts`、`assignments`、`assistants`、`skills`）均已创建

### Requirement: 密码与密钥安全

用户表 MUST 使用 `password_hash` 字段并禁止明文密码列；口令 MUST 使用单向哈希（bcrypt 或等价）；会话签名密钥 MUST 仅从环境变量读取，未提供时启动 MUST 失败。

#### Scenario: 库中无明文密码
- **WHEN** 查询 `users.password_hash`
- **THEN** 其值不等于任何预置账号的明文口令
- **AND** 可识别为哈希格式（bcrypt `$2` 前缀）

#### Scenario: 登录校验使用哈希比较
- **WHEN** 使用正确口令与错误口令分别登录
- **THEN** 正确口令建立会话
- **AND** 错误口令验证失败且不建立会话

#### Scenario: 缺少会话密钥时的启动行为
- **WHEN** 未提供环境变量 `SECRET_KEY` 启动应用
- **THEN** 进程启动失败并给出明确原因
- **AND** 不落入任何不安全默认密钥

### Requirement: 部署与可观测

标准启动方式 MUST 为 `docker compose up --build`；应用 MUST 提供免登录的 `GET /health` 返回 200 JSON；数据卷挂载后重建容器 MUST 仍可查询既有数据。

#### Scenario: Compose 启动后可访问
- **WHEN** 按 README 执行 `docker compose up --build` 直至 healthcheck 通过
- **THEN** 浏览器可访问登录页
- **AND** `GET /health` 返回 200 且表明服务可用

#### Scenario: health 不依赖登录态
- **WHEN** 不携带任何 Cookie 访问 `/health`
- **THEN** 仍返回成功状态码与 JSON
- **AND** 不发生到登录页的重定向

#### Scenario: 重建容器后数据仍在
- **WHEN** 执行 `docker compose down` 后再次 `up`（未删除 volume）
- **THEN** 预置班级与用户仍可登录
- **AND** 已上传材料与知识库记录仍可查询

### Requirement: 可验证性与可追溯

每条需求 MUST 可判定，负路径（错误密码、未登录、跨班、学生上传）与正路径同等重要；规约 MUST 写入 Git，换用新会话仅读取 `openspec/` 即可复现全部要求。

#### Scenario: 负路径与正路径同等覆盖
- **WHEN** 执行验收测试
- **THEN** 覆盖错误密码、未登录（页面与 API）、跨班按 ID、跨班列表、学生上传等负路径
- **AND** 同时覆盖登录、上传入库、列表可见等正路径

#### Scenario: 新会话凭规约复现
- **WHEN** 在新会话中仅阅读 `openspec/`、README 与项目规则文件
- **THEN** 可引用 spec 原文回答三条硬性要求（登录引导、班级隔离、教师专属上传）
- **AND** 验收结论以 spec 原文为依据，而非聊天记录
