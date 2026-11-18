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
frontend_lock_hash="$(.venv/bin/python -c 'import hashlib, pathlib; p = pathlib.Path("frontend/package-lock.json"); print(hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "")')"
if [[ ! -d frontend/node_modules ]] || [[ ! -f .venv/.frontend-lock-hash ]] || [[ "$(cat .venv/.frontend-lock-hash)" != "$frontend_lock_hash" ]]; then
  npm ci --prefix frontend
  printf '%s' "$frontend_lock_hash" > .venv/.frontend-lock-hash
else
  printf '前端依赖未变化，跳过 npm ci。\n'
fi
frontend_build_hash="$(.venv/bin/python - <<'PY'
import hashlib
from pathlib import Path

root = Path("frontend")
candidates = [
    root / "package.json",
    root / "package-lock.json",
    root / "index.html",
    *root.glob("tsconfig*.json"),
    *root.glob("vite.config.*"),
]
for directory in (root / "src", root / "public"):
    if directory.is_dir():
        candidates.extend(p for p in directory.rglob("*") if p.is_file())

excluded_dirs = {"node_modules", "dist"}
seen = {}
for path in candidates:
    if not path.is_file():
        continue
    rel = path.relative_to(root)
    if any(part in excluded_dirs for part in rel.parts):
        continue
    if rel.name == ".DS_Store" or rel.name.endswith(".tsbuildinfo"):
        continue
    seen[rel.as_posix()] = path

digest = hashlib.sha256()
for rel_posix in sorted(seen):
    data = seen[rel_posix].read_bytes()
    digest.update(f"{rel_posix}\0{len(data)}\0".encode("utf-8"))
    digest.update(data)
print(digest.hexdigest())
PY
)"
if [[ ! -f frontend/dist/index.html ]] || [[ ! -f .venv/.frontend-build-hash ]] || [[ "$(cat .venv/.frontend-build-hash)" != "$frontend_build_hash" ]]; then
  npm run build --prefix frontend
  printf '%s' "$frontend_build_hash" > .venv/.frontend-build-hash
else
  printf '前端构建产物已是最新，跳过 npm run build。\n'
fi
printf '\n报表对比：http://127.0.0.1:%s\n按 Ctrl+C 停止服务。\n\n' "${PORT:-8765}"
exec .venv/bin/python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port "${PORT:-8765}"

