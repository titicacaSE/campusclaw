#!/usr/bin/env python3
"""Docker 交付物静态校验（规约 NFR-02 / T6）

在没有 Docker 守护进程的环境中，至少保证交付物本身满足规约要求：
  - docker-compose.yml：service app、build: .、ports "8080:8080"、
    volumes ./data:/app/data 与 ./uploads:/app/uploads、env_file .env、healthcheck
  - Dockerfile：安装 requirements、暴露 8080、以 entrypoint.sh 启动
  - entrypoint.sh：库不存在时执行 scripts/init_db.py，并以 gunicorn 绑定 0.0.0.0:8080
  - .env.example：列出 SECRET_KEY 且不含真实密钥
退出码：0 = 全部通过。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

failures: list[str] = []
checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    if not ok:
        failures.append(name)


def read(name: str) -> str:
    path = ROOT / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


compose_text = read("docker-compose.yml")
dockerfile = read("Dockerfile")
entrypoint = read("entrypoint.sh")
env_example = read(".env.example")

# ---------------------------------------------------------------- compose
try:
    import yaml  # type: ignore

    compose = yaml.safe_load(compose_text) or {}
    services = compose.get("services") or {}
    app = None
    for svc in services.values():
        if isinstance(svc, dict) and svc.get("build"):
            app = svc
            break
    check("compose 定义了带 build 的服务", app is not None)
    if app:
        ports = [str(p) for p in (app.get("ports") or [])]
        check("compose ports 含 8080:8080", any("8080:8080" in p for p in ports), str(ports))
        volumes = [str(v) for v in (app.get("volumes") or [])]
        check("./data → /app/data 卷", any("./data:/app/data" in v for v in volumes), str(volumes))
        check("./uploads → /app/uploads 卷", any("./uploads:/app/uploads" in v for v in volumes), str(volumes))
        env_file = app.get("env_file")
        env_files = [env_file] if isinstance(env_file, str) else list(env_file or [])
        check("env_file 指向 .env", any(str(e).endswith(".env") for e in env_files), str(env_files))
        healthcheck = app.get("healthcheck") or {}
        hc_text = str(healthcheck)
        check("compose healthcheck 依赖 /health", "/health" in hc_text, hc_text[:80])
except ImportError:  # 无 PyYAML 时退化为文本校验
    check("compose 含 build 配置", bool(re.search(r"^\s*build:\s*\.\s*$", compose_text, re.M)))
    check("compose ports 含 8080:8080", '"8080:8080"' in compose_text)
    check("./data → /app/data 卷", "./data:/app/data" in compose_text)
    check("./uploads → /app/uploads 卷", "./uploads:/app/uploads" in compose_text)
    check("env_file 指向 .env", "- .env" in compose_text or "env_file" in compose_text)
    check("compose healthcheck 依赖 /health", "/health" in compose_text)

# ---------------------------------------------------------------- Dockerfile
check("Dockerfile 安装 requirements.txt", "pip install" in dockerfile and "requirements.txt" in dockerfile)
check("Dockerfile 暴露 8080", "EXPOSE 8080" in dockerfile)
check("Dockerfile 以 entrypoint.sh 启动", "entrypoint.sh" in dockerfile)
check("Dockerfile 未内置 SECRET_KEY 明文", not re.search(r"SECRET_KEY\s*=\s*[\"']?\w{8,}", dockerfile))

# ---------------------------------------------------------------- entrypoint.sh
check("entrypoint 在库缺失时执行 init_db.py", "init_db.py" in entrypoint and "! -f" in entrypoint)
check("entrypoint 以 gunicorn 绑定 0.0.0.0:8080", "gunicorn" in entrypoint and "0.0.0.0:8080" in entrypoint)

# ---------------------------------------------------------------- .env.example
check(".env.example 列出 SECRET_KEY", bool(re.search(r"^SECRET_KEY=\s*$", env_example, re.M)))
leaked = re.search(r"^SECRET_KEY=\S+", env_example, re.M)
check(".env.example 不含真实密钥", leaked is None, leaked.group(0) if leaked else "")

# ---------------------------------------------------------------- 输出
width = max(len(name) for name, _, _ in checks)
for name, ok, detail in checks:
    print(f"  {'✓' if ok else '✗'} {name.ljust(width)}  {'' if ok else detail}")
print(f"\nDocker 交付物校验：通过 {len(checks) - len(failures)} 项，失败 {len(failures)} 项")
sys.exit(1 if failures else 0)
