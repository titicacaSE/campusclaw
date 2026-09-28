# CampusClaw

面向中小学的教研智能体平台。仓库当前包含两个 OpenSpec 变更：

| 变更 | 能力 | 状态 |
| --- | --- | --- |
| `add-auth-rbac-class-knowledge` | 登录、角色权限、班级隔离、材料上传与知识库入库 | **已实现并通过验收** |
| `add-class-scoped-knowledge-search` | 班级限定知识库检索 + 内容溯源 | **规约已完成，功能未实现** |

> **「规约完成」≠「功能已实现」。** 第二个变更目前只有规约（四件套），`tasks.md` 全部保持 `[ ]`，仓库中尚无对应代码。

## 价值

把教研材料从「群文件 + 网盘」搬进有归属、有权限的班级知识库，让教师上传一次、全班可查，并能按关键词定位到原文的具体位置。

## 场景

教师登录后把讲义（`.txt` / `.md`）上传到本班知识库，服务端解析文本并入库；学生登录后只读本班材料列表；跨班访问一律被服务端拒绝。

规划中的检索场景：教师或学生输入关键词，只在本班材料中检索，每条结果给出**原文片段、材料与片段标识、码点偏移、源行号、来源链接与下载链接**，可回溯到原文。

## 不做

不做 RAG 问答生成 / 对话助手 / 作业流程 / 多校多租户 / SSO / K8s / PDF-OCR；不以隐藏按钮代替服务端权限校验。

检索能力**只做可复现的关键词匹配与溯源，不做分词、同义词、语义向量检索，也不生成任何答案或摘要**。

---

## 快速开始

### 方式一：Docker Compose（标准启动方式，规约 NFR-02）

```bash
cp .env.example .env                                  # 填写 SECRET_KEY（必填，缺失启动即失败）
python -c "import secrets;print(secrets.token_urlsafe(48))"   # 生成一个随机密钥填进 .env
docker compose up --build                             # 首次启动自动建表 + 写入种子数据
curl -sf http://127.0.0.1:8080/health && echo           # {"status":"ok",...}
```

打开 <http://127.0.0.1:8080/login> 登录。`docker compose down` 后再 `up`（未删 volume）数据仍在。

### 方式二：本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")   # Windows PowerShell: $env:SECRET_KEY='...'
python scripts/init_db.py                          # 建表 + 种子（幂等）
python wsgi.py                                     # http://127.0.0.1:8080
```

### 演示账号（仅课程演示环境，生产必须替换）

| 账号 | 角色 | 所属班级 | 口令 |
| --- | --- | --- | --- |
| `teacher_a` | 教师 | 班级 A | `TeacherA@2026` |
| `teacher_b` | 教师 | 班级 B | `TeacherB@2026` |
| `student_a1` | 学生 | 班级 A | `StudentA1@2026` |
| `student_b1` | 学生 | 班级 B | `StudentB1@2026` |

> 规约只要求教师 A、学生 A1、学生 B1 三名用户；本实现额外预置了「教师 B（班级 B）」。
> 原因是 FR-04 要求材料必须关联「上传者所属班级」——若 B 班样本材料署名为 A 班教师，种子数据自身就会破坏该不变量；
> 同时教师 B 可用于验证「B 班教师既不能上传到 A 班，也不能查看 A 班材料」。

---

## 状态码与隔离约定（固定取舍）

- **未登录**：页面请求 `302 → /login`；`/api/**` 请求 `401 {"error":"unauthorized"}`。
- **学生调用写接口**：`403 {"error":"forbidden"}`（消息含「无权限」）。
- **按 ID 访问跨班或不存在材料**：**统一返回 `404`**（规约 FR-03 S1 允许 403 或 404，本实现择一固定为 404）。
  理由是避免通过状态码差异枚举他人材料是否存在；响应体不含 B 班标题、正文、文件路径或存储键。
- **上传失败**（类型不支持 / 空文件 / 超限 / 解析失败）：`400`，且不产生任何 DB 行与孤立文件。
- `class_id` 一律取自服务端会话，请求体中的 `class_id` / `class` 字段被忽略；不存在「代传其他班」的 API。

## 接口

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | 公开 | 200 JSON；数据库不可读时 503；不重定向 |
| GET | `/login` · POST `/login` | 公开 | 表单登录；失败返回 401 且不建立会话 |
| POST/GET | `/logout` | 登录 | 清空会话 |
| GET | `/materials` | 登录 | 本班材料列表页（教师额外显示上传表单） |
| GET | `/api/materials` | 登录 | 本班材料列表（JSON） |
| GET | `/api/materials/<id>` | 登录 | 材料详情 + 知识库条目；跨班 404 |
| GET | `/api/materials/<id>/download` | 登录 | 下载原文件；跨班 404 |
| POST | `/api/materials/upload` | **教师** | multipart 上传 → 解析 → 同事务入库 → 201 |
| POST | `/materials/upload` | **教师** | 页面表单入口，成功后 302 回列表页 |

## 目录结构

```
app/                   # 应用（create_app / db / auth / materials / knowledge / main / seed / templates）
scripts/init_db.py     # 建表 + 种子数据
scripts/validate_spec.py   # 规约自检（本地等价 openspec validate --strict；可指定变更名）
scripts/check_compose.py   # compose / Dockerfile 静态校验
tests/                 # 验收测试（unittest + 真实 HTTP）
openspec/
  config.yaml                                   # 项目级配置
  changes/add-auth-rbac-class-knowledge/        # 变更一（已实现）
  changes/add-class-scoped-knowledge-search/    # 变更二（规约完成，未实现）
Dockerfile · docker-compose.yml · entrypoint.sh · .env.example
data/app.db            # 运行时生成
uploads/{class_id}/    # 运行时生成
```

## 规划中的检索能力（`add-class-scoped-knowledge-search`）

**尚未实现**，以下为规约确定的行为约定，供实现与验收对照：

| 接口 | 权限 | 说明 |
| --- | --- | --- |
| `GET /api/knowledge/search?q=<词>&limit=<1..20>` | 登录 | 只在本班检索；返回片段与溯源字段 |
| `GET /api/knowledge/sources/<chunk_id>` | 登录 | 读取来源，**重新鉴权 + 核对正文版本**；跨班与不存在同形 404，版本过期 409 |

关键约定（详见该变更的 `design.md`）：

- **班级过滤位置**：出现在候选生成 SQL 的**第一条谓词**上，先于匹配、计数、排序与 `limit`；禁止全库检索后隐藏。
- **匹配规则**：字面子串匹配，无分词/同义词/模糊；ASCII 大小写不敏感；SQL 通配符按字面处理；输入 1..64 码点，`limit` 1..20，越界返回 400。
- **坐标系统**：偏移单位是 **Unicode 码点**，相对 **NFC 归一化后的材料全文**，左闭右开；行号为 LF 归一化后的 1 起行号；摘要对象是**块正文**。
- **降级不伪装**：索引缺失或过期时返回 `degraded:true` + `index_warnings`，**不得**返回空结果冒充"无命中"。
- **向量数据库**：本变更**不采用**（保留扩展点）；若后续采用，必须写明模型、维度、元数据班级过滤与同步机制。

## 数据库

六类核心结构：`classes`（班级）、`users`（用户）、`handouts`（讲义）、`assignments`（作业）、`assistants`（助手）、`skills`（技能）；
班级私有数据：`materials`（材料）、`knowledge_entries`（知识库条目，与材料同事务写入，均带 `class_id`）。

规划中新增：`knowledge_chunks`（检索用分块索引，派生数据，可由 `materials` + `knowledge_entries` 重建）。

## 验收

```bash
python tests/test_acceptance.py    # 场景级测试：FR-01~05 / NFR-01~03 的每个 Scenario（含 A11 重启留存）
python tests/http_acceptance.py    # 真实 HTTP + SQL 计数：A1–A10 / A12
python scripts/validate_spec.py    # 规约校验（strict，全部变更）
python scripts/check_compose.py    # T6：Docker 交付物静态校验

# 只校验某个变更 / 追加任务覆盖检查
python scripts/validate_spec.py add-class-scoped-knowledge-search
python scripts/validate_spec.py add-class-scoped-knowledge-search --check-coverage

# 有 openspec CLI 的环境应另跑（本机无 CLI）
openspec validate add-class-scoped-knowledge-search --strict
```

结果记录见 `ACCEPTANCE.md`（逐条引用 spec 原文）。Docker 相关项在无 Docker 环境下以等价方式验证，
详见该文件的「验证方式说明」。
