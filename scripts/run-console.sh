#!/bin/bash
# keepsake console (:8200) 正典启动 —— 11-25 立：三件套必须齐。
# 缺 KEEPSAKE_CREDENTIAL_KEY ⇒ 签发照旧成功但密文不存档，新凭据全成「不可还原」（11-25 local_hermes 实踩）。
# 用法：nohup bash /opt/fragmented-memory/scripts/run-console.sh > /root/.hermes/logs/keepsake-console.log 2>&1 &
cd /opt/fragmented-memory || exit 1
set -a
source /root/.hermes/secrets/180_keepsake_pg.env
source /root/.hermes/secrets/keepsake_cred_key.env
set +a
KEEPSAKE_INTERNAL_TOKEN=$(systemctl --user show keepsake-mcp-http -p Environment --value | grep -oP 'KEEPSAKE_INTERNAL_TOKEN=\K[0-9a-f]+')
export KEEPSAKE_INTERNAL_TOKEN
export HOME=/root
if [ -z "$KEEPSAKE_CREDENTIAL_KEY" ]; then
  echo "FATAL: KEEPSAKE_CREDENTIAL_KEY 缺席——拒绝以不可还原模式启动" >&2
  exit 1
fi
exec /opt/keepsake-venv/bin/python -m uvicorn keepsake.api.app:app --host 0.0.0.0 --port 8200
