# Esquema del sistema de gastos

Contrato de datos del ledger único. Toda pieza del sistema (migradores, parsers de mail, conciliación, export) escribe/lee según este documento.

## Convenciones generales

- CSV UTF-8, separador coma, decimales con punto, sin separador de miles, montos con 2 decimales.
- Fechas `YYYY-MM-DD`, meses `YYYY-MM`.
- Los montos son siempre **positivos**; el signo lo da `tipo`.
- Python: `C:\Users\joaco\anaconda3\python.exe` (sqlite3, pandas, openpyxl disponibles).

## data/ledger.csv — el libro único

| Columna | Valores | Notas |
|---|---|---|
| id | string | hash corto estable: sha1(origen + fuente_id)[:12] |
| fecha | YYYY-MM-DD | |
| mes | YYYY-MM | mes de la fecha |
| tipo | gasto \| ingreso \| transferencia | |
| monto | decimal > 0 | en `moneda` |
| moneda | UYU \| USD \| EUR | |
| monto_uyu | decimal | equivalente UYU. 1Money: `amountInDefaultCurrency/100`. Planilla: igual a monto (todo UYU). Transferencias no-UYU: vacío. |
| cuenta | string | nombre canónico según `config/cuentas.csv`; si no está en la tabla, usar el título original de 1Money |
| cuenta_destino | string | solo transferencias |
| categoria | string | según `config/categorias.csv`, vía `config/mapeo_categorias.csv`. Transferencias y ajustes: vacío |
| subcategoria | string | categoría/concepto ORIGINAL (preserva el detalle histórico) |
| comercio | string | vacío en migración (lo llenan los parsers de mail) |
| descripcion | string | comment (1Money) / Descripcion (planilla) |
| ambito | personal \| casa \| split \| interno | ver reglas abajo |
| origen | 1money \| planilla_casa \| mail \| splitwise \| mp \| manual \| estado_cuenta | `estado_cuenta`: línea de un estado de cuenta PDF (BBVA, Santander Mastercard) sin aviso de mail que la respalde — ver docs/fase3.md §2 |
| estado | confirmado \| pendiente \| conciliado | migración → confirmado |
| en_analisis | 1 \| 0 | 0 = se conserva para auditoría pero NO cuenta en análisis de gasto (evita doble conteo). Reglas abajo. |
| fuente_id | string | uid de 1Money / `fila:N` de planilla / message-id de mail |

### Reglas de ámbito

- 1Money cuenta "Casa" → `casa`. Cuenta "Deudas" → `split`. Resto → `personal`.
- 1Money categoría "Gasto de Tercerl" → `split`.
- Planilla casa → `casa`.
- Transferencias → `interno`.
- Ajustes de saldo de 1Money (transacciones sin categoría) → `interno`, categoria vacía, subcategoria `Ajuste saldo`, en_analisis=0.
- **Fase 2 (mails)**: avisos de tarjeta **8261** → `casa` (cuenta casa de vuelta en Santander, decisión 2026-07); **Mastercard Santander** (estado de cuenta) → `casa`; 9071 y 6427 → `personal` salvo reclasificación en la revisión semanal. Transferencias con destino "Joaco Mercado Pago" o concepto "Casa" → `interno` (aportes, no gasto).

### División de gastos de casa (reemplaza a la planilla desde jul-2026)

La planilla "Gastos Vazquez Ledesma" queda **congelada como histórico** (migrada a data/casa.csv). Desde jul-2026 la división vive en el ledger:

- Las filas `ambito=casa` se dividen con los coeficientes del mes: `config/ingresos.csv` (mes, ingreso_lu, ingreso_joaco; coef = ingreso/(suma)). Si un mes no está cargado, se usan los valores del último mes disponible y el cierre lo marca como "a confirmar".
- Excepciones de participación: `data/participaciones.csv` (`id_ledger,frac_lu,frac_joaco,nota`) fija fracciones por fila cuando la revisión lo decide (ej. Paladar 2/3 Lu por ser con amigas de ella); el resto usa el coeficiente del mes. La leen cierre_casa.py y generar_dashboard_data.py.
- Retiros personales del fondo: gastos ámbito personal/split o préstamos a Deudas pagados desde cuentas de la casa **restan** del aporte de Joaco en el cierre.
- Las resoluciones (data/resoluciones.csv) son la única vía de corrección manual y se re-aplican en TODA reconstrucción del ledger (procesar_mails y merge_y_validar).
- **Gastos ámbito=casa pagados desde cuentas/tarjetas personales de Joaco** (ej. seguro del auto en la Platinum): cuentan doble en el cierre — como gasto de casa (se divide por coeficiente) y como aporte implícito de Joaco por el total que pagó.
- **Cierre mensual (cuenta continua, SIN transferencia de ajuste)**: la cuenta casa es continua; nadie le transfiere al otro. El cierre calcula la posición acumulada de cada uno (aportes acumulados − parte acumulada de los gastos) y sugiere cuánto debería aportar cada uno el mes siguiente para converger a cero. Posición semilla al 2026-06-30 (de la planilla congelada): Lu **+11.244,07** / Joaco **−9.692,04**. Aportes de Joaco = transferencias a Cuenta Casa (capturadas por mail/ledger) + gastos casa pagados desde sus cuentas personales (aporte implícito). Aportes de Lu = invisibles para el pipeline (transferencias entrantes no generan mail) → input manual en el cierre. Detalle operativo en docs/fase3.md.

## config/ingresos.csv

`mes,ingreso_lu,ingreso_joaco` — histórico sembrado desde la hoja "Ingresos Mensuales" de la planilla. Se agrega una fila por mes (input manual mínimo del sistema; el cierre mensual la pide y por defecto repite el mes anterior).

### Reglas anti doble conteo (en_analisis) — CASA

La planilla de casa y 1Money registran lo mismo dos veces (Joaco volcaba su parte a la app). Fuente canónica por ventana:

| Ventana | Fuente canónica (en_analisis=1) | Duplicado (en_analisis=0) |
|---|---|---|
| antes de 2023-05 | 1Money (categoría "Casa" y cuenta Casa) | — (la planilla no existía) |
| 2023-05 → 2024-08 | planilla: filas de gasto con parte Joaco = `Egresos * 0.5` (era 50/50, sin checkboxes) | 1Money: toda transacción de cuenta "Casa" o categoría "Casa" en esa ventana |
| 2024-09 → hoy | planilla: filas con `Origen = "Debito Casa"`, parte Joaco = columna `Gasto Joaco` (>0) | 1Money: toda transacción de cuenta "Casa" o categoría "Casa" en esa ventana |

- Filas "Aporte" de la planilla (columna Ingresos > 0): NO se migran al ledger — su reflejo canónico son las transferencias de 1Money ("$ Santander → Casa"). Sí van a `data/casa.csv`.
- Filas "Cierre"/"Ajuste" de la planilla: NO van al ledger; sí a `data/casa.csv`.
- 1Money cuenta "Deudas": en_analisis=1 (es el único registro del detalle de splits hasta que llegue la API de Splitwise).

## data/casa.csv — planilla de casa normalizada (completa, ambas partes)

Para el dashboard de casa (vista compartida Lu/Joaco). Una fila por fila original de la "Planilla de Gastos":

`fila,fecha,mes,descripcion,concepto,categoria,tipo_concepto,egreso,ingreso,lu,joaco,origen_planilla,coef_lu,coef_joaco,gasto_lu,gasto_joaco,saldo_tarjeta,era`

- `era`: `pre-sistema` (sin Origen), `tricount`, `50-50` (2024-09→2025-03), `proporcional` (2025-04→).
- `categoria` = concepto mapeado a categoría nueva. Filas sin fecha (aportes): usar fecha de la fila de gasto pareada si es evidente; si no, dejar fecha vacía y mes vacío.
- Montos tal cual la planilla (UYU).

## data/presupuesto.csv — histórico de presupuestos

Una fila por (mes, concepto) de los 19 archivos de Drive (2025-01 → 2026-07):

`mes,concepto_original,categoria,tipo,presupuesto,gasto_real_registrado,comentario`

- Excluir filas de total (se reconocen por posición/ausencia de concepto), "Sin Pago Auto" y celdas auxiliares sueltas.
- `gasto_real_registrado` es el que él tipeó en su momento (referencia histórica); el gasto real vivo se calculará desde el ledger.
- Mapear concepto → categoría nueva con el mapeo; conservar `concepto_original`.

## Cuentas, categorías y mapeo

- `config/cuentas.csv`: cuentas canónicas y mapeo de tarjetas (últimos 4 dígitos).
- `config/categorias.csv`: catálogo nuevo (17 de gasto + 3 de ingreso) con tipo Fijo/Variable/Ingreso.
- `config/mapeo_categorias.csv`: categoría/concepto original → categoría nueva. Matching case-insensitive y sin espacios sobrantes. Si un original no está en el mapeo: categoria = "Otros" y registrarlo en el reporte de validación.

## Notas por fuente (para los migradores)

### 1Money (data/raw/MyFinance.db, SQLite)

- Montos INTEGER en centésimos: valor real = valor/100.
- Filtrar SIEMPRE `isRemoved=0` (transaction, transfer, account, category, sync_link).
- `transaction` no tiene FK de cuenta/categoría: unir vía `sync_link` (`entityType='Transaction'`, `otherType='Account'` / `'Category'`). Transferencias: `entityType='Transfer'`, `otherType='FromAccount'` / `'ToAccount'`.
- Monto y moneda de una transacción: si `amountInAccountCurrency` no es NULL usar ese + `accountCurrencyCode`; si no, `amountInDefaultCurrency` + UYU. `monto_uyu` = `amountInDefaultCurrency/100` siempre.
- Transferencias: monto = `fromAmount/100` en `fromCurrencyCode`; si `toCurrencyCode` difiere, anotar en descripcion "→ {toAmount/100} {toCurrencyCode}".
- 218 transacciones activas sin categoría = ajustes de saldo (ver reglas de ámbito).
- `type`: 'Expense' → gasto, 'Income' → ingreso.

### Planilla casa (data/raw/casa.xlsx, hoja "Planilla de Gastos")

- Header fila 1; datos filas 2-1924. Columnas: A Descripcion, B Egresos, C Ingresos, D Lu (bool), E Joaco (bool), F Concepto, G Tipo, H Fecha, I Origen, J Valor Absoluto, K Mes-Año, L first_day, M coef Lu, N coef Joaco, O Saldo Tarjeta, P Gasto Lu, Q Gasto Joaco, R dist meses.
- openpyxl/pandas leen valores cacheados de las fórmulas (no recalcular).
- Filas sin fecha: K/L dan basura ("1899-12"); tratarlas según esquema.
- Checkboxes: TRUE/TRUE dividido por coeficiente; TRUE/FALSE 100% Lu; FALSE/TRUE 100% Joaco; FALSE/FALSE era pre-sistema.

### Presupuestos (Google Drive, 19 spreadsheets "YYYY-MM")

- `read_file_content` solo devuelve el primer tab: usar **`get_file_metadata`**, cuyo `contentSnippet` trae ambos tabs como CSV.
- En ese CSV las celdas vacías SE OMITEN (no queda coma vacía) y "Variacion" usa coma decimal (queda partida en dos campos): parsear con cuidado, validar que Presupuesto/Gasto sean numéricos.
- Números uruguayos: punto = miles ("22.400" = 22400).
- La fila de total es la última y no tiene etiqueta. "Pago Auto" 2025-01→07 era en USD (comentario). Desde 2025-07 hay bloque lateral fijo mezclado en el CSV ("Oportunidad de Ajustar") — ignorarlo.
