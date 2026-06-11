#!/usr/bin/env bash
#
# start-dev.sh - sobe a stack de desenvolvimento do NeuroStack no WSL:
#   API (uvicorn :8000) + ngrok + Frontend (Vite :5173).
#
# Pre-requisito (no Windows): Docker Desktop com os containers no ar
#   postgres(:5434) / redis(:6380) / qdrant(:6333).
#
# Uso (dentro do WSL/Ubuntu):
#   bash start-dev.sh
#
set -uo pipefail

# ---- Caminhos / config -----------------------------------------------------
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$PROJECT_DIR/frontend/artifacts/neurostack"
VENV="$HOME/neurostack-venv"
NGROK_DOMAIN="moaning-storeroom-skipper.ngrok-free.dev"
LOG_DIR="/tmp/neurostack-dev-logs"

# node fica em ~/.local/bin e o ngrok (snap) em /snap/bin
export PATH="$HOME/.local/bin:/snap/bin:$PATH"

# Variaveis exigidas pelo vite.config.ts (ele lanca erro se faltarem)
export PORT=5173
export BASE_PATH=/
export NODE_ENV=development

mkdir -p "$LOG_DIR"

die() { echo "ERRO: $*" >&2; exit 1; }

# ---- Checagens -------------------------------------------------------------
[ -x "$VENV/bin/python" ] || die "venv ausente em $VENV (crie: python3 -m venv $VENV && $VENV/bin/pip install -r requirements.txt)"
command -v node  >/dev/null || die "node nao encontrado no PATH"
command -v ngrok >/dev/null || echo "AVISO: ngrok nao encontrado no PATH - o tunel publico nao vai subir"

echo "==> Encerrando instancias antigas..."
pkill -f 'uvicorn src.api.main:app' 2>/dev/null || true
pkill -f 'vite.config.ts'           2>/dev/null || true
pkill -f 'ngrok http'               2>/dev/null || true
sleep 1

echo "==> Conferindo infra (Docker no Windows) via localhost..."
for entry in 5434:postgres 6380:redis 6333:qdrant; do
  port="${entry%%:*}"; name="${entry##*:}"
  if timeout 2 bash -c "echo > /dev/tcp/localhost/$port" 2>/dev/null; then
    echo "   ok    $name (:$port)"
  else
    echo "   AVISO $name (:$port) nao responde - suba os containers no Docker Desktop."
  fi
done

# ---- Servicos --------------------------------------------------------------
echo "==> API (uvicorn :8000)..."
( cd "$PROJECT_DIR" && nohup "$VENV/bin/python" -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000 >"$LOG_DIR/api.log" 2>&1 & )

printf "   aguardando /health"
for _ in $(seq 1 30); do
  if [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' http://localhost:8000/health 2>/dev/null)" = "200" ]; then
    echo " OK"; break
  fi
  printf "."; sleep 1
done

echo "==> ngrok (-> :8000)..."
( nohup ngrok http --domain="$NGROK_DOMAIN" 8000 --log=stdout >"$LOG_DIR/ngrok.log" 2>&1 & )

echo "==> Frontend (Vite :5173)..."
( cd "$FRONTEND_DIR" && nohup node node_modules/vite/bin/vite.js --config vite.config.ts --host 0.0.0.0 >"$LOG_DIR/vite.log" 2>&1 & )

sleep 1
cat <<EOF

==================================================
 NeuroStack - ambiente de desenvolvimento no ar
==================================================
 Frontend : http://localhost:5173
 API/Docs : http://localhost:8000/docs
 Publico  : https://$NGROK_DOMAIN
--------------------------------------------------
 Logs  : $LOG_DIR/{api,ngrok,vite}.log
 Parar : pkill -f 'uvicorn src.api.main:app'; pkill -f vite.config.ts; pkill -f 'ngrok http'
==================================================
EOF
