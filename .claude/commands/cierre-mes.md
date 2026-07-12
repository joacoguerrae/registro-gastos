---
description: Ceremonia mensual de cierre — ingresos, aportes de Lu, saldos reales, cierre de casa y conciliación de saldos
---

Cierre mensual del sistema de gastos. Contratos: docs/fase3.md (secciones 3 y 4), docs/esquema.md.

1. Determiná el mes a cerrar (`YYYY-MM`; default el mes calendario que acaba de terminar, o el que Joaco indique). Fijate si `data/cierre_casa.csv` ya tiene una fila para ese mes — si la tiene, avisá que este cierre la va a recalcular.

2. **Ingresos del mes** (`config/ingresos.csv`): si el mes ya tiene fila, mostrala y preguntá si cambió algo. Si no tiene fila, proponé como default repetir los valores del último mes cargado y pedí confirmación o los montos reales de `ingreso_lu` / `ingreso_joaco`. Actualizá el CSV.

3. **Aportes de Lu** (`data/aportes_lu.csv`): preguntale a Joaco si Lu hizo aportes a la cuenta casa en el mes (las transferencias entrantes no generan mail — es el único input manual). Por cada uno: fecha, monto, nota. Agregá las filas (`mes,fecha,monto,nota`).

4. **Saldos reales por cuenta**: mostrale la lista de cuentas de `config/saldos_iniciales.csv` (nombre y moneda) y pedile el saldo real actual de cada una (2 minutos: banco, tarjetas — estado de cuenta o app —, Mercado Pago, Splitwise, efectivo). Escribí `data/staging/saldos_reales.csv` (`cuenta,moneda,saldo`) con esos valores, sobrescribiendo el anterior.

5. **Estados de cuenta**: si hay PDFs nuevos en `data/estados/`, corré `C:/Users/joaco/anaconda3/python.exe scripts/conciliar_estado.py` (sin argumento procesa todos los pendientes) y leé su salida.

6. Corré, en este orden, y leé la salida completa de cada uno:
   - `C:/Users/joaco/anaconda3/python.exe scripts/cierre_casa.py <mes>`
   - `C:/Users/joaco/anaconda3/python.exe scripts/conciliar_saldos.py <fecha_corte_de_hoy>` (usa por default `data/staging/saldos_reales.csv` del paso 4)

7. Si `conciliar_saldos.py` reporta cuentas con diferencia ≠ 0: mostrale a Joaco el detalle (movimientos sospechosos del período) y preguntale la causa — anotala en el comentario de `data/saldos.csv` a mano si hace falta (NUNCA ajustar el saldo a ciegas).

8. Regenerá agregados (`scripts/generar_dashboard_data.py`) y corré `scripts/merge_y_validar.py`.

9. Presentá el reporte de cierre:
   - Posición de casa: parte de cada uno, aportes de cada uno (con el detalle de aportes implícitos de Joaco), posición nueva, sugerencia de convergencia para el próximo mes.
   - Presupuesto vs gasto real del mes (`data/presupuesto.csv` si el mes está cargado).
   - Saldos: cuentas OK vs con diferencia, con la causa si se identificó.
   - Pendientes abiertos (conteo por motivo) — recordá que quedan para `/revisar-pendientes`.

10. Commit: `git add` de los CSV/config tocados (`config/ingresos.csv`, `data/aportes_lu.csv`, `data/staging/saldos_reales.csv`, `data/cierre_casa.csv`, `data/saldos.csv`, dashboard data) con mensaje `cierre de mes <mes>: posicion casa Lu <X> / Joaco <Y>`.
