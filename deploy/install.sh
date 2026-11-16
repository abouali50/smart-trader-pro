#!/usr/bin/env bash
# Installation d'Smart Trader Pro sur un VPS Debian/Ubuntu.
# Auteur : Aziz Mernissi
#
#   curl -fsSL https://raw.githubusercontent.com/abouali50/smart-trader-pro/main/deploy/install.sh | sudo bash
#
# Relancer la meme commande met le scanner a jour sans toucher a config.json ni a .env.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/abouali50/smart-trader-pro.git}"
APP_DIR="${APP_DIR:-/opt/smart-trader-pro}"
APP_USER="smarttrader"

if [ "$(id -u)" -ne 0 ]; then
  echo "Lance ce script avec sudo ou en root." >&2
  exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "Ce script attend Debian ou Ubuntu (apt-get introuvable)." >&2
  exit 1
fi

echo "==> Paquets systeme"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv >/dev/null

echo "==> Code dans $APP_DIR"
git config --system --add safe.directory "$APP_DIR" 2>/dev/null || true
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Environnement Python"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Configuration"
[ -f "$APP_DIR/config.json" ] || cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
[ -f "$APP_DIR/.env" ] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Service systemd"
sed "s#/opt/smart-trader-pro#$APP_DIR#g" "$APP_DIR/deploy/smart-trader-pro.service" > /etc/systemd/system/smart-trader-pro.service
sed "s#/opt/smart-trader-pro#$APP_DIR#g" "$APP_DIR/deploy/smart-trader-pro-web.service" > /etc/systemd/system/smart-trader-pro-web.service
systemctl daemon-reload
systemctl enable smart-trader-pro smart-trader-pro-web >/dev/null 2>&1
systemctl restart smart-trader-pro smart-trader-pro-web

WEB_PORT="$(grep -E '^DASHBOARD_PORT=[0-9]+$' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 || true)"
WEB_PORT="${WEB_PORT:-8765}"
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
WEB_URL="http://${SERVER_IP:-ADRESSE_DU_SERVEUR}:$WEB_PORT"
if grep -q '^DASHBOARD_HOST=127\.0\.0\.1$' "$APP_DIR/.env" 2>/dev/null; then
  # Un nom de domaine est branche (deploy/domain.sh) : le port reste ferme.
  WEB_URL="ton nom de domaine (acces direct par le port ferme)"
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$WEB_PORT"/tcp >/dev/null
fi

cat <<MSG

Smart Trader Pro est installe et tourne.

  Tableau de bord web        : $WEB_URL
  Voir les signaux en direct : journalctl -u smart-trader-pro -f
  Reglages                   : nano $APP_DIR/config.json
  Telegram, CoinMarketCap    : nano $APP_DIR/.env
  Appliquer un changement    : systemctl restart smart-trader-pro
  Compte administrateur      : cd $APP_DIR && sudo -u $APP_USER .venv/bin/python -m smart_trader_pro admin
  Backtest                   : cd $APP_DIR && sudo -u $APP_USER .venv/bin/python -m smart_trader_pro backtest
MSG
