# Conectar tu cuenta de Binance en SOLO LECTURA

Con esto el panel (bot.commonbird.com → pestaña **Mi Binance**) te muestra lo que tienes en Binance:
cada moneda, cuánto vale en dólares y en qué billetera está (Spot, Earn o Funding).

**El bot no puede operar con esta llave.** La llave solo tiene permiso de lectura, Binance solo la acepta
desde la IP del VPS, y además el código (`binance_account.py`) solo sabe hacer consultas de saldo: si
la llave tuviera permiso para operar o retirar, el panel se niega a usarla. Los bots siguen en paper.

Son 3 partes: **A** en Binance (en tu PC), **B** en el VPS (Terminal de hPanel) y **C** comprobar.
Tiempo: unos 10 minutos.

> **Nunca pegues la API Key ni la Secret Key en el chat, en GitHub, en WhatsApp ni en capturas de pantalla.**
> Solo van en el archivo `.env` del VPS. Si alguna vez se filtra, bórrala en Binance y crea otra.

---

## A. Crear la llave en Binance (hazlo desde el navegador del PC, no desde la app)

1. Entra a **binance.com** con tu cuenta.
2. Arriba a la derecha, pasa el ratón por el **icono de tu perfil** (el muñequito) → **Gestión de API**
   (en inglés: *API Management*).
   - Si Binance te pide verificar identidad (KYC) o activar la autenticación en dos pasos (2FA), hazlo primero;
     sin eso no deja crear llaves.
3. Pulsa **Crear API** (*Create API*) → elige **Generada por el sistema** (*System generated*) → **Siguiente**.
4. Ponle un nombre que reconozcas, por ejemplo `Ingresosbot-lectura` → **Siguiente**.
5. Binance te pide los códigos de seguridad (correo, SMS o Google Authenticator). Escríbelos.
6. Aparece la llave nueva con dos datos:
   - **API Key** (siempre visible).
   - **Secret Key** (**solo se muestra esta vez**). Cópiala ahora a un lugar seguro y temporal, por ejemplo
     un Bloc de notas sin guardar en tu PC. Si la pierdes, borras la llave y creas otra; no pasa nada.
7. En esa misma llave pulsa **Editar restricciones** (*Edit restrictions*) y deja así:
   - ✅ **Enable Reading** (activado; viene marcado por defecto).
   - ⬜ **Enable Spot & Margin Trading**: SIN marcar.
   - ⬜ **Enable Withdrawals**: SIN marcar.
   - ⬜ Futuros, Margin, Opciones, transferencias universales: SIN marcar.
   - En **Restricciones de acceso por IP** (*IP access restrictions*) elige
     **Restringir el acceso solo a IPs de confianza** (*Restrict access to trusted IPs only*),
     escribe `148.230.115.148` y pulsa **Confirmar**.
8. Pulsa **Guardar** (*Save*) y confirma otra vez con tus códigos.

Ya está: esa llave solo sirve para mirar, y solo desde tu VPS de Francia.

---

## B. Poner la llave en el VPS

1. Entra a **hPanel de Hostinger** → **VPS** → el de Francia (148.230.115.148) → botón **Terminal**
   (arriba a la derecha). Se abre una pantalla negra donde ya eres `root`.
2. Primero trae el código nuevo (solo hace falta una vez, después de que el PR esté unido en GitHub).
   Copia y pega estas dos líneas (sin el `[root@srv… ]#` del principio):
   ```
   sudo git -C /opt/Ingresosbot pull && sudo bash /opt/Ingresosbot/deploy/install_almalinux.sh
   ```
3. Abre el archivo de configuración:
   ```
   nano /opt/Ingresosbot/.env
   ```
4. Baja con la flecha ↓ hasta el final del archivo y escribe estas dos líneas, poniendo tus datos
   después del `=` (sin espacios ni comillas):
   ```
   BINANCE_API_KEY=pega_aqui_la_API_Key
   BINANCE_API_SECRET=pega_aqui_la_Secret_Key
   ```
   Para pegar en la Terminal de hPanel: **clic derecho → Pegar**, o **Ctrl + Shift + V**.
5. Guarda y sal: **Ctrl + O**, luego **Enter** (guarda), luego **Ctrl + X** (sale).
6. Reinicia el panel para que lea la llave:
   ```
   sudo systemctl restart ingresosbot-web
   ```
7. Borra el Bloc de notas donde tenías la Secret Key (ya está guardada en el VPS).

---

## C. Comprobar

1. En la misma Terminal, prueba la conexión (no muestra la clave, solo el saldo):
   ```
   cd /opt/Ingresosbot && sudo -u ingresosbot .venv/bin/python binance_account.py
   ```
   Si todo va bien verás algo como:
   ```
   Llave ···a1B2: solo lectura OK · IP restringida: sí
   Total estimado: 512.34 USD  (Spot 400.00 · Earn 100.00 · Funding 12.34)
   ```
2. Abre **https://bot.commonbird.com** → arriba, la pestaña **Mi Binance**.

### Si sale un error

| Mensaje | Qué hacer |
|---|---|
| «Binance rechazó la llave… IP del VPS» | Revisa en Binance que la IP es exactamente `148.230.115.148` y que copiaste la API Key completa. |
| «La Secret Key no corresponde a la API Key» | Vuelve a copiar la Secret Key en el `.env` (sin espacios al final). Si ya no la tienes, borra la llave y crea otra. |
| «Por seguridad no se usa esta llave: tiene permiso para…» | Esa llave permite operar o retirar. Bórrala en Binance y crea una nueva solo con *Enable Reading*. |
| «Aún no hay llave de Binance en el .env» | Faltan las dos líneas del paso B4, o no reiniciaste con el paso B6. |
| «Earn: …» o «Funding: …» en las notas | Esa parte no se pudo leer; el resto del saldo sí se muestra. |

## Para quitar la conexión

En Binance → Gestión de API → **Eliminar** la llave. Opcional: borra las dos líneas `BINANCE_…` del `.env`
y reinicia `ingresosbot-web`. Nada más del bot depende de esto.

## Detalles técnicos (para quien revise el código)

- `binance_account.py` usa solo la librería estándar (urllib + hmac SHA-256). Firma con la hora del servidor
  de Binance (`/api/v3/time`) para evitar errores de reloj.
- Llamadas posibles, todas de lectura (`READ_ONLY_CALLS`): `GET /api/v3/account`, `GET /sapi/v1/account/apiRestrictions`,
  `GET /sapi/v1/simple-earn/flexible/position`, `GET /sapi/v1/simple-earn/locked/position`,
  `POST /sapi/v1/asset/get-funding-asset` (consulta de saldo que Binance define como POST) y los precios públicos.
  Cualquier otra ruta o método se rechaza antes de salir a la red; las pruebas lo verifican.
- Antes de leer el saldo consulta los permisos de la llave; si tiene trading, retiros, transferencias, margin,
  futuros u opciones, no lee nada más.
- El panel guarda la lectura 30 s (no consulta a Binance en cada refresco) y `GET /api/binance` solo acepta la
  clave del panel en la cabecera, no en la URL. La Secret Key nunca sale del servidor; el panel solo muestra los
  últimos 4 caracteres de la API Key.
