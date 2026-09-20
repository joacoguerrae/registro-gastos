# Sistema de gastos

Ledger único de gastos de Joaco: registro automático desde Gmail, división de gastos de casa con Lu por coeficiente de ingresos, conciliación mensual contra estados de cuenta, presupuesto y dashboard.

Diseño completo: [docs/esquema.md](docs/esquema.md) · Validación de la migración: [docs/validacion-migracion.md](docs/validacion-migracion.md)

## Estructura

```
config/               categorías, mapeos, cuentas, ingresos mensuales (coeficientes casa)
config/secrets.local.json   credenciales locales (gitignored — NO subir ni compartir; Splitwise API key + contraseña CI de los PDF)
config/posicion_casa.json   posición semilla de la cuenta continua de casa al 2026-06-30 (no se edita más)
config/saldos_iniciales.csv  saldo semilla por cuenta al corte de calibración (2026-07-12)
data/ledger.csv       el libro único (todas las transacciones, histórico 2021-11 →)
data/casa.csv         planilla de casa histórica normalizada (congelada jul-2026)
data/presupuesto.csv  presupuestos mensuales históricos (2025-01 → 2026-07)
data/splitwise.csv    crudo por gasto traído de la API de Splitwise (Fase 3, solo lectura)
data/cierre_casa.csv  posición mensual de la cuenta continua de casa (Fase 3, ver scripts/cierre_casa.py)
data/aportes_lu.csv   aportes de Lu a la cuenta casa (input manual — las transferencias entrantes no generan mail)
data/saldos.csv       conciliación mensual de saldos por cuenta (calculado vs real, Fase 3 §4)
data/estados/          PDF de estados de cuenta (BBVA, Santander Mastercard) que Joaco suelta a mano — ver más abajo
data/raw/             fuentes originales (MyFinance.db de 1Money, casa.xlsx, snapshots presupuestos)
data/staging/         salidas intermedias de los migradores
scripts/              migradores + merge_y_validar.py (regenera ledger, resumen y validación)
out/resumen_mensual.csv   agregado mes × categoría para el dashboard
```

## data/estados/ — estados de cuenta PDF

El conector de Gmail no baja adjuntos, así que los PDF de estados de cuenta (BBVA día 1-7, Santander Mastercard día 5-8) hay que soltarlos a mano en `data/estados/` apenas lleguen (nombre de archivo libre; el parser identifica el emisor por contenido). Después corré:

```
python scripts/conciliar_estado.py           # procesa todos los PDF nuevos de la carpeta
python scripts/conciliar_estado.py archivo.pdf  # o uno puntual
```

Concilia cada línea del estado contra el ledger (monto ±0,5%, fecha ±3 días): matchea → `estado=conciliado` y corrige `monto_uyu` con el TC real; sin match → inserta la línea (`origen=estado_cuenta`); fila del ledger sin línea del estado → pendiente `sin_respaldo_estado`. Cierra al peso contra el total del período. Hoy (2026-07-12) los extractores de BBVA y Santander Mastercard son *stubs* — el motor de matching (`conciliar()`, sin dependencia de PDF) ya está testeado con un estado sintético (`tests/test_conciliar_estado.py`); falta calibrar el parser de texto con el primer PDF real que Joaco suelte.

## /cierre-mes — ceremonia mensual

Orquesta el cierre de casa y la conciliación de saldos (`.claude/commands/cierre-mes.md`): pide ingresos del mes y aportes de Lu, pregunta los saldos reales por cuenta, corre `conciliar_estado.py` si hay PDF nuevos, `cierre_casa.py` y `conciliar_saldos.py`, y presenta el reporte (posición de casa, presupuesto vs real, saldos con diferencia, pendientes abiertos) antes de commitear. Se invoca escribiendo `/cierre-mes` al final de cada mes.

## Regenerar todo

```
python scripts/migrar_1money.py
python scripts/migrar_planilla_casa.py
python scripts/migrar_presupuestos.py
python scripts/merge_y_validar.py
```

## Datos en Drive (alimentan Looker Studio)

Carpeta [Sistema Gastos](https://drive.google.com/drive/folders/1vAPsrCr8_QbHpG_zyj24Erl3jPMzW-ad):

- [gastos_resumen_mensual](https://docs.google.com/spreadsheets/d/10Tfth6Z7V0_W4WzVQJ-GCqro8HeSuV1y8Aj0nob4MDo) — mes × categoría × gasto, desde `out/resumen_mensual.csv`
- [gastos_presupuesto_vs_real](https://docs.google.com/spreadsheets/d/1c-kDv0qQAdkQg1Qg_WFtg6qM2qgKG3pXlmIg941z1qo) — mes × categoría × presupuesto/real/diferencia, desde `out/presupuesto_vs_real.csv`
- [gastos_casa_mensual](https://docs.google.com/spreadsheets/d/15YU1L7MFnqbYCkkP8IT0o2e8v7yTPJu8ADvbxHS6cPA) — gastos de la CASA por mes × categoría con la parte de Lu y de Joaco, desde `out/casa_mensual.csv`

Los 3 Sheets se **refrescan solos en cada corrida** del pipeline (`scripts/actualizar_sheets.py`), escribiendo sobre los mismos archivos por su ID (no se recrean → la conexión de Looker no se rompe). Auth: cuenta de servicio en `config/gcp_service_account.json` (gitignored), con los 3 Sheets compartidos como Editor con su `client_email`.

## Estado

- [x] Fase 1 — migración del histórico + validación (cierra 0,00 contra fuentes)
- [x] Fase 1 — export a Google Sheets + Looker Studio
- [x] Fase 2 — pipeline de mails (parsers + backfill julio verificado 1:1 vs Gmail, ver docs/validacion-fase2.md; comandos /sync-gastos y /revisar-pendientes)
- [x] Fase 2.5 — **refresh automático de los Sheets** vía cuenta de servicio (`scripts/actualizar_sheets.py`, integrado al pipeline).
- [~] **Migración a fetch determinístico + GitHub Actions** — `fetch_mails.py` (Gmail API, sin Claude) + `.github/workflows/sync.yml` corriendo 2×/día en la nube. Paridad validada 57/57 (ver docs/validacion-migracion.md). En **semana shadow** (Actions valida sin commitear, la PC sigue de dueña); al cutover se flipa a `live` y se deshabilitan las tareas de Windows. Plan: [docs/plan-migracion-fetch.md](docs/plan-migracion-fetch.md).
- [x] Fase 3 — Splitwise (cuenta Deudas, solo lectura, integrado a `scripts/correr_pipeline.cmd`), cierre continuo de casa (`scripts/cierre_casa.py`, `/cierre-mes`) y conciliación de saldos (`scripts/conciliar_saldos.py`) operativos. Motor de matching de estados de cuenta construido y testeado con estado sintético (`scripts/conciliar_estado.py` + `tests/test_conciliar_estado.py`); falta calibrar los extractores de BBVA y Santander Mastercard con el primer PDF real (ver `data/estados/`).
- [ ] Fase 4 — presupuesto integrado + alertas

## Decisiones clave

- Cuenta casa: vuelve a Santander (débito **8261**) → los avisos de esa tarjeta son ámbito casa. Mastercard Santander = crédito de la casa. 9071 débito personal, 6427 = Platinum personal. Itaú cerrada.
- La app 1Money queda congelada (histórico migrado); la planilla de casa queda congelada como histórico.
- División casa: coeficientes por ingresos del mes en `config/ingresos.csv`. La cuenta casa es CONTINUA: sin transferencias de ajuste entre Lu y Joaco — el cierre calcula la posición acumulada de cada uno y sugiere los aportes del mes siguiente para converger (semilla 2026-06-30 en `config/posicion_casa.json`).
- Anti doble conteo: columna `en_analisis` (reglas en esquema.md; excepción documentada: 2024-09 usa volcados 1Money).
