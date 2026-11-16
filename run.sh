#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
dependency_hash="$(.venv/bin/python -c 'import hashlib; print(hashlib.sha256(open("requirements.txt","rb").read()).hexdigest())')"
if [[ ! -f .venv/.requirements-hash ]] || [[ "$(cat .venv/.requirements-hash)" != "$dependency_hash" ]]; then
  .venv/bin/python -m pip install -r requirements.txt
  printf '%s' "$dependency_hash" > .venv/.requirements-hash
fi
if [[ ! -d frontend/node_modules ]]; then
  npm ci --prefix frontend
fi
npm run build --prefix frontend
printf '\n报表对比：http://127.0.0.1:%s\n按 Ctrl+C 停止服务。\n\n' "${PORT:-8765}"
exec .venv/bin/python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port "${PORT:-8765}"

