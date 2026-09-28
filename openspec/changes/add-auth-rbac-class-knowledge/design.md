# Design: add-auth-rbac-class-knowledge

## 1. 技术决策

| 决策 | 选择 | 备选 |
| --- | --- | --- |
| D1 技术栈 | Flask 3.x + SQLite 3（标准库 `sqlite3`） | Node + Express + better-sqlite3（行为等价即可，换栈须同步改 tasks 命令但 spec 不变） |
| D2 会话 | Flask 服务端 signed cookie session（HttpOnly、SameSite=Lax） | JWT |
| D3 密码 | bcrypt（`bcrypt` 库，cost=12） | PBKDF2-only |
| D4 隔离 | 单库 + `class_id` 过滤 | 每班独立库 |
| D5 解析 | MVP 仅 `.txt` / `.md` | PDF 管道后置 |

## 2. 会话与密码

- 会话 payload 至少含 `user_id`、`role`（`teacher` \| `student`）、`class_id`（规约 §3）。
- Cookie 名 `campusclaw_session`，`HttpOnly=True`、`SameSite=Lax`，由 Flask 签名；客户端无法伪造 `class_id`。
- 口令只以 bcrypt 哈希存储于 `users.password_hash`，库中**不存在** `password` 明文列；校验走 `bcrypt.checkpw`（恒定时间语义）。
- `SECRET_KEY` 只从环境变量读取（Compose 走 `env_file: .env`）；缺失或长度 < 16 时 `create_app()` 抛 `ConfigError`，进程启动失败。

## 3. 服务端班级隔离（FR-03）

1. **单一入口**：所有材料读写都从 `session["class_id"]` 取班级，客户端传入的 `class_id` / `class` 字段被**显式忽略**（`materials._perform_upload()` 从不读取它）。
2. **列表**：`db.list_materials(class_id)` 内部固定 `WHERE m.class_id = ?`，调用方必须传入会话班级。
3. **按 ID 访问**：`db.get_material(material_id, class_id)` 使用 `WHERE m.id = ? AND m.class_id = ?`。
4. **状态码取舍（固定为 404）**：不存在与跨班一律返回 `404 not_found`，理由是避免通过状态码差异枚举他人材料是否存在；响应体不含 B 班标题、正文、文件路径或存储键。README 与 `ACCEPTANCE.md` 均按 404 验收（规约 FR-03 S1 允许 403 或 404，实现择一并固定）。
5. **无代传通道**：不存在「指定目标班级上传」的 API，也没有任何接受客户端 `class_id` 的写接口。
6. **前端不作为安全边界**：模板仅按角色隐藏上传表单，服务端 `teacher_required` 仍对写接口独立鉴权（规约 Non-goals 明示）。

## 4. 上传数据流与事务边界（FR-04）

```
Client (teacher, multipart/form-data)
POST /api/materials/upload
├─ login_required + role == teacher，否则 403（匿名 → 401 / 页面 → 302）
├─ 校验扩展名（.txt/.md）与大小（默认上限 5MiB）
├─ 保存 uploads/{class_id}/{uuid}_{safe_filename}
├─ parse → plain text（UTF-8 严格解码，失败则删文件、返回 400、无 DB 行）
├─ BEGIN TRANSACTION
│   INSERT materials (title, class_id, file_path, subject, uploaded_by, ...)
│   INSERT knowledge_entries (material_id, class_id, body_text, char_count, ...)
├─ COMMIT
└─ 201 { "material_id": ... }
```

- 页面入口 `POST /materials/upload` 复用同一个 `_perform_upload()`，成功后 302 回列表页并提示（上传后列表可见新记录）。
- 任何失败路径（类型不支持、空文件、超限、解析失败、事务异常）都先删落盘文件再返回 4xx，保证「无孤立 DB 行、无孤立文件」。

## 5. 模块划分

```
app/
  __init__.py      # create_app()：SECRET_KEY 校验、错误处理、注册蓝图、确保库就绪
  db.py            # 连接管理、SCHEMA、参数化查询与按 class_id 的业务查询封装
  auth.py          # bcrypt 哈希/校验、会话读写、login_required / teacher_required、登录登出
  materials.py     # 页面与列表、按 id 详情/下载、上传（JSON API + 页面入口）
  knowledge.py     # safe_filename / store_upload / parse_file / 同事务双表写入
  main.py          # 首页跳转、GET /health
  seed.py          # FR-05 六类核心表种子数据
  templates/       # login.html、materials.html
  static/app.css
scripts/init_db.py     # 建表 + 种子（entrypoint 与本地共用）
scripts/validate_spec.py
wsgi.py                # gunicorn 入口 wsgi:app
data/app.db            # 运行时生成
uploads/{class_id}/    # 运行时生成
```

## 6. 部署与可观测（NFR-02）

- `docker compose up --build` 为标准启动方式；容器内 `entrypoint.sh` 先判断 `data/app.db` 是否存在，不存在则执行 `scripts/init_db.py`，再以 `gunicorn --bind 0.0.0.0:8080 wsgi:app` 启动。
- compose 挂载 `./data:/app/data`、`./uploads:/app/uploads`，`down/up` 后数据留存；`env_file: .env` 注入密钥；`healthcheck` 依赖 `/health`。
- `GET /health` 免登录，返回 200 `{"status":"ok","service":"campusclaw","database":"ok"}`；数据库不可读时返回 503。

## 7. 风险与权衡

- **PDF 解析复杂** → 首版仅 txt/md，失败场景已要求无脏数据；PDF 管道后置。
- **跨班 403 与 404 的取舍** → 固定 404，并在 README / 本文件 / ACCEPTANCE 三处一致声明。
- **Flask dev server** → Compose 内使用 gunicorn 绑定 `0.0.0.0:8080`。
- **GBK 编码文件** → MVP 只接受 UTF-8（含 BOM）；其他编码按「损坏文件」返回 400 并删除落盘文件。
