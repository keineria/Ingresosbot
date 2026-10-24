# Publicar el panel con SSL en CyberPanel 3.0 (OpenLiteSpeed)

El panel (`web_server.py`) escucha solo en `127.0.0.1:8050`. OpenLiteSpeed recibe el tráfico HTTPS en el 443 (que ya está abierto) y lo reenvía por dentro del servidor, así que **no hay que abrir ningún puerto nuevo en `firewalld`**.

Antes de empezar: ejecuta `sudo bash deploy/install_almalinux.sh` y comprueba que `curl http://127.0.0.1:8050/healthz` devuelve `{"ok": true}`.

## 1. Crear el subdominio

1. En tu proveedor de DNS crea un registro **A**: `bot.tudominio.com` → IP del VPS.
2. En CyberPanel: **Websites → Create Sub Domain** (o **Create Website** si quieres un dominio aparte).
   - Domain: `bot.tudominio.com`
   - Path: el que propone por defecto (no se usa; el contenido lo sirve el bot).
   - PHP: cualquiera (no se usa).

## 2. Emitir el SSL gratis (Let's Encrypt)

**SSL → Manage SSL** → selecciona `bot.tudominio.com` → **Issue SSL**. Espera a que el DNS ya apunte al VPS; si falla, vuelve a intentarlo unos minutos después.

## 3. Proxy inverso hacia 127.0.0.1:8050

**Websites → List Websites → bot.tudominio.com → Manage → vHost Conf** y añade al final:

```
extprocessor ingresosbot {
  type                    proxy
  address                 127.0.0.1:8050
  maxConns                100
  pcKeepAliveTimeout      60
  initTimeout             60
  retryTimeout            0
  respBuffer              0
}

context / {
  type                    proxy
  handler                 ingresosbot
  addDefaultCharset       off
}
```

Guarda. Si CyberPanel ya tenía un bloque `context / { ... }` en ese vHost, sustitúyelo por este (solo puede haber uno). Después, en **Rewrite Rules** del mismo sitio, deja forzado HTTPS:

```
RewriteEngine On
RewriteCond %{HTTPS} !=on
RewriteRule ^(.*)$ https://%{HTTP_HOST}/$1 [R=301,L]
```

Reinicia OpenLiteSpeed: **Server Status → LiteSpeed Status → Reboot Litespeed** (o `sudo systemctl restart lsws`).

Abre `https://bot.tudominio.com/?key=TU_DASHBOARD_ACCESS_KEY` una sola vez: la página guarda la clave en ese navegador y la quita de la URL.

## Si algo falla

| Síntoma | Qué revisar |
|---|---|
| 503 / "Service Unavailable" | `systemctl status ingresosbot-web` y `curl http://127.0.0.1:8050/healthz` en el VPS |
| 503 con el servicio arriba y SELinux en `Enforcing` | `sudo setsebool -P httpd_can_network_connect 1` |
| La página carga pero dice "Clave de acceso inválida" | El valor de `DASHBOARD_ACCESS_KEY` en `.env` (reinicia `ingresosbot-web` tras cambiarlo) |
| El panel no muestra el último tick | `journalctl -u ingresosbot -n 50`: el bot aún no ha cerrado ninguna vela |

Recomendación: no cambies `WEB_HOST` a `0.0.0.0`. Si lo haces, el panel quedaría expuesto sin SSL fuera del proxy.
