# Bot lateral "Rebotes en rango": laboratorio

Idea de Keiner (31-oct-2026): hay monedas que tocan varias veces el mismo piso y el mismo techo en poco tiempo.
El bot compraría en el piso, vendería cerca del techo y volvería a comprar, muchas veces, mientras el rango aguante.
Antes de construirlo como bot en papel, `range_lab.py` comprueba con velas reales de 2024-2026 si eso gana
**después de comisiones** y en datos que no se usaron para elegir las reglas. Si nada pasa, no se enciende.

## Cómo se llama esto y dónde está el riesgo

En trading se le dice **operar en rango** o **mercado lateral** (soporte = piso, resistencia = techo), y la familia
de estrategias es la **reversión a la media**. Su versión automática más conocida es el **grid bot** (bot de
cuadrícula o escalera) que ofrece Binance: muchas órdenes de compra escalonadas bajo el precio y cada una vende un
escalón más arriba.

- Gana poco y seguido: cada ida y vuelta deja el ancho del escalón menos 0,2% de comisiones.
- Pierde de golpe cuando el rango se rompe hacia abajo: el bot queda lleno de monedas compradas en todos los
  escalones justo cuando el precio se va. Los grid bots que se anuncian no ponen stop, por eso en una caída como la
  de 2026 quedan "atrapados" con pérdidas grandes.
- Si el rango se rompe hacia arriba no se pierde, pero el bot se queda sin monedas y sin ganar la subida.

Por eso aquí cada rango tiene **corte por ruptura**: si el precio cae bajo el piso menos k ATR, se vende todo.

## Qué simula

| Paso | Regla |
| --- | --- |
| Detección | Al cierre de cada vela de 1 h o 4 h mira las últimas N velas: techo = máximo, piso = mínimo. Vale como rango si el ancho está entre w_min y w_max %, el precio tocó la zona del piso **y** la del techo al menos T veces (zona = 15% del ancho), el rango mide al menos 2 ATR y cada escalón deja ≥ 0,6% |
| Filtros opcionales | Moneda sobre su EMA 200, BTC diario sobre su SMA 200, ADX bajo |
| Órdenes | G escalones de compra límite entre el piso y el techo (10% hacia dentro de cada borde); cada uno vende un escalón más arriba y al venderse vuelve a quedar listo. G = 1 es el rebote simple piso → techo. Solo compra por debajo del precio: nunca persigue |
| Corte | Precio toca piso − k ATR → vende todo a mercado. Cierra sobre techo + k ATR sin monedas → el rango terminó. Dura más de 2 N velas → vende lo que quede y busca otro |
| Capa 1 | 3 rangos perdedores seguidos → 15 velas sin abrir rangos; 2 seguidos → riesgo a la mitad; caída del día ≥ 3% → nada nuevo hasta mañana; -20% desde el máximo → el bot se apaga |
| Capa 3 | Lo asignado a un rango es lo que hace que, si se llenan **todos** los escalones y salta el stop, se pierda como mucho 1,5% del saldo. Máx. 30% del saldo por rango, 85% en total, 3 rangos a la vez |
| Costos | 0,1% por orden (Binance sin descuento BNB) y 0,05% de deslizamiento en stops |

Ejecución sin trampas: se simula en velas de 15 min aunque el rango se detecte en 1 h o 4 h; una orden límite solo
se llena si el precio la **atraviesa**; lo comprado en una vela no se vende en esa misma vela; en la vela que toca
el stop primero se llenan todas las compras y después salta el stop (el peor caso).

Universo: las 40 monedas del Cazador más el oro tokenizado (PAXG), 2024-01 → hoy. Las variantes (~190) se eligen
**solo** con la primera mitad; las 5 mejores se miden en la segunda mitad, completo, por año y en 20 grupos
aleatorios de 20 monedas. Pasa si: completo > 0, validación > 0, ≥ 15 de 20 grupos > 0, caída máx. < 20% y ≥ 40
rangos. El informe también muestra la misma escalera **sin stop** y comprar y mantener, para comparar.

## Cómo correrlo en el VPS (Francia)

En la **Terminal de hPanel** del VPS (hPanel → VPS → *Terminal*), como `root`. Copia solo las líneas de comando.

```
cd /opt/Ingresosbot && git pull
sudo -u ingresosbot mkdir -p data/rango/informes
sudo -u ingresosbot nice -n 10 .venv/bin/python range_lab.py download --csv-dir data/cazador/velas
sudo -u ingresosbot nice -n 10 .venv/bin/python range_lab.py run --csv-dir data/cazador/velas --out-dir data/rango/informes --workers 2
```

Rangos más cortos (detectados en velas de 15 min, de 6 a 24 horas, pedido de Keiner):

```
sudo -u ingresosbot nice -n 10 .venv/bin/python range_lab.py run --set corto --csv-dir data/cazador/velas --out-dir data/rango/informes_15m --workers 2
```

"Temporadas" (idea de Keiner, 1-nov): las mejores formas de 15 y 30 min, operando solo cuando el lateral está
confirmado (la moneda se movió de lado: eficiencia de Kaufman ≤ 0,3 en las últimas 4N velas) y/o cuando un
semáforo está en verde (el bot sigue todos los rangos en sombra, sin dinero, y solo opera si los últimos 20 rangos
de todas las monedas, o los últimos 5 de esa moneda, suman ganancia):

```
sudo -u ingresosbot nice -n 10 .venv/bin/python range_lab.py run --set temporadas --csv-dir data/cazador/velas --out-dir data/rango/informes_temporadas --workers 1
```

"Ruptura del lateral" (idea de Keiner, 1-nov): los rangos se rompen más de lo que rebotan, así que se prueba
entrar A FAVOR de la ruptura. Rango confirmado por toques (también anchos, hasta 25%), con o sin rebotes dentro;
si cierra sobre el techo + 0,5 ATR compra en la vela siguiente, y si salta el stop bajo el piso abre un corto
(solo papel: con dinero real los cortos requieren Futuros 1x). Stop inicial en la mitad del rango y luego trailing
a 3 o 5 ATR. Riesgo 1,5% o 3% (más riesgo, como pidió Keiner). 160 variantes:

```
sudo -u ingresosbot nice -n 10 .venv/bin/python range_lab.py run --set ruptura --csv-dir data/cazador/velas --out-dir data/rango/informes_ruptura --workers 1
```

La descarga solo baja lo que falta desde la última vez (y PAXG completo, unos minutos). La corrida tarda unos
20-40 minutos y no toca a los bots que están encendidos. Para ver el resultado:

```
cat /opt/Ingresosbot/data/rango/informes/informe_rango.md
```

Archivos: `informe_rango.md` (resumen y veredicto), `variantes_rango.csv` (todas las variantes),
`rangos_mejor.csv` (cada rango de la mejor variante) y, solo si alguna pasa, `rango_params_candidato.json`.
