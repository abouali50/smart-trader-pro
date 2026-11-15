#!/usr/bin/env bash
# Relie un nom de domaine au tableau de bord Smart Trader Pro, avec HTTPS, via Caddy.
# Auteur : Aziz Mernissi
#
#   curl -fsSL https://raw.githubusercontent.com/abouali50/smart-trader-pro/main/deploy/domain.sh | sudo bash -s -- trader.exemple.com
#
# Le script ajoute un seul bloc a la fin du Caddyfile, apres en avoir fait une
# copie de sauvegarde. Si Caddy refuse la nouvelle configuration, la copie est
# restauree et rien ne change pour les autres sites.
set -euo pipefail

DOMAIN="${1:-}"
APP_DIR="${APP_DIR:-/opt/smart-trader-pro}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"

fail() { echo "ERREUR : $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "lance ce script avec sudo ou en root."
[ -n "$DOMAIN" ] || fail "indique le nom de domaine, par exemple : trader.exemple.com"
[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] \
  || fail "nom de domaine invalide : $DOMAIN"
command -v caddy >/dev/null 2>&1 || fail "Caddy n'est pas installe sur ce serveur."
[ -f "$CADDYFILE" ] || fail "fichier $CADDYFILE introuvable."
systemctl cat caddy 2>/dev/null | grep -q -- "$CADDYFILE" \
  || fail "le service Caddy n'utilise pas $CADDYFILE ; je ne touche a rien."
[ -f "$APP_DIR/.env" ] || fail "Smart Trader Pro n'est pas installe dans $APP_DIR."

PORT="$(grep -E '^DASHBOARD_PORT=[0-9]+$' "$APP_DIR/.env" | cut -d= -f2 || true)"
PORT="${PORT:-8765}"

echo "==> Verification du nom de domaine"
SERVER_IPS="$(hostname -I 2>/dev/null || true)"
RESOLVED="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | head -1 || true)"
[ -n "$RESOLVED" ] || fail "$DOMAIN n'est pas encore reconnu. Attends quelques minutes apres l'ajout DNS, puis relance."
case " $SERVER_IPS " in
  *" $RESOLVED "*) echo "    $DOMAIN -> $RESOLVED (ce serveur)" ;;
  *) fail "$DOMAIN pointe vers $RESOLVED, pas vers ce serveur ($SERVER_IPS)." ;;
esac

echo "==> Configuration de Caddy"
if grep -qE "^[[:space:]]*(https?://)?${DOMAIN//./\\.}([[:space:],{]|$)" "$CADDYFILE"; then
  echo "    $DOMAIN est deja dans $CADDYFILE, rien a ajouter."
else
  BACKUP="$CADDYFILE.avant-smart-trader-pro.$(date +%Y%m%d-%H%M%S)"
  cp -p "$CADDYFILE" "$BACKUP"
  printf '\n# Smart Trader Pro - tableau de bord\n%s {\n\treverse_proxy 127.0.0.1:%s\n}\n' \
    "$DOMAIN" "$PORT" >> "$CADDYFILE"
  if caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 \
     && systemctl reload caddy; then
    echo "    bloc ajoute ; sauvegarde : $BACKUP"
  else
    cp -p "$BACKUP" "$CADDYFILE"
    systemctl reload caddy >/dev/null 2>&1 || true
    fail "Caddy a refuse la configuration. Le fichier d'origine a ete restaure, tes autres sites ne sont pas touches."
  fi
fi

echo "==> Attente du certificat HTTPS (jusqu'a 90 secondes)"
CODE=000
for _ in $(seq 1 18); do
  CODE="$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://$DOMAIN/" || true)"
  [ "$CODE" = "200" ] && break
  sleep 5
done

if [ "$CODE" = "200" ]; then
  echo "==> Fermeture de l'acces direct par le port $PORT"
  if grep -q '^DASHBOARD_HOST=' "$APP_DIR/.env"; then
    sed -i 's|^DASHBOARD_HOST=.*|DASHBOARD_HOST=127.0.0.1|' "$APP_DIR/.env"
  else
    printf 'DASHBOARD_HOST=127.0.0.1\n' >> "$APP_DIR/.env"
  fi
  chown smarttrader: "$APP_DIR/.env" 2>/dev/null || true
  chmod 600 "$APP_DIR/.env"
  systemctl restart smart-trader-pro-web
  if command -v ufw >/dev/null 2>&1; then
    ufw delete allow "$PORT"/tcp >/dev/null 2>&1 || true
  fi
  printf '\nC est en ligne : https://%s\n' "$DOMAIN"
else
  printf '\nLe bloc est en place, mais https://%s ne repond pas encore (code %s).\n' "$DOMAIN" "$CODE"
  echo "Le certificat peut prendre quelques minutes. Reessaie d'ouvrir l'adresse dans 5 minutes."
  echo "Pour voir ce que dit Caddy : journalctl -u caddy -n 30 --no-pager"
fi
