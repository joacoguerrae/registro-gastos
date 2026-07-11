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

## Estado

- [x] Fase 1 — migración del histórico + validación (cierra 0,00 contra fuentes)
- [ ] Fase 1 — export a Google Sheets + dashboard Looker Studio
- [ ] Fase 2 — pipeline de mails (avisos Santander 2×/día, bandeja de pendientes)
- [ ] Fase 3 — conciliación mensual (PDFs BBVA/Santander MC, cierre casa, Splitwise API)
- [ ] Fase 4 — presupuesto integrado + alertas

## Decisiones clave

- Cuenta casa: vuelve a Santander (débito **8261**) → los avisos de esa tarjeta son ámbito casa. Mastercard Santander = crédito de la casa. 9071 débito personal, 6427 = Platinum personal. Itaú cerrada.
- La app 1Money queda congelada (histórico migrado); la planilla de casa queda congelada como histórico.
- División casa: coeficientes por ingresos del mes en `config/ingresos.csv`; cierre mensual con una sola transferencia de ajuste.
- Anti doble conteo: columna `en_analisis` (reglas en esquema.md; excepción documentada: 2024-09 usa volcados 1Money).
