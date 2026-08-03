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

5. **Saldos reales por cuenta**: los saldos de las cuentas con extracto adjunto **salen del propio extracto** (los reporta el paso 4 — no se los pidas a Joaco). Pedile solo los que faltan: tarjetas de crédito (si no adjuntó el PDF), Mercado Pago, Splitwise, efectivo. Escribí `data/staging/saldos_reales.csv` (`cuenta,moneda,saldo,fecha_corte`), sobrescribiendo el anterior. **`fecha_corte` = el día del que son los saldos** (normalmente hoy, si los extractos se exportaron hoy y los saldos se preguntaron hoy) — una sola fecha para todo el archivo. Si un extracto es de otro día, pedile a Joaco el saldo actual de esa cuenta en vez de usar el del extracto: mezclar cosechas inventa diferencias falsas.

6. **Estados de cuenta de crédito (PDF)**: si hay PDFs nuevos en `data/estados/`, corré `C:/Users/joaco/anaconda3/python.exe scripts/conciliar_estado.py` (sin argumento procesa todos los pendientes) y leé su salida. Recordá que **Santander Mastercard y BBVA Crédito NO mandan avisos por transacción**: sus consumos solo entran por acá.

7. Corré, en este orden, y leé la salida completa de cada uno:
   - `C:/Users/joaco/anaconda3/python.exe scripts/cierre_casa.py <mes>`
   - `C:/Users/joaco/anaconda3/python.exe scripts/conciliar_saldos.py <fecha_corte del paso 5>` — la MISMA fecha que escribiste en `saldos_reales.csv`; el script aborta si no coinciden (guard B10: comparar el calculado de un día contra saldos de otro inventa diferencias falsas).

8. Si `conciliar_saldos.py` reporta cuentas con diferencia ≠ 0: mostrale a Joaco el detalle (movimientos sospechosos del período) y preguntale la causa — anotala en el comentario de `data/saldos.csv` a mano si hace falta (NUNCA ajustar el saldo a ciegas).

9. Regenerá agregados (`scripts/generar_dashboard_data.py`) y corré `scripts/merge_y_validar.py`.

10. Presentá el reporte de cierre:
   - Posición de casa: parte de cada uno, aportes de cada uno (con el detalle de aportes implícitos de Joaco), posición nueva, sugerencia de convergencia para el próximo mes.
   - Presupuesto vs gasto real del mes (`data/presupuesto.csv` si el mes está cargado).
   - Saldos: cuentas OK vs con diferencia, con la causa si se identificó.
   - Pendientes abiertos (conteo por motivo) — recordá que quedan para `/revisar-pendientes`.

11. Commit: `git add` de los CSV/config tocados (`config/ingresos.csv`, `data/aportes_lu.csv`, `data/staging/saldos_reales.csv`, `data/cierre_casa.csv`, `data/saldos.csv`, `data/estados/*`, `data/staging/debitos_procesados.csv`, `data/resoluciones.csv`, dashboard data) con mensaje `cierre de mes <mes>: posicion casa Lu <X> / Joaco <Y>`. Hacé `git push`.

## Chuleta — aprendida en el cierre de julio 2026

**Archivos y cuentas.** Joaco deja los exports del web de Santander en `data/estados/`. Cada cuenta se identifica por **número** (`config/cuentas_bancarias.csv`): `000001322680` Cuenta Casa · `001203500692` caja de ahorro $ · `005203662400` caja de ahorro USD · `800944254160` Mastercard (PDF) · `770054522300` Visa Platinum (PDF). Si aparece un número nuevo, el guard corta: mapearlo ahí antes de seguir. Ojo con los formatos de monto: hay exports con punto decimal y otros con coma.

**Calendario de estados de crédito.** BBVA: PDF por mail, día 1-7. Santander Mastercard (casa): día ~5-8. Visa Platinum: cierra ~28 y llega a fin de mes. El PDF de Santander **no dice la marca** — solo el número.

**SPLITS = falsos positivos garantizados en la conciliación.** El banco trae UNA línea por el total; el ledger lo tiene partido (fila propia + hermana a Deudas), así que el matcher nunca los aparea: la línea del banco aparece como "débito nuevo" y las filas del split como "sin respaldo". Pasó 3 veces en julio (Movistar 1.750,82 = 875,41×2 · Devoto 275,41 = 46,25+229,16 · Desborde 1.785,20 = 892,60×2). **Antes de insertar un débito "nuevo", verificar si es la suma de un split existente.** Si el duplicado ya se insertó, `en_analisis=0` NO alcanza — la conciliación de saldos cuenta movimientos reales: hay que borrar la fila insertada.

**Semillas y fines de semana.** El banco postea sábado/domingo con fecha del lunes. Toda semilla/foto de saldo debe ser la posición **al CIERRE del día** del corte, no una lectura de mediodía (mordió dos veces: "Pizza" 466,66 y KINKO 203,61).

**Patrones USD.** Pagos de tarjeta vía Banred: `TCSANTOL` / `TCBBVAVI` → transferencia interna a la tarjeta, no gasto. `PAHORRO` en ambas cuentas = compra de dólares (dos patas: −UYU y +USD, ambas internas, `en_analisis=0`). El banco factura **~3% de recargo** sobre consumos en moneda extranjera: el importe del estado manda sobre el del aviso (el matcher tolera 6% en no-UYU). Splitwise a veces salda con centavos de diferencia (328,80 vs 328,00) → registrar la comisión para que ambos lados cierren.

**Dos cuentas virtuales de cobranzas, no confundirlas.** `Deudas` = SOLO Splitwise (se concilia contra su API en cada corrida; meter ahí una cobranza ajena rompe ese chequeo). `A cobrar` = plata que le deben a Joaco por fuera de Splitwise (creada 3/8/2026 con el choque del auto: pagó 18.000 de arreglo, 12.600 se los reintegra el tercero). Cuando llegue un reintegro de `A cobrar`, registrarlo como transferencia `A cobrar` → la cuenta donde entró, `en_analisis=0`.

**Recurrentes de casa** (valores a ago-2026; actualizar acá si cambian): gastos comunes ~23.768 TOTAL mensual · contribución inmobiliaria en cuotas de 10.484 — en 2026 quedan agosto (neto 6.690: se adelantaron 3.794 en julio) y setiembre · IMM **bimestral** ~2.252 (jun, ago, …) · ORT 28.170 mensual · psicóloga 10.800 mensual (personal, desde ago-2026).

**Parte vs TOTAL — la trampa que más mordió.** Las filas `origen=planilla_casa` del ledger **ya son la parte de Joaco**; el egreso TOTAL de la casa vive en `data/casa.csv`. Para proyectar el gasto de la casa usar TOTALES (casa.csv hasta jun-2026 + ledger `ambito=casa` desde jul-2026). Nunca comparar una serie contra la otra: fabrica "saltos" ficticios (pasó con la patente y con los gastos comunes).
