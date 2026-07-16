---
description: Ceremonia mensual de cierre — ingresos, aportes de Lu, saldos reales, cierre de casa y conciliación de saldos
---

Cierre mensual del sistema de gastos. Contratos: docs/fase3.md (secciones 3 y 4), docs/esquema.md.

1. Determiná el mes a cerrar (`YYYY-MM`; default el mes calendario que acaba de terminar, o el que Joaco indique). Fijate si `data/cierre_casa.csv` ya tiene una fila para ese mes — si la tiene, avisá que este cierre la va a recalcular.

2. **Ingresos del mes** (`config/ingresos.csv`): si el mes ya tiene fila, mostrala y preguntá si cambió algo. Si no tiene fila, proponé como default repetir los valores del último mes cargado y pedí confirmación o los montos reales de `ingreso_lu` / `ingreso_joaco`. Actualizá el CSV.

3. **Aportes de Lu** (`data/aportes_lu.csv`): preguntale a Joaco si Lu hizo aportes a la cuenta casa en el mes (las transferencias entrantes no generan mail — es el único input manual). Por cada uno: fecha, monto, nota. Agregá las filas (`mes,fecha,monto,nota`).

4. **Extractos de débito** (`data/estados/`, export CSV del web de Santander: Santander UYU 9071, Cuenta Casa 8261, Santander USD). Pedile a Joaco que los suelte ahí si no están. Corré `C:/Users/joaco/anaconda3/python.exe scripts/conciliar_debito.py` (dry-run) y leé la salida. Reporta:
   - **Créditos solo en el extracto = INGRESOS** (sueldo, transferencias recibidas: no generan mail, el ledger está ciego a ellos). Presentáselos a Joaco para clasificar: `Sueldo` / `Otros ingresos` / `Devoluciones & cashback`, o transferencia interna entre cuentas propias (→ `en_analisis=0`).
   - **Débitos solo en el extracto**: gastos sin aviso (comisiones, débitos automáticos).
   - **Filas del ledger sin respaldo**: sospechosas (movimiento que el banco no tiene) — preguntá la causa, NUNCA borrar a ciegas.
   - **Saldo de cierre** de cada cuenta.
   Después corré `conciliar_debito.py --insertar` y aplicá las clasificaciones de Joaco vía `data/resoluciones.csv` (los ingresos entran con pendiente `ingreso_a_clasificar`).

5. **Saldos reales por cuenta**: los saldos de las cuentas con extracto adjunto **salen del propio extracto** (los reporta el paso 4 — no se los pidas a Joaco). Pedile solo los que faltan: tarjetas de crédito (si no adjuntó el PDF), Mercado Pago, Splitwise, efectivo. Escribí `data/staging/saldos_reales.csv` (`cuenta,moneda,saldo`), sobrescribiendo el anterior.

6. **Estados de cuenta de crédito (PDF)**: si hay PDFs nuevos en `data/estados/`, corré `C:/Users/joaco/anaconda3/python.exe scripts/conciliar_estado.py` (sin argumento procesa todos los pendientes) y leé su salida. Recordá que **Santander Mastercard y BBVA Crédito NO mandan avisos por transacción**: sus consumos solo entran por acá.

7. Corré, en este orden, y leé la salida completa de cada uno:
   - `C:/Users/joaco/anaconda3/python.exe scripts/cierre_casa.py <mes>`
   - `C:/Users/joaco/anaconda3/python.exe scripts/conciliar_saldos.py <fecha_corte_de_hoy>` (usa por default `data/staging/saldos_reales.csv` del paso 5)

8. Si `conciliar_saldos.py` reporta cuentas con diferencia ≠ 0: mostrale a Joaco el detalle (movimientos sospechosos del período) y preguntale la causa — anotala en el comentario de `data/saldos.csv` a mano si hace falta (NUNCA ajustar el saldo a ciegas).

9. Regenerá agregados (`scripts/generar_dashboard_data.py`) y corré `scripts/merge_y_validar.py`.

10. Presentá el reporte de cierre:
   - Posición de casa: parte de cada uno, aportes de cada uno (con el detalle de aportes implícitos de Joaco), posición nueva, sugerencia de convergencia para el próximo mes.
   - Presupuesto vs gasto real del mes (`data/presupuesto.csv` si el mes está cargado).
   - Saldos: cuentas OK vs con diferencia, con la causa si se identificó.
   - Pendientes abiertos (conteo por motivo) — recordá que quedan para `/revisar-pendientes`.

11. Commit: `git add` de los CSV/config tocados (`config/ingresos.csv`, `data/aportes_lu.csv`, `data/staging/saldos_reales.csv`, `data/cierre_casa.csv`, `data/saldos.csv`, `data/estados/*`, `data/staging/debitos_procesados.csv`, `data/resoluciones.csv`, dashboard data) con mensaje `cierre de mes <mes>: posicion casa Lu <X> / Joaco <Y>`. Hacé `git push`.
