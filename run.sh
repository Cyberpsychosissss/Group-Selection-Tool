#!/usr/bin/env bash
# 本地启动（开发模式：带身份切换栏，改代码自动重载）
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt -r requirements-dev.txt
exec env GST_DEV="${GST_DEV:-1}" .venv/bin/uvicorn app.web:app --host 127.0.0.1 --port "${PORT:-8000}" --reload
