# Sistema de gastos

Ledger único de gastos de Joaco: registro automático desde Gmail, división de gastos de casa con Lu por coeficiente de ingresos, conciliación mensual contra estados de cuenta, presupuesto y dashboard.

Diseño completo: [docs/esquema.md](docs/esquema.md) · Validación de la migración: [docs/validacion-migracion.md](docs/validacion-migracion.md)

## Estructura

```
config/               categorías, mapeos, cuentas, ingresos mensuales (coeficientes casa)
config/secrets.local.json   credenciales locales (gitignored — NO subir ni compartir)
data/ledger.csv       el libro único (todas las transacciones, histórico 2021-11 →)
data/casa.csv         planilla de casa histórica normalizada (congelada jul-2026)
data/presupuesto.csv  presupuestos mensuales históricos (2025-01 → 2026-07)
data/raw/             fuentes originales (MyFinance.db de 1Money, casa.xlsx, snapshots presupuestos)
data/staging/         salidas intermedias de los migradores
scripts/              migradores + merge_y_validar.py (regenera ledger, resumen y validación)
out/resumen_mensual.csv   agregado mes × categoría para el dashboard
```

## Regenerar todo

```
python scripts/migrar_1money.py
python scripts/migrar_planilla_casa.py
python scripts/migrar_presupuestos.py
python scripts/merge_y_validar.py
```

## Datos en Drive (alimentan Looker Studio)

Carpeta [Sistema Gastos](https://drive.google.com/drive/folders/1vAPsrCr8_QbHpG_zyj24Erl3jPMzW-ad):

- [gastos_resumen_mensual](https://docs.google.com/spreadsheets/d/10Tfth6Z7V0_W4WzVQJ-GCqro8HeSuV1y8Aj0nob4MDo) — mes × categoría × gasto (2021-11 → 2026-06), desde `out/resumen_mensual.csv`
- [gastos_presupuesto_vs_real](https://docs.google.com/spreadsheets/d/1c-kDv0qQAdkQg1Qg_WFtg6qM2qgKG3pXlmIg941z1qo) — mes × categoría × presupuesto/real/diferencia (2025-01 → 2026-07), desde `out/presupuesto_vs_real.csv`

## Estado

- [x] Fase 1 — migración del histórico + validación (cierra 0,00 contra fuentes)
- [x] Fase 1 — export a Google Sheets (falta conectar Looker Studio, 3 clicks del usuario)
- [ ] Fase 2 — pipeline de mails (avisos Santander 2×/día, bandeja de pendientes)
- [ ] Fase 3 — conciliación mensual (PDFs BBVA/Santander MC, cierre casa, Splitwise API)
- [ ] Fase 4 — presupuesto integrado + alertas

## Decisiones clave

- Cuenta casa: vuelve a Santander (débito **8261**) → los avisos de esa tarjeta son ámbito casa. Mastercard Santander = crédito de la casa. 9071 débito personal, 6427 = Platinum personal. Itaú cerrada.
- La app 1Money queda congelada (histórico migrado); la planilla de casa queda congelada como histórico.
- División casa: coeficientes por ingresos del mes en `config/ingresos.csv`; cierre mensual con una sola transferencia de ajuste.
- Anti doble conteo: columna `en_analisis` (reglas en esquema.md; excepción documentada: 2024-09 usa volcados 1Money).
