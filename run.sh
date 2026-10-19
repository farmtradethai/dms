#!/usr/bin/env bash
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --no-cache-dir --disable-pip-version-check -r requirements.txt
[ -f data/.seeded ] || python scripts/seed_ah.py --with-history
echo "Die Management System -> http://localhost:${PORT:-8790}   login ah_admin / 1234"
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8790}
