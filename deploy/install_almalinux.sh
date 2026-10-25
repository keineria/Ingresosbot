#!/usr/bin/env bash
# Instalador para AlmaLinux 9 (también Rocky/RHEL 9) y Ubuntu 22.04+/Debian 12. Ejecutar como root desde la carpeta del repo:
#   sudo bash deploy/install_almalinux.sh
# Variables opcionales: APP_USER (por defecto ingresosbot), PYTHON_BIN (por defecto python3.11 o python3).
# Es idempotente: se puede volver a ejecutar tras un `git pull` para actualizar dependencias y servicios.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-ingresosbot}"
UNIT_DIR="/etc/systemd/system"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Ejecuta con sudo: sudo bash deploy/install_almalinux.sh"
[[ -f "$APP_DIR/bot_runner.py" && -f "$APP_DIR/web_server.py" ]] || die "No encuentro bot_runner.py en $APP_DIR"

say "1/6 Python (se prefiere 3.11; mínimo 3.9)"
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if command -v apt-get >/dev/null 2>&1; then
    # Ubuntu/Debian: el python3 del sistema (3.10+) vale; python3-venv no viene instalado de serie.
    apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
  elif ! command -v python3.11 >/dev/null 2>&1; then
    dnf install -y python3.11 python3.11-pip >/dev/null 2>&1 || echo "python3.11 no disponible; se usará python3"
  fi
  PYTHON_BIN="$(command -v python3.11 || command -v python3)"
fi
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  || die "$PYTHON_BIN es demasiado antiguo (se necesita 3.9+)"
echo "Usando $("$PYTHON_BIN" --version) en $PYTHON_BIN"

say "2/6 Usuario de servicio '$APP_USER'"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell "$(command -v nologin || echo /sbin/nologin)" "$APP_USER"
fi

say "3/6 Entorno virtual e instalación de pandas + numpy"
[[ -x "$APP_DIR/.venv/bin/python" ]] || "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install --quiet pandas numpy
"$APP_DIR/.venv/bin/python" -c 'import pandas, numpy; print("pandas", pandas.__version__, "· numpy", numpy.__version__)'

say "4/6 Archivo .env"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "Creado $APP_DIR/.env desde .env.example"
fi
if ! grep -Eq '^DASHBOARD_ACCESS_KEY=.{16,}' "$APP_DIR/.env"; then
  KEY="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  if grep -q '^DASHBOARD_ACCESS_KEY=' "$APP_DIR/.env"; then
    sed -i "s|^DASHBOARD_ACCESS_KEY=.*|DASHBOARD_ACCESS_KEY=$KEY|" "$APP_DIR/.env"
  else
    printf '\nDASHBOARD_ACCESS_KEY=%s\n' "$KEY" >> "$APP_DIR/.env"
  fi
  GENERATED_KEY="$KEY"
fi
mkdir -p "$APP_DIR/data/candles" "$APP_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

say "5/6 Servicios systemd"
# ingresosbot-trend.service (Trend Rider), ingresosbot-tortuga.service (Tortuga 55) e ingresosbot-ai.service
# (agentes de IA) se instalan pero no se activan: ver docs/TREND_RIDER.md, docs/ALTERNATIVAS.md y docs/AGENTES_IA.md.
# El informe diario (ingresosbot-report.timer) tampoco. ingresosbot-bot@.service es la plantilla para bots nuevos
# de deploy/bots.json (docs/PANEL_BOTS.md).
for unit in ingresosbot.service ingresosbot-web.service ingresosbot-trend.service ingresosbot-tortuga.service \
            ingresosbot-report.service ingresosbot-report.timer ingresosbot-bot@.service ingresosbot-ai.service; do
  sed -e "s|/opt/Ingresosbot|$APP_DIR|g" \
      -e "s|^User=.*|User=$APP_USER|" \
      -e "s|^Group=.*|Group=$APP_USER|" \
      "$APP_DIR/deploy/$unit" > "$UNIT_DIR/$unit"
  chmod 644 "$UNIT_DIR/$unit"
done
# SELinux: que systemd pueda ejecutar el Python del venv aunque el repo esté fuera de /opt.
if command -v restorecon >/dev/null 2>&1; then restorecon -R "$APP_DIR" >/dev/null 2>&1 || true; fi
systemctl daemon-reload
systemctl enable --now ingresosbot-web.service
systemctl enable --now ingresosbot.service

say "6/6 Comprobación"
sleep 3
systemctl --no-pager --lines=0 status ingresosbot.service ingresosbot-web.service || true
PORT="$(grep -E '^WEB_PORT=' "$APP_DIR/.env" | cut -d= -f2 || true)"
PORT="${PORT:-8050}"
if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  echo "Panel respondiendo en http://127.0.0.1:$PORT (solo local)."
else
  echo "El panel aún no responde: revisa 'journalctl -u ingresosbot-web -n 50'."
fi
if [[ -n "${GENERATED_KEY:-}" ]]; then
  echo
  echo "Clave del panel generada (guárdala; también está en $APP_DIR/.env):"
  echo "  DASHBOARD_ACCESS_KEY=$GENERATED_KEY"
fi
cat <<MSG

Siguiente paso: completa WEBHOOK_URL / TELEGRAM_* en $APP_DIR/.env, reinicia con
  sudo systemctl restart ingresosbot ingresosbot-web
y publica el panel con SSL en https://bot.commonbird.com siguiendo deploy/CYBERPANEL_SETUP.md
(o deploy/MIGRAR_VPS.md, paso 5, en un VPS sin CyberPanel).
MSG
