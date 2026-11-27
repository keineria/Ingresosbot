# Salidas: por qué los bots "no cierran ganancias" (27-nov-2026)

## Auditoría (exit_audit.py, VPS Francia, 2026-11-27 01:15 UTC)
- No es un error: el trailing se mueve bien (ETH 1538 + 5×105,64 = 2066,20). Con 5 ATR, sin take profit ni
  break-even, una posición puede ir +15% a +25% y su stop seguir por detrás de la entrada.
- Celeste (Tortuga 30): 6 cortos abiertos con +48 USD latentes; solo el de PAXG tenía el stop en la entrada.
- Nathalia y Trend Rider: solo compras; en noviembre todo cayó 20-27% → 1 y 0 operaciones.
- Rupturas de rango: 10 cierres por trailing (5 ganan, 5 pierden), neto +0,64 USD.
- IA: guardián de salidas en sombra (104 MANTENER, 2 SALIR), 0 cierres pedidos.
- Ninguna operación llegó a +1 R y terminó en pérdida.

## Laboratorio (exit_lab.py, motor real de cartera, 500 USD, velas hasta 2026-11-26 20:00 UTC)
Retorno % 2022→hoy · caída máx. % · últimos 12 meses % (caída) · últimos 3 meses %.

| Bot | Salida | 2022→hoy | Caída | 12 m | 3 m | 2026 USD |
|---|---|---|---|---|---|---|
| Nathalia | hoy (trailing 5 ATR) | +141,0 | 21,3 | −5,3 (11,3) | −6,8 | −76 |
| Nathalia | **p2: vende 33% en +2 R** | **+160,3** | **20,8** | **−3,7 (7,8)** | **−3,0** | **+44** |
| Nathalia | cortos en papel | +190,7 | 31,7 | −6,3 (25,5) | −12,2 | −69 |
| Trend Rider | **hoy** | **+185,3** | **19,6** | +7,1 (11,4) | −4,5 | +88 |
| Trend Rider | mejor alternativa (be2) | +141,1 | 20,3 | +7,6 (11,4) | −4,5 | +79 |
| Trend Rider | cortos en papel | +108,0 | 33,1 | −5,3 (31,5) | −1,8 | −67 |
| Celeste | hoy | +186,4 | 19,6 | +15,9 (8,7) | +2,5 | +311 |
| Celeste | **p2: cierra 33% en +2 R** | +151,0 | **18,9** | **+32,1 (6,7)** | +2,3 | +312 |

- Trailing más corto (t3, t4, aj*) empeora a los tres bots; break-even temprano (be1) también.
- Cortos en Nathalia y Trend Rider: suben la caída a 31-33% → descartados.
- La toma parcial mejora los últimos 12 meses de Celeste en todas sus versiones (p1 +28,8, p15 +29,7, p2 +32,1),
  a cambio de menos retorno total (corta parte de las tendencias grandes de 2025).
- Elegido: p2 (`trend_partial_at_r: 2.0`, `trend_partial_pct: 33.0`) en Nathalia y Celeste; Trend Rider sin cambios.

Reproducir en el VPS: ver el encabezado de exit_lab.py (copia en data/salidas/lab, velas en data/salidas/velas).
