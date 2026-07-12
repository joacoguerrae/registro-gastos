---
description: Revisión semanal de la bandeja de pendientes del ledger de gastos (categorías, ámbitos, duplicados)
---

Revisión semanal de pendientes del sistema de gastos.

1. Leé `data/pendientes.csv` (filas con resuelto vacío/0) y las filas correspondientes de `data/ledger.csv`.
2. Presentale a Joaco los pendientes agrupados por motivo, en tandas cortas y con contexto (fecha, monto, comercio, cuenta). Para cada uno proponé una resolución concreta (la más probable primero). Usá AskUserQuestion cuando haya opciones claras.
3. Con las respuestas: actualizá `data/ledger.csv` (categoria, ambito, en_analisis para duplicados confirmados → 0, estado → confirmado) y marcá resuelto=1 con la decisión en `data/pendientes.csv`.
4. REGLAS NUEVAS: si una decisión es generalizable ("Handy del kiosco X siempre es Super"), agregá el patrón a `config/reglas_comercio.csv` para que no vuelva a caer en pendientes. Idem transferencias recurrentes.
5. Si Joaco confirma que la casa ya volvió a la 8261 (o da la fecha del cambio), registrá la fecha en `config/parametros.json` (clave `fecha_8261_casa`) y resolvé en bloque los `ambito_a_confirmar`: posteriores a la fecha → casa; anteriores → preguntar si eran casa o personal.
5b. SPLITS (`split_a_cargar`): si Joaco confirma el split con fracción f (ej. "yo 1/4, hermanos 3/4"): ajustá monto y monto_uyu de la fila original a ×f, agregá fila hermana tipo=transferencia misma cuenta → cuenta_destino=Deudas por el resto (en_analisis=0, fuente_id=<original>-split) — la suma de ambas DEBE igualar el cargo original (invariante de conciliación, ver docs/pipeline-mails.md sección Splits). Si es recurrente, registrá patrón y fracción en `config/splits_recurrentes.csv`. Recordale cargarlo en Splitwise (hasta que exista la integración API que lo haga sola).
6. Regenerá agregados (`scripts/generar_dashboard_data.py`), corré `scripts/merge_y_validar.py`, commit "revision pendientes <fecha>: N resueltos".
7. Cerrá con el estado: pendientes restantes, gasto del mes vs presupuesto, y cualquier cosa rara detectada.
