---
description: Ceremonia mensual de cierre — ingresos, aportes de Lu, saldos reales, cierre de casa y conciliación de saldos
---

Cierre mensual del sistema de gastos. Contratos: docs/fase3.md (secciones 3 y 4), docs/esquema.md.

1. Determiná el mes a cerrar (`YYYY-MM`; default el mes calendario que acaba de terminar, o el que Joaco indique). Fijate si `data/cierre_casa.csv` ya tiene una fila para ese mes — si la tiene, avisá que este cierre la va a recalcular.

2. **Ingresos del mes** (`config/ingresos.csv`): si el mes ya tiene fila, mostrala y preguntá si cambió algo. Si no tiene fila, proponé como default repetir los valores del último mes cargado y pedí confirmación o los montos reales de `ingreso_lu` / `ingreso_joaco`. Actualizá el CSV.

3. **Aportes de Lu.** Primero mirá qué ya entró solo: los de la pestaña *Aportes* de la planilla "Carga manual" (`data/staging/aportes_lu_carga.csv`) y los gastos de casa que pagó ella (`Lu tarjeta` / `Lu efectivo`, de la pestaña *Gastos* o de `gastos.py pago-lu`): `cierre_casa.py` los suma solo y los lista como "planilla" y "pagado por Lu". Preguntale a Joaco solo por lo que falte (las transferencias entrantes no generan mail) y cargalo con `gastos.py aporte --quien lu --monto M --mes AAAA-MM` (o `--fecha F`): rechaza un aporte repetido del mismo mes y monto (SM12), `--forzar` si de verdad son dos. **No** agregues filas a `data/aportes_lu.csv` ni a `config/cierre_exclusiones.csv` a mano.

4. **Extractos de débito** (`data/estados/`, export CSV del web de Santander: Santander UYU 9071, Cuenta Casa 8261, Santander USD). Pedile a Joaco que los suelte ahí si no están. Corré `C:/Users/joaco/anaconda3/python.exe scripts/conciliar_debito.py` (dry-run) y leé la salida. Reporta:
   - **Créditos solo en el extracto = INGRESOS** (sueldo, transferencias recibidas: no generan mail, el ledger está ciego a ellos). Presentáselos a Joaco para clasificar: `Sueldo` / `Otros ingresos` / `Devoluciones & cashback`, o transferencia interna entre cuentas propias (→ `en_analisis=0`).
   - **Débitos solo en el extracto**: gastos sin aviso (comisiones, débitos automáticos).
   - **Filas del ledger sin respaldo**: sospechosas (movimiento que el banco no tiene) — preguntá la causa, NUNCA borrar a ciegas.
   - **Saldo de cierre** de cada cuenta.
   Después corré `conciliar_debito.py --insertar` y aplicá las clasificaciones de Joaco con `gastos.py resolver <id> ... --cerrar ingreso_a_clasificar --nota "..."` (los ingresos entran con ese pendiente). Una fila insertada que resultó duplicada se da de baja con `gastos.py borrar <id> --motivo "..."`, nunca editando el ledger.

5. **Saldos reales por cuenta**: los saldos de las cuentas con extracto adjunto **salen del propio extracto** (los reporta el paso 4 — no se los pidas a Joaco). Pedile solo los que faltan: tarjetas de crédito (si no adjuntó el PDF), Mercado Pago, Splitwise, efectivo. Escribí `data/staging/saldos_reales.csv` (`cuenta,moneda,saldo,fecha_corte`), sobrescribiendo el anterior. **`fecha_corte` = el día del que son los saldos** (normalmente hoy, si los extractos se exportaron hoy y los saldos se preguntaron hoy) — una sola fecha para todo el archivo. Si un extracto es de otro día, pedile a Joaco el saldo actual de esa cuenta en vez de usar el del extracto: mezclar cosechas inventa diferencias falsas.

6. **Estados de cuenta de crédito (PDF)**: si hay PDFs nuevos en `data/estados/`, corré `C:/Users/joaco/anaconda3/python.exe scripts/conciliar_estado.py <pdf1> <pdf2>` (SIEMPRE con los PDFs explícitos, ver la chuleta) y leé su salida. Recordá que **BBVA Crédito NO manda avisos por transacción**: sus consumos solo entran por acá. **La Santander Mastercard (casa) se dio de baja el 27/9/2026**: su ÚLTIMO estado (llega ~5-8/10, con la cuota anual 1.602 + IVA del 4/9 y lo que se haya cargado después) todavía hay que conciliarlo; si trae un reintegro de la cuota anual, entra como gasto negativo de `Servicios`. Después de ese, no hay más.

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

**Toda escritura manual pasa por `scripts/gastos.py`** (Fase 1, 27/9/2026): altas (`gasto`, `pago-lu`, `aporte`, `transferencia`, `cobro`), splits (`split`), correcciones (`resolver`) y bajas (`borrar`). Todo en seco por defecto; `--aplicar` escribe y corre la verificación. Si el ledger se rompe o se pierde: `scripts/reconstruir_ledger.py` lo arma de nuevo desde `data/fuentes/`, los staging e `inbox_raw/` (idéntico, probado en el test).

**Archivos y cuentas.** Joaco deja los exports del web de Santander en `data/estados/`. Cada cuenta se identifica por **número** (`config/cuentas_bancarias.csv`): `000001322680` Cuenta Casa · `001203500692` caja de ahorro $ · `005203662400` caja de ahorro USD · `800944254160` Mastercard (PDF) · `770054522300` Visa Platinum (PDF). Si aparece un número nuevo, el guard corta: mapearlo ahí antes de seguir. Ojo con los formatos de monto: hay exports con punto decimal y otros con coma.

**Calendario de estados de crédito.** BBVA: PDF por mail, día 1-7. Santander Mastercard (casa): día ~5-8 — **dada de baja el 27/9/2026, el de octubre es el último**. Visa Platinum: cierra ~28 y llega a fin de mes. El PDF de Santander **no dice la marca** — solo el número.

**SPLITS = falsos positivos garantizados en la conciliación.** El banco trae UNA línea por el total; el ledger lo tiene partido (fila propia + hermana a Deudas), así que el matcher nunca los aparea: la línea del banco aparece como "débito nuevo" y las filas del split como "sin respaldo". Pasó 3 veces en julio (Movistar 1.750,82 = 875,41×2 · Devoto 275,41 = 46,25+229,16 · Desborde 1.785,20 = 892,60×2). **Antes de insertar un débito "nuevo", verificar si es la suma de un split existente.** Si el duplicado ya se insertó, `en_analisis=0` NO alcanza — la conciliación de saldos cuenta movimientos reales: hay que darla de baja con `gastos.py borrar <id> --motivo "total del split <id>"`.

**Semillas y fines de semana.** El banco postea sábado/domingo con fecha del lunes. Toda semilla/foto de saldo debe ser la posición **al CIERRE del día** del corte, no una lectura de mediodía (mordió dos veces: "Pizza" 466,66 y KINKO 203,61).

**Patrones USD.** Pagos de tarjeta vía Banred: `TCSANTOL` / `TCBBVAVI` → transferencia interna a la tarjeta, no gasto. `PAHORRO` en ambas cuentas = compra de dólares (dos patas: −UYU y +USD, ambas internas, `en_analisis=0`). El banco factura **~3% de recargo** sobre consumos en moneda extranjera: el importe del estado manda sobre el del aviso (el matcher tolera 6% en no-UYU). Splitwise a veces salda con centavos de diferencia (328,80 vs 328,00) → registrar la comisión para que ambos lados cierren.

**Dos cuentas virtuales de cobranzas, no confundirlas.** `Deudas` = SOLO Splitwise (se concilia contra su API en cada corrida; meter ahí una cobranza ajena rompe ese chequeo). `A cobrar` = plata que le deben a Joaco por fuera de Splitwise (creada 3/8/2026 con el choque del auto: pagó 18.000 de arreglo, 12.600 se los reintegra el tercero). Cuando llegue un reintegro de `A cobrar`: `gastos.py cobro --monto M --fecha F --a "<cuenta donde entró>" --desc "..."`.

**Gastos fijos → `config/gastos_fijos.csv`** (mapeado con Joaco el 9/8/2026). Es la fuente única de qué se repite, con qué frecuencia, en qué cuenta y cuántas cuotas quedan. **Al cerrar el mes, recorrelo y verificá que cada fijo con su frecuencia esté en el ledger** — así se cazan los que faltan (en julio 2026 faltó ANTEL, ~2.103, y nadie lo notó). Actualizalo cuando cambie un importe o se termine una serie de cuotas.

Series de cuotas vivas a ago-2026: **ORT** cuota 18/24 en agosto → quedan 6 (hasta feb-2027) · **contribución inmobiliaria** queda solo la de setiembre · **BSE seguro del auto** 10 cuotas, van 2, la cuota baja a ~2.000 con el auto nuevo.

**"INTERNET/Proyecto PAS" no dice qué se pagó.** Es la app de pagos del Santander: UTE, ANTEL, IMM y Movistar llegan todos con ese mismo nombre. Al resolverlos, poné en `descripcion` de qué factura se trata (vía `resoluciones.csv`) — si no, los fijos se vuelven invisibles en cualquier análisis por comercio.

**Carpeta por mes: `data/estados/<MM-AA>/`** (desde setiembre 2026, ej. `09-26/`). Joaco deja ahí los extractos, los PDF bajados a mano y los exports de Splitwise del mes que se cierra. Los PDF que baja el pipeline desde el mail siguen cayendo en `data/estados/` raíz: al cerrar, moverlos a la carpeta del mes con `git mv`. Los scripts toman rutas explícitas: `conciliar_debito.py 09-26/caja_ahorro.csv` (relativa a `data/estados/`) y `conciliar_estado.py data/estados/09-26/x.pdf` (relativa al repo).

**Splitwise en el cierre (la API está cerrada desde el 1/9/2026).** Pedirle a Joaco que exporte desde la **web** (no la app) cada grupo con movimientos en el mes: grupo → ajustes → *Export as spreadsheet*, a `data/estados/<MM-AA>/splitwise/` sin renombrar (el nombre trae el grupo y la fecha). Después correr `sync_splitwise.py --fuente export` y leer la salida:
- **Paridad verificada el 19/9** contra los 43 gastos que había traído la API: montos, lo que pagó y lo que le toca, 43/43 idénticos. La fecha del export es la local; la de la API era UTC (un día después en gastos de la noche). El export es el correcto.
- **Reorganización entre grupos** (18/9, viaje a Colonia: las deudas de Guasones se saldaron con pagos ficticios y se recrearon como "Colonia" en Caqui Tapa): el script las detecta — un gasto y un pago en grupos distintos, mismo día y monto, que se anulan en el neto de Joaco — y no las inserta. Si alguna se escapa, se ve como un ingreso o una transferencia que el banco no tiene.
- `splitwise_revisar`: gastos con varios pagadores (el export trae solo el neto por persona, no se puede separar) o anteriores al último sync sin equivalente en el caché. Se cargan a mano.
- **El saldo de referencia es PARCIAL**: solo los grupos exportados. Faltan los gastos sueltos con amigos y los grupos no exportados (Casita con Lu, Baby Shower). Para cerrar `Deudas` contra algo real, exportar TODOS los grupos con saldo y los gastos sueltos. Además, los splits que pagó Joaco (`split_a_cargar`) no mueven `Deudas` hasta que se cargan: la diferencia se achica a medida que se resuelven.

**`conciliar_estado.py` SIEMPRE con los PDFs explícitos** (`conciliar_estado.py <pdf1> <pdf2>`). Sin argumentos barre todo `data/estados/*.pdf` que no figure en `data/staging/estados_procesados.csv`, y ese log NO tenía los PDFs de calibración de may-jul (en julio se procesaron pasándolos a mano). El 15/9 reinsertó 53 filas sobre meses ya cerrados — una con id repetido, porque `EC_0726_BBVA.pdf` y `2026-07-07_bbva_EC_Visa_.pdf` son el mismo documento — y hubo que borrarlas. Los viejos ya quedaron logueados, pero la regla sigue.

**PDF bajado del portal ≠ PDF del mail.** El del portal tiene otro layout: el detector busca "SU ESTADO DE CUENTA" y el extractor no encuentra el período. Esperar el mail (llega igual, ~4-8 del mes) y mover el del portal a `data/estados/no_procesar/` para que no aborte la corrida.

**Líneas que el extractor Santander SALTEA: `I.V.A. 22%`.** Cuando el total declarado no cuadra con la suma de líneas (la Mastercard del 4/9 dio 352,44 = IVA sobre la cuota anual de 1.602), buscar IVA/intereses en el texto del PDF y cargar la fila con `gastos.py gasto --cuenta "<tarjeta>" --categoria Servicios ...`. Pendiente: arreglar el extractor (Fase 2).

**`sin_respaldo_estado` se cierra en el mismo cierre.** Cada estado deja pendientes por las filas del ledger que no apareó. Son casi siempre: (a) mitades de split, (b) avisos con importe distinto al del estado — Uber cierra con la tarifa final, los USD llevan ~3% de recargo — que se resuelven ajustando el monto del aviso al del estado (`gastos.py resolver <id> monto=... monto_uyu=... --cerrar sin_respaldo_estado`) y **dando de baja la fila que el estado insertó** (`gastos.py borrar`), (c) cargos posteriores al cierre del estado, que entran en el siguiente. `borrar` deja la fila completa en `data/fuentes/borrados.csv`: si te equivocaste, se recupera de ahí.

**Débitos: antes de `--insertar`, arreglar fechas y sacar los totales de splits.** Un aporte informado con otra fecha (59.000: 9/8 en el ledger, 3/8 en el banco) o un pago de Splitwise del fin de semana (sábado 1/8, posteado lunes 3/8) quedan fuera de la ventana del matcher y se proponen como nuevos: corregir `fecha` por resoluciones primero. Y los totales bancarios de un split (Alfredo 18.000 = 5.400 + 12.600; Movistar 1.847,21; Pantagruel 4.300) se insertan igual: `gastos.py borrar <id>` (cierra también sus pendientes).

**Tarjetas y hermanas.** Para una tarjeta de crédito, una transferencia que SALE (hermana → `Deudas`, Dilema → `A cobrar`) SUMA deuda igual que un gasto: original + hermana = cargo real. `conciliar_saldos` ya lo hace así; no "corregirlo".

**Semilla = `config/saldos_iniciales.csv`, UNA fecha para todas las cuentas.** Movida del 12/7 al **2026-08-31** en el cierre de agosto (el Platinum arrastraba 5.482,14 UYU / 131,80 USD del tramo 12/7-27/7, de una lectura manual marcada DISCREPANCIA; agosto cuadró al peso contra el estado). Al re-sembrar, NO volver a correr `conciliar_saldos` para la misma fecha: pisaría en `data/saldos.csv` el registro de las diferencias que la justificaron. `Deudas` quedó sembrada con el calculado (sin real: la API de Splitwise da 403 desde el 1/9). BBVA en pesos: el ledger no ve cómo se paga la tarjeta; el real lo da el estado (saldo a favor 1.021,96 al 31/8).

**Parte vs TOTAL — la trampa que más mordió.** Las filas `origen=planilla_casa` del ledger **ya son la parte de Joaco**; el egreso TOTAL de la casa vive en `data/casa.csv`. Para proyectar el gasto de la casa usar TOTALES (casa.csv hasta jun-2026 + ledger `ambito=casa` desde jul-2026). Nunca comparar una serie contra la otra: fabrica "saltos" ficticios (pasó con la patente y con los gastos comunes).
