# ACCEPTANCE.md — CampusClaw `add-auth-rbac-class-knowledge` 验收记录

> 本文件是**验收结论的唯一落点**。每条结论以 `openspec/` 内的规约原文为依据，不引用聊天记录（NFR-03 S2）。
> 规约来源：`openspec/changes/add-auth-rbac-class-knowledge/specs/auth-upload/spec.md`

## 0. 验收环境与总览

| 项目 | 值 |
| --- | --- |
| 运行栈 | CPython 3.13 + Flask 3.1.3 + bcrypt 5.0.0 + SQLite 3（标准库 `sqlite3`） |
| 部署形态 | Dockerfile + docker-compose.yml + gunicorn（NFR-02） |
| 验收日期 | 2026-09-23 |
| 数据基线 | `classes=2 / users=4 / materials=4 / knowledge_entries=4`（`python scripts/init_db.py --force`） |

三条命令即可复现全部结论：

```bash
python tests/test_acceptance.py    # 场景级：23 项，覆盖 spec 每个 Scenario + A11 重启留存
python tests/http_acceptance.py    # 真实 HTTP + SQL 计数：58 项，覆盖 A1–A10 / A12
python scripts/validate_spec.py    # A13 规约校验（strict）
python scripts/check_compose.py    # T6 Docker 交付物静态校验：14 项
```

**总览：`23/23` + `58/58` + `0 error` + `14/14` 全部通过。**

---

## 1. 逐条 Scenario 验收（对照 spec 原文）

### FR-01 用户登录与会话建立

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-01 S1 | 教师 A 或学生 A1 在 `/login` 提交正确账号与口令 → 建立服务端会话，下发会话 Cookie；会话中记录 `user_id`、`role`、`class_id` | `test_acceptance.FR01.test_s1` + `http_acceptance A1` | ✅ 302 且 `Set-Cookie: campusclaw_session`；API 回显 `role=teacher/student`、`class_id=1` |
| FR-01 S2 | 用户名存在但口令错误 → 登录失败且不建立会话；响应不得返回密码哈希等敏感信息 | `test_acceptance.FR01.test_s2` + `http_acceptance A2` | ✅ 401，无 `Set-Cookie`，响应体不含 `$2` 与 `password_hash` |
| FR-01 S3 | 未携带有效会话访问受保护资源 → 页面 302 至 `/login`；API 401 且不含任何班级材料标题、正文或文件路径 | `test_acceptance.FR01.test_s3` + `http_acceptance A3` | ✅ `/materials`→302 `/login`；`/api/materials`→401，响应体不含「讲义」「A 班」`.md` |

### FR-02 角色权限

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-02 S1 | 学生 A1 向 `POST /api/materials/upload` 提交文件 → 返回 403；`materials` 与 `knowledge_entries` 行数不变，上传目录无新文件 | `test_acceptance.FR02.test_s1` + `http_acceptance A4` | ✅ 403（含「无权限」）；两表行数与文件数均不变 |
| FR-02 S2 | 教师 A 提交支持的文件（`.txt`/`.md`）→ 201 且响应含 `material_id`；材料与知识库条目均归属教师所属班级 | `test_acceptance.FR02.test_s2` + `http_acceptance A5` | ✅ 201；`materials.class_id = knowledge_entries.class_id = 1` |
| FR-02 S3 | 学生 A1 打开 `/materials` 或 `GET /api/materials` → 可见本班列表；提供学生可用上传/删除入口？服务端仍拒绝写接口 | `test_acceptance.FR02.test_s3` | ✅ 列表 200；页面 HTML 无 `action="/materials/upload"`；上传仍 403 |

### FR-03 班级隔离（核心）

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-03 S1 | A 班用户通过 URL 路径/路径参数/请求体指定 B 班材料 ID 访问或下载 → 403 或 404（本实现固定 404）；响应不得含 B 班标题、正文片段、文件路径或存储键 | `test_acceptance.FR03.test_s1` + `http_acceptance A7` | ✅ 详情与下载均 404；响应体不含「B 班」「一元二次方程」`.md`；不存在 ID 同为 404 |
| FR-03 S2 | 学生 A1 请求材料列表 → 每条记录均归属班级 A；列表不得出现含「B 班」的可区分标题 | `test_acceptance.FR03.test_s2` + `http_acceptance A8` | ✅ 全部 `class_id == 1`，无「B 班」标题 |
| FR-03 S3 | 教师 A 打开 `/materials` 或 `GET /api/materials` → 每条记录归属班级 A；数据来自带班级过滤的查询（`WHERE class_id = :session_class_id`） | `test_acceptance.FR03.test_s3` + `http_acceptance A8` | ✅ API 标题集合 == `SELECT title … WHERE class_id=1` 结果集；列表条数等于库内本班条数 |
| FR-03 S4 | 教师在请求体额外传入 `class_id`、`class` 等字段（如试图指定 B 班）→ 服务端忽略，材料仍写入会话所属班级；不存在「代传 B 班」的 API | `test_acceptance.FR03.test_s4` + `http_acceptance A9` | ✅ 仍 201 且 `class_id=1`；班级 B 行数未增加 |

### FR-04 材料上传与知识库入库

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-04 S1 | 教师 A 上传合法 `.md`/`.txt` 且解析成功 → `materials` +1、`knowledge_entries` 新增关联班级 A 的记录（同事务）；刷新本班列表可见该标题 | `test_acceptance.FR04.test_s1` + `http_acceptance A5/A6` | ✅ 两表各 +1，标题出现在教师与学生列表 |
| FR-04 S2 | 学生 A1 刷新 `/materials` → 可见教师 A 刚上传的材料；仍为只读，无法通过 API 修改或删除 | `test_acceptance.FR04.test_s2` + `http_acceptance A6` | ✅ 学生列表可见；上传 403；`DELETE /api/materials/<id>` 返回 404/405（无路由） |
| FR-04 S3 | 上传不支持格式（`.exe`）、空文件或解析失败（非 UTF-8 损坏文件）→ 400；两表无残留记录，且不保留孤立未关联文件 | `test_acceptance.FR04.test_s3` + `http_acceptance A12` | ✅ 三类失败均 400；两表行数与落盘文件数均不变 |

### FR-05 预置核心数据

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-05 S1 | 执行初始化脚本或 Compose 首次启动 → 存在班级 A、班级 B，以及教师 A（A 班）、学生 A1（A 班）、学生 B1（B 班）；各用户具备可登录用户名与已哈希密码 | `test_acceptance.FR05.test_s1` | ✅ 两班齐全；三名必需用户在场，`password_hash` 非明文 |
| FR-05 S2 | 查看预置材料 → 至少一条标题含「A 班」且归属班级 A，至少一条含「B 班」且归属班级 B；六类核心表均已创建 | `test_acceptance.FR05.test_s2` | ✅ 标题含「A 班」/「B 班」且归属正确；六表全部存在 |

### NFR-01 密码与密钥安全

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| NFR-01 S1 | 查询 `users.password_hash` → 其值不等于任何预置明文口令；可识别为哈希格式（bcrypt `$2` 前缀） | `test_acceptance.NFR01.test_s1` | ✅ 全部以 `$2` 开头；`users` 表无 `password` 列 |
| NFR-01 S2 | 使用正确口令与错误口令分别登录 → 正确口令建立会话；错误口令验证失败且不建立会话 | `test_acceptance.NFR01.test_s2` | ✅ 302 vs 401 |
| NFR-01 S3 | 未提供环境变量 `SECRET_KEY` 启动应用 → 进程启动失败并给出明确原因；不落入任何不安全默认密钥 | `test_acceptance.NFR01.test_s3` | ✅ 抛 `ConfigError`；长度 < 16 同样被拒 |

### NFR-02 部署与可观测

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| NFR-02 S1 | 按 README 执行 `docker compose up --build` 直至 healthcheck 通过 → 浏览器可访问登录页；`GET /health` 返回 200 且表明服务可用 | `scripts/check_compose.py`（静态校验）+ `test_acceptance.NFR02.test_s1` | ✅ 交付物 14 项齐全；`/health` 200 `{"status":"ok","database":"ok"}` |
| NFR-02 S2 | 不携带任何 Cookie 访问 `/health` → 仍返回成功状态码与 JSON；不发生到登录页的重定向 | `test_acceptance.NFR02.test_s2` + `http_acceptance A10` | ✅ 无 Cookie 时 200、`Content-Type: application/json`、无 `Location` 头 |
| NFR-02 S3 | `docker compose down` 后再次 `up`（未删除 volume）→ 预置班级与用户仍可登录；已上传材料与知识库记录仍可查询 | `test_acceptance.NFR02.test_s3`（等价：同一 `DATABASE`/`UPLOAD_DIR` 重建应用） | ✅ 新应用实例下材料与知识库正文仍可查 |

### NFR-03 可验证性与可追溯

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| NFR-03 S1 | 执行验收测试 → 覆盖错误密码、未登录（页面与 API）、跨班按 ID、跨班列表、学生上传等负路径；同时覆盖登录、上传入库、列表可见等正路径 | `test_acceptance.py`（23）+ `http_acceptance.py`（58） | ✅ 负路径与正路径同等覆盖，见下方 §2 索引 |
| NFR-03 S2 | 在新会话中仅阅读 `openspec/`、README 与项目规则文件 → 可引用 spec 原文回答三条硬性要求；验收结论以 spec 原文为依据 | `test_acceptance.NFR03.test_s2` | ✅ 断言 spec 原文含三条关键句；四件套齐全 |

---

## 2. 验收编号索引 A1–A13

| 编号 | 内容 | 承载文件 | 结果 |
| --- | --- | --- | --- |
| A1 | 会话建立（教师 / 学生，含 Cookie 往返） | `http_acceptance.py` | ✅ |
| A2 | 错误密码登录失败且不泄露哈希 | `http_acceptance.py` | ✅ |
| A3 | 未登录（页面 302 / API 401）与登出后 401 | `http_acceptance.py` | ✅ |
| A4 | 学生上传 403，两表与目录无副作用 | `http_acceptance.py` | ✅ |
| A5 | 教师上传 201，同事务双表写入且同班 | `http_acceptance.py` | ✅ |
| A6 | 上传后教师与学生列表均可见 | `http_acceptance.py` | ✅ |
| A7 | 跨班按 ID 详情/下载 404，不泄露存在性 | `http_acceptance.py` | ✅ |
| A8 | 列表仅含本班，逐条校验 `class_id` | `http_acceptance.py` | ✅ |
| A9 | 上传忽略客户端 `class_id`/`class` 字段 | `http_acceptance.py` | ✅ |
| A10 | `/health` 免登录、200 JSON、不重定向 | `http_acceptance.py` | ✅ |
| A11 | 重启（down/up 未删 volume）后数据留存 | `test_acceptance.py`（等价验证） | ✅ |
| A12 | 失败路径 400 且无脏数据（三类失败） | `http_acceptance.py` | ✅ |
| A13 | 规约校验 `--strict` 无 error | `scripts/validate_spec.py` | ✅ 0 error / 0 warning |

---

## 3. 三条关键 Scenario 实测输出

### 3.1 FR-03 S1 — 跨班按 ID 访问被拒绝（核心隔离）

`http_acceptance.py` 实测（教师 A 会话请求班级 B 的材料 `id=3`）：

```
跨班访问被拒：material_id=3 属于班级 2，来访会话班级 1
127.0.0.1 - - "GET /api/materials/3 HTTP/1.1" 404 -
跨班访问被拒：material_id=3 属于班级 2，来访会话班级 1
127.0.0.1 - - "GET /api/materials/3/download HTTP/1.1" 404 -
127.0.0.1 - - "GET /api/materials/999999 HTTP/1.1" 404 -
```

`test_acceptance.py` 补充断言：响应体不含「B 班」「一元二次方程」`.md`。

### 3.2 FR-02 S1 — 学生上传被拒绝（角色权限 + 无副作用）

```
=== A4 学生上传被拒绝 ===
  [PASS] 学生上传返回 403
  [PASS] 403 消息含「无权限」
  [PASS] materials 行数不变
  [PASS] knowledge_entries 行数不变
  [PASS] 上传目录无新文件
```

### 3.3 FR-04 S1 — 上传后知识库与列表可查（同事务入库）

```
=== A5 教师上传（落盘 → 解析 → 同事务双表写入） ===
  [PASS] 教师上传返回 201
  [PASS] 响应含 material_id
  [PASS] 材料归属班级 A
  [PASS] 知识库条目归属班级 A
  [PASS] 班级 A materials +1
  [PASS] 班级 A knowledge_entries +1
=== A6 上传后列表可见（教师 + 学生） ===
  [PASS] 教师列表可见新上传材料
  [PASS] 学生列表可见新上传材料
```

---

## 4. 验证方式说明（无 Docker 环境下的等价验证）

本机未安装 Docker，Docker 相关结论以**等价方式**验证，力求结论强度不降低：

1. **交付物完整性**（NFR-02 S1 上半）：`scripts/check_compose.py` 对 Dockerfile / docker-compose.yml /
   entrypoint.sh / .env.example 做 14 项静态校验 —— 含 `build` 服务、`8080:8080` 端口映射、
   `./data` 与 `./uploads` 卷、`env_file .env`、healthcheck 指向 `/health`、entrypoint 在库缺失时执行
   `init_db.py`、以 gunicorn 绑定 `0.0.0.0:8080`、`.env.example` 不含真实密钥。**14/14 通过**。
2. **启动行为**（NFR-02 S1 下半）：`GET /health` 在真实 HTTP 下返回 200 JSON，`db.ensure_ready()`
   在库缺失时自动建表 + 写入种子 —— 与 `entrypoint.sh` 的 `init_db.py` 分支同源。
3. **数据留存**（NFR-02 S3）：以「同一 `DATABASE` + `UPLOAD_DIR` 重建应用实例」等价模拟
   `down` / `up` 不删 volume 的场景，验证材料与知识库记录仍可查询（`NFR02.test_s3`）。
4. **待补做（如有 Docker 环境）**：`docker compose up --build` 后确认 healthcheck 转为 healthy、
   浏览器可访问登录页，并执行 `down` → `up` 复核数据留存。

---

## 5. 实现取舍记录（与 spec 的一致性说明）

| 取舍点 | spec 措辞 | 本实现 | 说明 |
| --- | --- | --- | --- |
| 跨班按 ID 的状态码 | 「返回 403 或 404」 | **固定 404** | 避免以状态码差异枚举他人材料存在性；已在 README / design.md 固定 |
| 种子用户数量 | 要求教师 A、学生 A1、学生 B1（3 名） | **额外预置教师 B（4 名）** | FR-04 要求材料必须关联上传者所属班级；若 B 班样本材料署名 A 班教师，种子数据自身即破坏该不变量。教师 B 同时用于验证「B 班教师不能上传/查看 A 班」 |
| 支持的文件类型 | 未限定（MVP） | `.txt` / `.md` | 与 D5 决策一致，其余类型按 400 拒绝并保证无脏数据 |

---

*本文件所有结论均可由 §0 的四条命令重新生成。*
