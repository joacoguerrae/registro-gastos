# Validación de la migración — ledger unificado

Generado por `scripts/merge_y_validar.py` el 2026-07-11. Todas las cifras de fuentes se recalcularon directamente desde `data/raw/MyFinance.db`, `data/raw/casa.xlsx` y `data/presupuesto.csv` (independiente de los reportes de los migradores).

## 1. Merge → data/ledger.csv

- Filas totales: **4525** (3060 de 1money + 1465 de planilla_casa)
- Por tipo: {'gasto': 3881, 'ingreso': 437, 'transferencia': 207}
- Rango de fechas: 2021-11-01 → 2026-07-08
- IDs únicos: sí
- Suma |monto_uyu| (todas las filas con valor): 15,851,119.03
- Celdas vacías fuera de regla del esquema: ninguna
- Gasto 2026 en análisis (en_analisis=1): **834,245.66 UYU**

## 2a. Conteos vs MyFinance.db

| Métrica | DB (crudo) | Ledger | Esperado | Resultado |
|---|---|---|---|---|
| Transacciones activas | 2853 | 2853 | 2853 | OK |
| Transferencias activas | 207 | 207 | 207 | OK |

Suma `monto_uyu` de transacciones 1money: ledger 12,175,952.36 vs DB 12,175,952.36 → diff 0.0000 (OK)

## 2b. Gastos por año (monto_uyu, en_analisis=1) — ledger vs fuentes

Regla en_analisis reconstruida desde cero sobre las fuentes crudas (DB: gastos con categoría, excluyendo cuenta/categoría Casa desde 2023-05; planilla: ventana A = Egresos×0.5 sin Origen 2023-05→2024-08, ventana B = Gasto Joaco>0 con Origen 'Debito Casa').

| Año | Ledger 1money | DB recalc | Δ | Ledger planilla | XLSX recalc | Δ | Total año |
|---|---|---|---|---|---|---|---|
| 2021 | 50,109.14 | 50,109.14 | 0.00 | 0.00 | 0.00 | 0.00 | 50,109.14 |
| 2022 | 358,189.06 | 358,189.06 | 0.00 | 0.00 | 0.00 | 0.00 | 358,189.06 |
| 2023 | 416,646.33 | 416,646.33 | 0.00 | 224,873.45 | 224,873.45 | 0.00 | 641,519.78 |
| 2024 | 418,340.38 | 418,340.38 | 0.00 | 280,601.45 | 280,601.45 | 0.00 | 698,941.83 |
| 2025 | 1,044,969.92 | 1,044,969.92 | 0.00 | 449,165.42 | 449,165.42 | 0.00 | 1,494,135.34 |
| 2026 | 567,525.16 | 567,525.16 | -0.00 | 266,720.50 | 266,720.50 | 0.00 | 834,245.66 |

Resultado: **OK — diferencias 0.00 en todos los años**

## 2c. Anti doble conteo casa — planilla (parte Joaco) vs volcados 1Money

Por mes desde 2023-05: parte Joaco según planilla (fuente canónica, en_analisis=1) vs transacciones de cuenta/categoría 'Casa' en 1Money (duplicado, en_analisis=0). Se esperan diferencias (Joaco no volcaba cada gasto idéntico); se reportan los meses con diferencia >15%. **Se documenta, no se corrige.**

| Mes | Planilla Joaco | 1Money Casa | Δ | Δ% | >15% |
|---|---|---|---|---|---|
| 2023-05 | 22,772.50 | 13,263.00 | 9,509.50 | 41.8% | SI |
| 2023-06 | 24,216.53 | 55,723.00 | -31,506.47 | 56.5% | SI |
| 2023-07 | 47,400.14 | 23,126.00 | 24,274.14 | 51.2% | SI |
| 2023-08 | 42,072.50 | 78,541.48 | -36,468.98 | 46.4% | SI |
| 2023-09 | 23,346.50 | 16,602.00 | 6,744.50 | 28.9% | SI |
| 2023-10 | 20,702.97 | 38,271.00 | -17,568.03 | 45.9% | SI |
| 2023-11 | 27,622.54 | 41,053.50 | -13,430.96 | 32.7% | SI |
| 2023-12 | 16,739.77 | 15,678.00 | 1,061.77 | 6.3% |  |
| 2024-01 | 22,670.02 | 16,570.00 | 6,100.02 | 26.9% | SI |
| 2024-02 | 22,942.00 | 19,881.13 | 3,060.87 | 13.3% |  |
| 2024-03 | 12,279.50 | 8,162.00 | 4,117.50 | 33.5% | SI |
| 2024-04 | 12,319.00 | 20,100.00 | -7,781.00 | 38.7% | SI |
| 2024-05 | 21,764.93 | 16,385.00 | 5,379.93 | 24.7% | SI |
| 2024-06 | 28,711.00 | 26,100.62 | 2,610.38 | 9.1% |  |
| 2024-07 | 21,727.00 | 19,782.00 | 1,945.00 | 9.0% |  |
| 2024-08 | 28,094.50 | 31,093.00 | -2,998.50 | 9.6% |  |
| 2024-10 | 29,828.50 | 26,674.00 | 3,154.50 | 10.6% |  |
| 2024-11 | 30,664.00 | 29,579.90 | 1,084.10 | 3.5% |  |
| 2024-12 | 49,601.00 | 53,206.97 | -3,605.97 | 6.8% |  |
| 2025-01 | 41,352.50 | 37,249.13 | 4,103.37 | 9.9% |  |
| 2025-02 | 37,807.00 | 35,213.00 | 2,594.00 | 6.9% |  |
| 2025-03 | 36,746.00 | 36,555.11 | 190.89 | 0.5% |  |
| 2025-04 | 34,948.68 | 33,652.32 | 1,296.36 | 3.7% |  |
| 2025-05 | 34,457.51 | 33,033.21 | 1,424.30 | 4.1% |  |
| 2025-06 | 34,675.59 | 29,328.00 | 5,347.59 | 15.4% | SI |
| 2025-07 | 30,920.98 | 37,806.82 | -6,885.84 | 18.2% | SI |
| 2025-08 | 35,061.21 | 32,845.00 | 2,216.21 | 6.3% |  |
| 2025-09 | 35,061.14 | 35,810.00 | -748.86 | 2.1% |  |
| 2025-10 | 39,011.62 | 32,168.00 | 6,843.62 | 17.5% | SI |
| 2025-11 | 38,318.66 | 33,061.00 | 5,257.66 | 13.7% |  |
| 2025-12 | 50,804.53 | 56,575.00 | -5,770.47 | 10.2% |  |
| 2026-01 | 50,654.75 | 49,614.00 | 1,040.75 | 2.1% |  |
| 2026-02 | 41,442.00 | 41,410.00 | 32.00 | 0.1% |  |
| 2026-03 | 46,240.10 | 43,408.00 | 2,832.10 | 6.1% |  |
| 2026-04 | 47,570.07 | 47,487.00 | 83.07 | 0.2% |  |
| 2026-05 | 29,744.89 | 29,746.00 | -1.11 | 0.0% |  |
| 2026-06 | 51,068.69 | 51,069.00 | -0.31 | 0.0% |  |

Meses con diferencia >15%: **14 de 37**.

Detalle de los desvíos:

- **2023-05**: planilla 22,772.50 vs 1Money 13,263.00 (41.8%)
- **2023-06**: planilla 24,216.53 vs 1Money 55,723.00 (56.5%)
- **2023-07**: planilla 47,400.14 vs 1Money 23,126.00 (51.2%)
- **2023-08**: planilla 42,072.50 vs 1Money 78,541.48 (46.4%)
- **2023-09**: planilla 23,346.50 vs 1Money 16,602.00 (28.9%)
- **2023-10**: planilla 20,702.97 vs 1Money 38,271.00 (45.9%)
- **2023-11**: planilla 27,622.54 vs 1Money 41,053.50 (32.7%)
- **2024-01**: planilla 22,670.02 vs 1Money 16,570.00 (26.9%)
- **2024-03**: planilla 12,279.50 vs 1Money 8,162.00 (33.5%)
- **2024-04**: planilla 12,319.00 vs 1Money 20,100.00 (38.7%)
- **2024-05**: planilla 21,764.93 vs 1Money 16,385.00 (24.7%)
- **2025-06**: planilla 34,675.59 vs 1Money 29,328.00 (15.4%)
- **2025-07**: planilla 30,920.98 vs 1Money 37,806.82 (18.2%)
- **2025-10**: planilla 39,011.62 vs 1Money 32,168.00 (17.5%)

Interpretación: los volcados a 1Money eran manuales y agregados (no gasto a gasto), por lo que el calce mensual exacto no era esperable. La fuente canónica en el ledger es la planilla; los registros 1Money quedan con en_analisis=0 y no se cuentan dos veces.

Además, 64 gastos de la planilla (suma 81,313.86 UYU) no caen en ninguna ventana y NO están en el ledger:

- A REVISAR: Debito Casa con Gasto Joaco vacio: 4 filas, 2,866.00 UYU
- A REVISAR: sin Origen fuera de ventana A: 31 filas, 44,002.00 UYU
- Debito Casa 100% Lu (gasto_joaco=0, por diseno): 22 filas, 26,907.86 UYU
- tricount (excluida por diseno): 7 filas, 7,538.00 UYU

Las exclusiones 'por diseño' son correctas (tricount tiene su reflejo en 1Money/Deudas; gasto_joaco=0 significa gasto 100% de Lu, no es parte de Joaco). Los casos 'A REVISAR' coinciden con lo reportado por el migrador (31 sin Origen fechados alrededor de 2024-09 + 4 Debito Casa con Gasto Joaco vacío). Decisión pendiente del usuario si deben incorporarse a mano.

## 2d. Categorías

- Filas gasto/ingreso con en_analisis=1 y categoría vacía: **0**
- Categorías del ledger fuera de `config/categorias.csv`: **ninguna**
- Resultado: OK

## 2e. Presupuesto (data/presupuesto.csv)

- Meses presentes: 19 (2025-01 → 2026-07) — esperado 19
- Filas totales: 490; filas por mes 2026: {'2026-01': 26, '2026-02': 26, '2026-03': 26, '2026-04': 26, '2026-05': 26, '2026-06': 26, '2026-07': 26}
- Controles (suma presupuesto / gasto_real_registrado):
  - 2025-01: 75680/111496 vs esperado 75680/111496 OK
  - 2025-06: 109413/91695 vs esperado 109413/91695 OK
  - 2025-12: 106801/148901 vs esperado 106801/148901 OK
  - 2026-06: 169835/262321 vs esperado 169835/262321 OK
  - 2026-07: 107745/0 vs esperado 107745/0 OK
- Resultado: OK

## 2f. Muestreo — 10 filas del ledger rastreadas a su fuente

| id | origen | fuente_id | fecha | tipo | monto | categoría | traza | detalle fuente |
|---|---|---|---|---|---|---|---|---|
| 5e0a0cb2c6a3 | planilla_casa | fila:1253 | 2025-07-28 | gasto | 1531.39 | Auto | OK | xlsx fila 1253: 2025-07-28 egreso=2800.0 gasto_joaco=1531.393506 origen=Debito Casa desc='Nafta' |
| e2dcae1b97b2 | 1money | c7ac1259-a23b-4307-a0ec-4857d856b04f | 2026-03-31 | gasto | 2389.00 | Casa & hogar | OK | DB: 2026-03-31 Expense uyu=2389.00 cat='Cosas Casa' acc='Casa' |
| f5d1bad6afed | 1money | 399ee3ec-14ed-4d14-9e85-636c5835c6e7 | 2023-06-30 | gasto | 109.00 | Super | OK | DB: 2023-06-30 Expense uyu=109.00 cat='Super' acc='$ Santander' |
| 7d432af0c39a | 1money | 20220537-a6e3-42cc-afa8-4aea5197b08a | 2026-01-19 | gasto | 4715.00 | Club | OK | DB: 2026-01-19 Expense uyu=4715.00 cat='Bigua' acc='$ Santander' |
| 3020fa9b183d | 1money | 73791372-cade-4b88-8e99-5173fb4e0db8 | 2025-10-15 | gasto | 311.00 | Comer afuera / Delivery | OK | DB: 2025-10-15 Expense uyu=311.00 cat='Pedir/Comer Afuera' acc='$ Santander' |
| e8427ced6875 | planilla_casa | fila:263 | 2023-12-04 | gasto | 268.50 | Super | OK | xlsx fila 263: 2023-12-04 egreso=537.0 gasto_joaco=None origen=(sin) desc='Macro' |
| 1da44b9971e9 | 1money | e854999e-1457-4881-9cc3-ccbd2763068c | 2025-05-02 | gasto | 83.09 | Suscripciones | OK | DB: 2025-05-02 Expense uyu=83.09 cat='Suscripciones' acc='USD Santander' |
| 23809b50f8fc | 1money | 91818e65-70e6-478f-a3c2-939aed3df1ed | 2025-08-04 | gasto | 651.00 | Casa & hogar | OK | DB: 2025-08-04 Expense uyu=651.00 cat='Casa' acc='$ Santander' |
| e24e0838e21d | planilla_casa | fila:278 | 2023-12-26 | gasto | 24.50 | Super | OK | xlsx fila 278: 2023-12-26 egreso=49.0 gasto_joaco=None origen=(sin) desc='Peya Super' |
| 541878df3408 | 1money | 3a462659-feb7-447f-af28-42605b0e44d9 | 2023-05-29 | gasto | 50.00 | Comer afuera / Delivery | OK | DB: 2023-05-29 Expense uyu=50.00 cat='Pedir/Comer Afuera' acc='$ Santander' |

Resultado: **10/10 OK**

## Resumen de resultados

| Validación | Estado | Detalle |
|---|---|---|
| 1.merge | OK | 4525 filas ({'1money': 3060, 'planilla_casa': 1465}), tipos {'gasto': 3881, 'ingreso': 437, 'transferencia': 207}, rango 2021-11-01 -> 2026-07-08, ids duplicados: ninguno |
| 1.vacios | OK | celdas vacias fuera de regla: 0 |
| 1.suma | OK | suma \|monto_uyu\| todas las filas: 15851119.03 |
| 2a.conteos | OK | DB: 2853 tx activas / 207 transfers; ledger 1money: 2853 tx / 207 transfers (esperado 2853/207) |
| 2a.suma_uyu | OK | suma monto_uyu transacciones 1money: ledger 12175952.36 vs DB 12175952.36 (diff 0.0000) |
| 2b.gastos_anio | OK | 2021: ledger 50109.14 (1money d=0.00, planilla d=0.00); 2022: ledger 358189.06 (1money d=0.00, planilla d=0.00); 2023: ledger 641519.78 (1money d=0.00, planilla d=0.00); 2024: ledger 698941.83 (1money d=0.00, planilla d=0.00); 2025: ledger 1494135.34 (1money d=0.00, planilla d=0.00); 2026: ledger 834245.66 (1money d=-0.00, planilla d=0.00) |
| 2c.doble_conteo | DESVIO | 37 meses comparados, 14 con diferencia >15%: 2023-05 (planilla 22772 vs 1money 13263, 42%), 2023-06 (planilla 24217 vs 1money 55723, 57%), 2023-07 (planilla 47400 vs 1money 23126, 51%), 2023-08 (planilla 42072 vs 1money 78541, 46%), 2023-09 (planilla 23346 vs 1money 16602, 29%), 2023-10 (planilla 20703 vs 1money 38271, 46%), 2023-11 (planilla 27623 vs 1money 41054, 33%), 2024-01 (planilla 22670 vs 1money 16570, 27%), 2024-03 (planilla 12280 vs 1money 8162, 34%), 2024-04 (planilla 12319 vs 1money 20100, 39%), 2024-05 (planilla 21765 vs 1money 16385, 25%), 2025-06 (planilla 34676 vs 1money 29328, 15%), 2025-07 (planilla 30921 vs 1money 37807, 18%), 2025-10 (planilla 39012 vs 1money 32168, 18%) |
| 2d.categorias | OK | gasto/ingreso en_analisis=1 sin categoria: 0; categorias fuera de catalogo: ninguna |
| 2e.presupuesto | OK | 19 meses (2025-01->2026-07), 490 filas, filas/mes 2026: {'2026-01': 26, '2026-02': 26, '2026-03': 26, '2026-04': 26, '2026-05': 26, '2026-06': 26, '2026-07': 26}; controles: 2025-01: 75680/111496 vs esperado 75680/111496 OK; 2025-06: 109413/91695 vs esperado 109413/91695 OK; 2025-12: 106801/148901 vs esperado 106801/148901 OK; 2026-06: 169835/262321 vs esperado 169835/262321 OK; 2026-07: 107745/0 vs esperado 107745/0 OK |
| 2f.muestreo | OK | 10/10 rastreadas: 10 OK |
| 2c.huerfanas | INFO | 64 gastos de la planilla fuera de toda ventana (no migrados al ledger), suma 81313.86 UYU -> A REVISAR: Debito Casa con Gasto Joaco vacio: 4 filas, 2866.00 UYU; A REVISAR: sin Origen fuera de ventana A: 31 filas, 44002.00 UYU; Debito Casa 100% Lu (gasto_joaco=0, por diseno): 22 filas, 26907.86 UYU; tricount (excluida por diseno): 7 filas, 7538.00 UYU |
| 4.resumen | OK | 628 filas (mes,categoria) -> out/resumen_mensual.csv; suma total 4077140.81 |
| total.2026 | INFO | gasto 2026 en_analisis=1: 834245.66 UYU |

## Pendientes / decisiones del usuario

1. Los 35 gastos de planilla 'a revisar' fuera de ventana (46,868.00 UYU) no están en el ledger: decidir si se agregan manualmente.
2. Delta interno del workbook casa.xlsx en saldo tarjeta (1521.03 vs 1552.03, 31.00): es interno de la planilla original, no de la migración.
3. Concepto de presupuesto sin mapeo: 'Ropa / Cosas pa Uno' (2025-12) → asignado a 'Otros'.
4. 14 meses con diferencia >15% entre planilla y volcados 1Money (ver 2c): esperado por volcados manuales; no requiere acción salvo que quiera auditarlos.
