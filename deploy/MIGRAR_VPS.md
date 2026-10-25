# Mudar el bot a otro VPS (USA → Francia)

Objetivo: que el bot corra en el VPS de Francia, donde `api.binance.com` sí responde (en USA da HTTP 451),
conservando el historial (bases `data/*.db`), la configuración (`.env`) y el dominio `bot.commonbird.com`.
Sigue siendo **paper trading**: no hacen falta API keys de Binance.

Todo se hace como **root** en la terminal de cada VPS. Copia solo las líneas de comando, no el prompt
(`[root@servidor ~]#`). Donde pone `IP_FRANCIA` escribe la IP del VPS nuevo.

## 0. Comprobar que Binance responde desde Francia (VPS nuevo)

```bash
curl -s https://api.binance.com/api/v3/ping; echo
```

Debe salir `{}`. Si sale un error 451 o "restricted location", **para aquí**: ese VPS tampoco sirve para Binance.

## 1. Descargar el código (VPS nuevo)

Ubuntu/Debian:
```bash
apt-get update && apt-get install -y git curl
```
AlmaLinux/Rocky:
```bash
dnf install -y git curl
```
Después, en los dos casos:
```bash
git clone https://github.com/keineria/Ingresosbot.git /opt/Ingresosbot
git config --global --add safe.directory /opt/Ingresosbot
```
Todavía **no** ejecutes el instalador: primero traemos los datos del VPS viejo.

## 2. Parar el bot viejo y empaquetar sus datos (VPS de USA)

Se para antes de copiar para que la base no cambie a medias y para que no lleguen alertas de Telegram
duplicadas. El panel viejo deja de responder desde aquí.

```bash
systemctl stop ingresosbot ingresosbot-trend ingresosbot-web
cd /opt/Ingresosbot && tar czf /root/ingresosbot-migracion.tgz .env data/*.db
scp /root/ingresosbot-migracion.tgz root@IP_FRANCIA:/root/
```

`scp` pregunta `Are you sure you want to continue connecting (yes/no)?` → escribe `yes`. Luego pide la
contraseña de root **del VPS de Francia** (no se ve mientras escribes; pulsa Enter al terminar).

> Si algo sale mal más adelante, el bot viejo vuelve con
> `systemctl start ingresosbot ingresosbot-trend ingresosbot-web` en el VPS de USA.

## 3. Instalar en el VPS nuevo

```bash
cd /opt/Ingresosbot && tar xzf /root/ingresosbot-migracion.tgz
sed -i '/^MARKET_SOURCES=/d' .env && echo 'MARKET_SOURCES=binance,binance_vision,bybit' >> .env
bash deploy/install_almalinux.sh
systemctl enable --now ingresosbot-trend
```

`MARKET_SOURCES` pone `api.binance.com` como fuente principal y deja la copia pública
`data-api.binance.vision` y Bybit de respaldo. El instalador sirve para AlmaLinux y para Ubuntu/Debian,
conserva el `.env` (Telegram y clave del panel) y las bases copiadas.

Comprobar:
```bash
systemctl --no-pager status ingresosbot ingresosbot-trend ingresosbot-web | grep -E '●|Active'
curl -s http://127.0.0.1:8050/healthz; echo
tail -n 20 /opt/Ingresosbot/logs/bot.log
```
Los tres servicios deben decir `active (running)`, el panel `{"ok": true}` y en Telegram debe llegar el
aviso de arranque una sola vez por bot.

## 4. Apuntar el dominio al VPS nuevo

Donde creaste el registro de `bot.commonbird.com` (el DNS de `commonbird.com`; si el dominio está en
Hostinger: **hPanel → Dominios → commonbird.com → DNS / Nameservers**), edita el registro **A** `bot` y
cambia `31.97.11.31` por `IP_FRANCIA`. Guarda. Suele tardar de 5 a 30 minutos. Para saber si ya cambió:

```bash
getent hosts bot.commonbird.com
```
Cuando muestre `IP_FRANCIA`, sigue con el paso 5.

## 5. HTTPS y proxy en el VPS nuevo

### Opción A — VPS sin panel (lo normal en Ubuntu/Debian): Caddy

Caddy pide y renueva el certificado de Let's Encrypt solo.

Ubuntu/Debian:
```bash
apt-get install -y caddy || {
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update && apt-get install -y caddy; }
command -v ufw >/dev/null && ufw status | grep -q active && ufw allow 80,443/tcp
```
AlmaLinux sin CyberPanel:
```bash
dnf install -y 'dnf-command(copr)' && dnf copr enable -y @caddy/caddy && dnf install -y caddy
firewall-cmd --permanent --add-service=http --add-service=https && firewall-cmd --reload
setsebool -P httpd_can_network_connect 1
```
Después, en los dos casos:
```bash
cp /opt/Ingresosbot/deploy/Caddyfile /etc/caddy/Caddyfile
systemctl enable caddy && systemctl restart caddy
```
Si el proveedor tiene un firewall en su web (p. ej. Hostinger **VPS → Firewall**), abre ahí también 80 y 443.

### Opción B — VPS con CyberPanel

Sigue [`CYBERPANEL_SETUP.md`](CYBERPANEL_SETUP.md) igual que en el VPS de USA. Si CyberPanel emite un
certificado de prueba (STAGING), emite el real con acme.sh usando `--webroot /usr/local/lsws/Example/html`.

Prueba: abre `https://bot.commonbird.com/?key=TU_DASHBOARD_ACCESS_KEY` (la clave está en
`/opt/Ingresosbot/.env`, la misma que antes).

## 6. Apagar el VPS viejo para siempre (VPS de USA)

Solo cuando el panel nuevo funcione y Telegram reciba avisos desde Francia:

```bash
systemctl disable --now ingresosbot ingresosbot-trend ingresosbot-web
```

Así, aunque el VPS de USA se reinicie, no vuelve a mandar alertas duplicadas. La carpeta
`/opt/Ingresosbot` y el archivo `/root/ingresosbot-migracion.tgz` pueden quedarse ahí como copia de seguridad.

## Actualizar después de una fusión en GitHub (VPS nuevo)

```bash
git -C /opt/Ingresosbot pull && bash /opt/Ingresosbot/deploy/install_almalinux.sh
systemctl restart ingresosbot ingresosbot-trend ingresosbot-web
```
