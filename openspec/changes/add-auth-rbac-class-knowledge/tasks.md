# Tasks: add-auth-rbac-class-knowledge

> 规约 §8 要求「当堂保持全部未勾选」；本仓库已完成 Apply（第 3 课内容），因此下列任务均已勾选并附 `verify:` 命令。
> 每条任务 MUST 附带可执行的 verify:。

## T1 仓库与文档

- [x] README 三行说明（价值 / 场景 / 不做）与 Docker 启动方式
  - verify: `python -c "import pathlib;t=pathlib.Path('README.md').read_text(encoding='utf-8');assert '## 价值' in t and '## 场景' in t and '## 不做' in t;print('README 三行说明就位')"`
- [x] `requirements.txt`、`wsgi.py` 应用入口、`app/` 包骨架
  - verify: `python -c "import flask,bcrypt;print('deps ok')"` 与 `python -c "from app import create_app;print('package ok')"`
- [x] 项目规则文件 `AGENTS.md`（写明「无规约不写代码」）与 `openspec/config.yaml`
  - verify: `python scripts/validate_spec.py`

## T2 数据模型与种子

- [x] `scripts/init_db.py` 建六类表（classes/users/handouts/assignments/assistants/skills）+ materials + knowledge_entries
  - verify: `python scripts/init_db.py --force` 随后 `python -c "import sqlite3;c=sqlite3.connect('data/app.db');print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\")])"`
- [x] 种子 A/B 班与账号（教师 A、学生 A1、学生 B1），两班标题可区分（含「A 班」/「B 班」）
  - verify: `python -c "import sqlite3;c=sqlite3.connect('data/app.db');print(c.execute('SELECT c.name,m.title FROM materials m JOIN classes c ON c.id=m.class_id').fetchall())"`
- [x] `app/db.py` 参数化查询与按 `class_id` 的列表封装（`list_materials(class_id)` 等）
  - verify: `python -c "import inspect,app.db as d;src=inspect.getsource(d.list_materials);assert 'WHERE m.class_id = ?' in src;print('列表查询已强制 class_id 过滤')"`

## T3 登录、会话与密码

- [x] `/login` 页面与 POST 逻辑；会话写入 `role`、`class_id`
  - verify: `python tests/http_acceptance.py`（A1/A2）
- [x] `SECRET_KEY` 仅来自环境变量，缺失即启动失败
  - verify: `python -c "import os;os.environ.pop('SECRET_KEY',None);import app;app.create_app();print('不应到达这里')"`（应抛 ConfigError 且非零退出）
- [x] 登出与 `login_required`（页面 302 / API 401）
  - verify: `python tests/http_acceptance.py`（A3）

## T4 班级隔离

- [x] 列表仅查 `session.class_id`
  - verify: `python tests/http_acceptance.py`（A8）
- [x] 按 id 访问先查行、再校验 class，跨班与不存在统一 404
  - verify: `python tests/http_acceptance.py`（A7 与 /api/materials/<id> 用例）
- [x] 路由不接受客户端传入的 class_id 覆盖（上传时显式忽略 class 字段）
  - verify: `python tests/http_acceptance.py`（ignored-class-field 用例）

## T5 角色权限与上传入库

- [x] 学生上传 403，且无 DB 新增行、无落盘文件
  - verify: `python tests/http_acceptance.py`（A4）
- [x] 教师上传：落盘 → 解析 → 同事务双表写入 → 201
  - verify: `python tests/http_acceptance.py`（A5）
- [x] 列表可见新记录（教师与学生刷新均可）
  - verify: `python tests/http_acceptance.py`（A6）
- [x] 失败路径 4xx 无脏数据（不支持格式 / 空文件 / 非 UTF-8 损坏文件）
  - verify: `python tests/http_acceptance.py`（A12）

## T6 Docker Compose 与 `/health`

- [x] Dockerfile、docker-compose.yml、entrypoint.sh、.env.example、volumes
  - verify: `python scripts/check_compose.py`
- [x] `GET /health` 免登录、返回 200 JSON
  - verify: `python tests/http_acceptance.py`（health 免登录用例，含无 Cookie 访问）
- [x] 启动自动 init_db；down/up 数据留存（依赖 ./data 与 ./uploads 卷）
  - verify: `python tests/test_acceptance.py`（persistence 用例：重启应用后数据仍在）
- [x] README 写明 compose 步骤
  - verify: `python -c "t=open('README.md',encoding='utf-8').read();assert 'docker compose up --build' in t;print('README 启动步骤就位')"`

## T7 规约与收尾

- [x] 规约校验 `--strict` 无 error
  - verify: `python scripts/validate_spec.py`（本地等价实现；若已安装 openspec CLI，可执行 `openspec validate add-auth-rbac-class-knowledge --strict`）
- [x] 手工对照三条关键 Scenario（跨班被拒、学生上传 403、上传后列表可查）并记录结果
  - verify: 见 `ACCEPTANCE.md` 中 FR-03 S1 / FR-02 S1 / FR-04 S1 三节，均附实测输出
