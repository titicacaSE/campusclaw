"""WSGI 入口：gunicorn 使用 `wsgi:app`（规约 D1 / NFR-02）。"""
from app import create_app

app = create_app()

if __name__ == "__main__":  # 本地开发：python wsgi.py
    app.run(host="127.0.0.1", port=8080, debug=False)
