#!/bin/sh
# 容器入口（规约 NFR-02）：
#   1) 若 data/app.db 不存在 → 自动执行 scripts/init_db.py（建表 + 种子）
#   2) 以 gunicorn 绑定 0.0.0.0:8080 启动
#   3) SECRET_KEY 由 compose 的 env_file: .env 注入；缺失时应用启动即失败（NFR-01 S3）
set -eu

DB_PATH="${DATABASE_PATH:-/app/data/app.db}"
UP_DIR="${UPLOAD_DIR:-/app/uploads}"

mkdir -p "$(dirname "$DB_PATH")" "$UP_DIR"

if [ ! -f "$DB_PATH" ]; then
  echo "[entrypoint] 未发现数据库 $DB_PATH，执行初始化与种子写入…"
  python scripts/init_db.py --db "$DB_PATH" --uploads "$UP_DIR"
else
  echo "[entrypoint] 复用已有数据库 $DB_PATH（数据持久化：down/up 后记录仍在）"
fi

# 向量库集合：幂等创建。**失败不阻止启动** —— 缺向量库时 keyword 仍可用，
# vector / hybrid 按降级语义返回 503（spec §4）。所以这里用 `||` 吞掉错误。
if [ -n "${QDRANT_URL:-}" ]; then
  echo "[entrypoint] 创建/校验 Qdrant 集合（$QDRANT_URL）…"
  python scripts/init_qdrant.py ||
    echo "[entrypoint] Qdrant 暂不可用，跳过集合初始化（keyword 模式仍可用；vector/hybrid 将返回 503）"
else
  echo "[entrypoint] 未配置 QDRANT_URL，跳过集合初始化（keyword 模式仍可用）"
fi

exec gunicorn \
  --bind 0.0.0.0:8080 \
  --workers "${GUNICORN_WORKERS:-2}" \
  --timeout 60 \
  --access-logfile - \
  --error-logfile - \
  "wsgi:app"
