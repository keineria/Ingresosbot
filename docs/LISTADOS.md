# Nuevos listados de Binance: laboratorio

Última idea de corto plazo después de descartar el Cazador ([docs/CAZADOR.md](CAZADOR.md)): comprar monedas en sus
primeros días tras listarse en Binance Spot. Es un evento que ocurre pocas veces al mes, no un patrón de velas.
`listing_lab.py` lo prueba con todas las parejas USDT listadas desde 2023 (también las pausadas o retiradas que
Binance aún muestra) antes de construir nada. Si ninguna regla pasa, se descarta.

## Qué prueba

| Familia | Regla |
| --- | --- |
| hora | Comprar 1, 4, 24 o 72 horas después del listado; salir por stop (15% / 30%), trailing 25% opcional o tiempo (1 a 14 días) |
| caida | Esperar a que caiga 20%, 35% o 50% desde su máximo en la primera semana; salir por TP (15% / 30%), stop o tiempo |
| ruptura | Desde el día 2, comprar cuando supera el máximo de las primeras 24 h; stop, trailing o tiempo |

Cada regla también se prueba solo cuando BTC está sobre su SMA 200 diaria (208 reglas en total). Se excluyen las acciones y ETF tokenizados de Binance (bStocks, p. ej. NVDABUSDT, desde junio de 2026): no son criptos nuevas (`--keep-stocks` para incluirlas).

Costos: 0,08% de comisión por orden y deslizamiento de 0,5% por lado en las primeras 24 h (libro fino) y 0,2% después.
Cuenta de 500 USD, 10% del saldo por operación, máx. 3 a la vez, Capa 1 completa y apagado al -20%.

Se elige con la primera mitad de los listados (por fecha) y se valida con la segunda. Pasa si: total > 0,
validación > 0, al menos 15 de 20 sorteos de la mitad de las monedas > 0, sigue > 0 **sin sus 3 mejores
operaciones** (para que una sola moneda no lo explique todo, como pasó con PEPE en el Cazador), caída máx. < 20%
y al menos 40 operaciones.

## Cómo correrlo en el VPS (Francia)

En la Terminal de hPanel del VPS de Francia, como `root` (copia solo las líneas de comando):

```
sudo git -C /opt/Ingresosbot pull
sudo -u ingresosbot bash -c 'cd /opt/Ingresosbot && nice -n 10 .venv/bin/python listing_lab.py download > data/listados_descarga.log 2>&1'
sudo -u ingresosbot bash -c 'cd /opt/Ingresosbot && nice -n 10 .venv/bin/python listing_lab.py run > data/listados_run.log 2>&1'
cat /opt/Ingresosbot/data/listados/informes/informe_listados.md
```

La descarga tarda unos 10-15 minutos (una consulta por moneda) y la prueba 1-2 minutos. Deja en
`data/listados/informes/` el informe, `entrenamiento.csv` (las 208 reglas) y `operaciones_mejor.csv`.

## Resultado (31-oct-2026) y bot en papel

Una regla pasó (242 listados de 2023-01 a 2026-10, sin bStocks): **comprar la primera vez que una vela de 1 h
cierra sobre el máximo del día 1, hasta el día 14; stop -15%, trailing 15% bajo el máximo, salida como mucho a las
168 h, sin filtro de BTC**. Total +28,3% (entrenamiento +8,6%, validación +18,2%), caída máx. 8,7%, 53 operaciones,
gana 47%, PF 1,95; +6,7% sin sus 3 mejores operaciones y 19 de 20 sorteos positivos. Cuidado: fue la 4.ª-5.ª mejor
de las 5 finalistas por entrenamiento (algo de suerte por probar muchas reglas), opera ~1 vez al mes y en 2026 las
rupturas han ido flojas. Desde esta versión también se excluyen BETH, WBETH, BNSOL, XAUT, PAXG y 1MBABYDOGE
(tickers nuevos de activos viejos).

`listing_bot.py` corre esa regla en **papel** con 500 USD (base `data/listados_state.db`), su tarjeta "Nuevos
listados" en el panel (las monedas que vigila salen solas: `"dynamic_symbols": true` en `deploy/bots.json`) y avisos
de Telegram con el prefijo `[Nuevos listados]`. 10% del saldo por operación (5% en THROTTLED), máx. 3, Capa 1 y
apagado automático si el saldo cae 20% desde su máximo. Simula 0,08% de comisión y 0,2% de deslizamiento por lado.

Encenderlo en el VPS (Terminal de hPanel, como `root`):

```
sudo git -C /opt/Ingresosbot pull
sudo cp /opt/Ingresosbot/deploy/ingresosbot-listados.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ingresosbot-listados
tail -n 20 /opt/Ingresosbot/logs/bot-listados.log
```

La primera vela tarda ~1 minuto más (pregunta la fecha de listado de cada pareja USDT una sola vez). Para apagarlo:
botón *Apagar* de su tarjeta, o `sudo systemctl disable --now ingresosbot-listados`.
