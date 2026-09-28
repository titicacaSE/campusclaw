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

exec gunicorn \
  --bind 0.0.0.0:8080 \
  --workers "${GUNICORN_WORKERS:-2}" \
  --timeout 60 \
  --access-logfile - \
  --error-logfile - \
  "wsgi:app"
