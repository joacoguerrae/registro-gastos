# Plan: app de carga para el celular (Joaco y Lu)

> **Estado: PROPUESTO, NO EJECUTADO** (documentado el 12/10/2026). Joaco decidió dejarlo escrito y no implementarlo todavía. Antes de arrancar: releer el relevamiento (pudo cambiar `leer_carga_manual.py`), confirmar si ya migró a Itaú (`aporte_joaco_desde`) y conseguir el mail de Lu para la allowlist.
>
> **Decisiones ya tomadas con Joaco (12/10):** Apps Script web app sobre la misma planilla · alcance solo cargar (sin ver estado ni corregir) · incluir *aclaraciones* de cargos que el banco ya vio · incluir reparto fijo (por ingresos / mitades / todo Lu / todo Joaco) · login con la cuenta Google de cada uno (Lu tiene Gmail) · cuenta de origen de los aportes de Joaco configurable (va a migrar a Itaú).
>
> Las referencias `archivo:línea` son del 12/10/2026 y pueden haberse corrido.
## Contexto

Joaco quiere que Lu y él puedan registrar desde el celular, en el momento, los movimientos de la casa que el flujo diario no captura bien: aportes a la Cuenta Casa, gastos de casa pagados con plata personal, gastos personales pagados con la cuenta casa, efectivo. Lo que dice: "no me parece tan simple entrar rápido con el celular" a la planilla actual.

Relevamiento (12/10/2026):
- La planilla "Carga manual" (Fase 1.3) **ya cubre** los movimientos que el banco no ve (Lu tarjeta/efectivo, Joaco efectivo, aportes). El problema ahí es ergonomía y que una fila inválida se rechaza en silencio (el pendiente `[planilla]` solo lo ve Joaco).
- Dos de las casuísticas que nombra Joaco ("pagué algo mío con la cuenta casa", "pagué algo de casa con mi tarjeta") **no son filas nuevas**: el cargo ya llega por mail (8261, Platinum, Santander UYU mandan aviso). Cargarlo en la planilla lo haría caer como duplicado. Lo que falta es una **aclaración** que reclasifique el cargo existente (hoy solo `gastos.py resolver` en la revisión semanal; tres de los pendientes de hoy eran exactamente eso).
- El **reparto fijo** (mitades, 100% Lu) no se puede expresar en la planilla; las 5 filas de Lu de setiembre dicen "para ambos" en la nota, señal de que quería decir eso.
- Joaco va a migrar su cuenta a Itaú: el origen de los aportes (`DESDE_JOACO` fijo en código) tiene que ser configurable.

Decisiones tomadas con Joaco: Apps Script web app sobre la misma planilla (la recomendación: sin servidor, sin secretos nuevos, sin tocar el scope readonly del fetch); alcance **solo cargar**; incluir aclaraciones; incluir reparto; login con cuenta Google de cada uno (Lu tiene Gmail); cuenta de origen de aportes configurable.

## Arquitectura

```
Celular → Apps Script (app_carga/Code.gs + Index.html, pegado a la planilla)
            ├─ lee pestaña oculta `Listas` (la escribe --preparar-planilla desde config/)
            ├─ appendRow → Gastos (+col `reparto`) · Aportes (+col `desde`) · Aclaraciones (nueva)
leer_carga_manual.py (2x/día, scope readonly, SIN cambios de permisos)
            ├─ data/fuentes/carga_manual.csv            snapshot (+1 col: reparto)
            ├─ data/staging/ledger_carga_manual.csv      como hoy (aportes de Joaco con cuenta=desde)
            ├─ data/staging/aclaraciones_carga.csv       NUEVO (id_ledger, campo, valor) regenerable
            ├─ data/staging/participaciones_carga.csv    NUEVO, mismo esquema que participaciones.csv
            └─ data/pendientes.csv                       3 motivos [planilla] nuevos
merge_y_validar.py::merge()  aplica aclaraciones_carga ANTES de resoluciones.csv (la resolución gana)
cierre_casa / dashboard / resumen  participaciones = staging ∪ participaciones.csv (gastos.py gana)
```

Invariantes respetados: `resoluciones.csv` y `participaciones.csv` siguen siendo exclusivos de `gastos.py`; la planilla produce solo staging regenerable (como `ledger_carga_manual.csv` hoy); `ledger.csv` sigue siendo artefacto de build y `reconstruir_ledger.py` no cambia (su paso 3 regenera los dos staging nuevos).

**Regla de precedencia:** resolución manual > aclaración de la planilla. Si dicen lo mismo → no-op silencioso; si difieren → pendiente `aclaracion_en_conflicto` sobre el id de la aclaración.

## Fase 1 — Parser + staging + tests (sin app)

### `scripts/leer_carga_manual.py`
- `SNAP_COLS` (:67): agregar `reparto` al final. `leer_snapshot` tolerante al snapshot viejo de 10 columnas (rellena `reparto=""`). Actualizar la copia de `SNAP_COLS` en `tests/test_carga_manual.py:12`.
- **No tocar la clave del id** (:298-302): `reparto`/`nota` quedan fuera; `desde` entra por `cuenta`, que ya está (cambiar el `desde` de un aporte = otra fila, correcto).
- `ENCABEZADOS` (:89): `Gastos` += `reparto`; `Aportes` += `desde`/`cuentaorigen` → `cuenta`; nueva `Aclaraciones`: `fecha, monto, moneda, cuenta, quefue/que → que, es/casapersonal/ambito → ambito, reparto, nota`. `OBLIGATORIOS["Aclaraciones"] = {fecha, monto, cuenta, ambito}`.
- `REPARTOS = {"": None, "poringresos": None, "mitades": 0.5, "todolu": 1.0, "todojoaco": 0.0}`; formato de fracciones como `gastos.py:449-457`.
- `DESDE_JOACO` (:107) → `parametros()["aporte_joaco_desde"]`; en Aportes de Joaco (:343-349) `cuenta = r["cuenta"] or default`, validada contra `config/cuentas.csv` (activa=si, moneda UYU, tipo banco) → `Invalida` si no. Lu con `desde` → `Invalida`.
- Gastos con `reparto`: solo si `ambito == "casa"` (si no `Invalida`) → fila en `participaciones_carga` con nota `planilla Gastos fila N: <reparto>`.
- `procesar(snap, hoy)` devuelve además `aclaraciones` y `participaciones`; `guardar()` escribe los dos staging ordenados (`(id_ledger, campo, id_aclaracion)` y `id_ledger`). `main()` gana `--hoy AAAA-MM-DD` (para tests).
- Nueva función pura `matchear_aclaracion(r, candidatos, hoy, tol_dias, dias_sin_match, resoluciones)`; candidatos = el `ledger` ya cargado en :275, filtrado a `tipo=gasto`.

```
cands  = cuenta igual, moneda igual, |monto - monto_fila| < 0.005 (monto ORIGINAL, nunca monto_uyu),
         |fecha - fecha_fila| <= tol_dias
activos = cands con en_analisis=1
sin activos:  si (hoy - fecha) >= dias_sin_match → pendiente aclaracion_sin_match
              (detalle nombra ids en_analisis=0 si los hay); si no, esperar en silencio (BBVA llega con el estado)
mejores = activos a distancia mínima en fecha
len > 1:      si alguno ya tiene resolución ambito == pedido → satisfecha; si no → pendiente aclaracion_ambigua (ids)
x = mejores[0]
ambito:       res = resoluciones[x.id].get("ambito")
              res None y x.ambito != es → override(x.id, "ambito", es);  res == es → satisfecha;
              res != es → pendiente aclaracion_en_conflicto
reparto:      si frac no None y (es or x.ambito) == "casa" → participaciones_carga(x.id, frac)
```
Casos borde: dos cargos idénticos el mismo día → ambiguo (se resuelve con `gastos.py resolver` sobre el cargo real y la aclaración queda satisfecha sola); cargo en `en_analisis=0` no es candidato; el override nunca toca `monto_uyu`; si una fila cambia de id por cambio de fuente, el staging se recalcula por contenido (ventaja sobre `resoluciones.csv`). Idempotente: `procesar` es puro dado `(snap, ledger, resoluciones, hoy)`; el único uso de `hoy` es el umbral de 45 días (monótono).

- Pendientes nuevos: `aclaracion_sin_match`, `aclaracion_ambigua`, `aclaracion_en_conflicto` (con prefijo `[planilla]`, regenerados como los existentes :404-420; cerrables con `gastos.py resolver <id> --cerrar <motivo>`, que ya acepta ids sin fila, `gastos.py:576-587`).

### `scripts/fuentes.py`
- `PARTICIPACIONES_CARGA` + `participaciones_vigentes() -> {id: (frac_lu, frac_joaco)}`: staging primero, `data/participaciones.csv` encima. Usarla en `cierre_casa.py:177-182`, `generar_dashboard_data.py:106-110`, `merge_y_validar.py:774-779`.

### `scripts/merge_y_validar.py`
- En `merge()`, justo antes del bloque de resoluciones (:177-189): leer `data/staging/aclaraciones_carga.csv` y aplicar `overrides[id][campo]=valor` (solo `campo in COLUMNS`); después el bloque existente de resoluciones pisa lo que corresponda. Log informativo `1.aclaraciones`.

### `scripts/gastos.py`
- `:728` default de `--desde` desde `aporte_joaco_desde` (las dos puertas coinciden).

### `config/parametros.json`
- `aporte_joaco_desde: "Santander UYU"` (cuando migre: alta `Itaú UYU` en `cuentas.csv` + cambiar la clave), `aclaracion_tol_dias: 3`, `aclaracion_dias_sin_match: 45`, `app_carga_mails: [joacoguerrae@gmail.com, <mail de Lu>]`. Cuentas para aclaraciones: derivar de `cuentas.csv` (activa=si, tipo ∈ {conjunta, banco, tarjeta_credito}, moneda UYU), no lista a mano.

### Tests (`tests/test_carga_manual.py`, patrón existente: copia en `tmp_path`, `--offline`, sin red)
Fixture `aclaracion_para(base)` toma un gasto real `origen=mail`, `cuenta=Cuenta Casa`, `ambito=casa`, `en_analisis=1` (como `planilla_para` :32-46).
1. aclaración cambia el ámbito del cargo y `cierre_casa` lo resta de `aportes_joaco`
2. sin match: a +10 días nada; a +46 → `aclaracion_sin_match` (`--hoy`)
3. empate → `aclaracion_ambigua`; con resolución sobre uno, desaparece
4. resolución previa distinta → ledger conserva la resolución + `aclaracion_en_conflicto`
5. idempotencia: dos corridas → staging y pendientes idénticos, `fecha_alta` conservada
6. `mitades` → `participaciones_carga` 0.5/0.5 y `cierre_casa` reparte así; `participaciones.csv` gana
7. `desde` configurable: default desde parámetros (con `Itaú UYU` dado de alta), explícito, inválido → `carga_manual_invalida`
8. snapshot viejo de 10 columnas se lee
9. `test_fuentes.py`: variante `test_rebuild_incluye_aclaraciones` como `test_rebuild_incluye_la_planilla` (:155-162)

**Gate:** `python -m pytest -q` verde y `leer_carga_manual.py --offline` sobre el snapshot actual deja `data/` sin diff.

## Fase 2 — Planilla: `--preparar-planilla`

En `preparar_planilla()` (:443-500): crear pestaña `Aclaraciones` si falta (encabezado `fecha | monto | moneda | cuenta | que fue | es | reparto | nota`); agregar `reparto` a Gastos y `desde` a Aportes **solo si faltan, al final del encabezado** (nunca mover ni pisar: `columnas()` matchea por nombre); desplegables nuevos en `listas` (:447-452); pestaña **`Listas`** oculta (`updateSheetProperties.hidden=true`), reescrita con `clear` + `update` como `Que Va` (:490-495): fila 1 = nombres (`categorias, quien_paga, moneda, ambito, aporte_quien, aporte_desde, aporte_desde_default, aclaracion_cuentas, reparto, mails`), valores hacia abajo. Fila nueva en `INSTRUCCIONES` para Aclaraciones.

**Gate:** correrlo dos veces (idempotente), ninguna fila cargada se movió, el paso online siguiente no cambia el ledger.

## Fase 3 — Apps Script (`app_carga/`)

Archivos: `Code.gs`, `Index.html`, `appsscript.json` (`webapp: {access: ANYONE, executeAs: USER_DEPLOYING}`, `timeZone: America/Montevideo`), `.clasp.json` (solo scriptId), `README.md`.

**Primer paso, antes de la UI:** `doGet` de prueba que muestre `Session.getActiveUser().getEmail()`. Con "ejecutar como Joaco" + cuentas @gmail.com puede venir vacío; si pasa, fallback a `executeAs: USER_ACCESSING` (Lu ya es editora de la planilla; cada uno autoriza una vez).

**Code.gs:** `doGet` (allowlist por `Listas.mails` → página "no autorizado" o `Index` con meta viewport); `getListas()` (lee `Listas` por nombre, cache 10 min); `getUsuario()`; `guardarGasto/guardarAporte/guardarAclaracion(d, forzar)`: revalidan en servidor (fecha ≤ hoy+1 y ≥ 2026, monto ≠ 0, obligatorios, valores dentro de listas), `chequearDuplicado_` (mismo monto ±3 días en la misma pestaña → `{dup, detalle}`; la UI pregunta "¿ya está cargado? Guardar igual"), `LockService`, `filaPorEncabezado_` (lee fila 1, normaliza como `norm()` de Python, arma el array y `appendRow`). **Fecha como `new Date(y, m-1, d)`** (pasa `DATE_IS_VALID`, respeta `dd/mm/yyyy`, `UNFORMATTED_VALUE` la devuelve como serial que `bajar()` ya convierte :259); monto como `Number`.

**Index.html:** una página, vanilla JS, `google.script.run`. Inicio con 3 botones grandes: "Pagué algo", "Aporté a la cuenta casa", "Aclaración de un cargo". Fecha hoy por defecto, monto `inputmode=decimal`, moneda UYU default, "quién" preseleccionado por mail (Joaco → `Joaco efectivo`/`Joaco`; Lu → `Lu tarjeta`/`Lu`), `desde` solo si quién=Joaco (default `aporte_desde_default`), reparto solo si es=casa, botón deshabilitado hasta validar, pantalla "Guardado" con resumen + "Cargar otro"/"Inicio", modo oscuro, botones ≥ 56 px, sin librerías.

**Deploy (recomendado clasp):** `npm i -g @google/clasp`, `clasp login` (token en `~/.clasprc.json`, fuera del repo), habilitar Apps Script API, `clasp clone <scriptId>` en `app_carga/`, `clasp push` + `clasp deploy -i <deploymentId>` (misma URL). Alternativa manual: pegar en Extensiones → Apps Script. Luego "Agregar a pantalla de inicio" en cada celular.

**Gate:** cargar un gasto de prueba desde el celular y verlo en la planilla con la fecha como fecha.

## Fase 4 — Docs

- `docs/esquema.md`: participaciones (+staging), precedencia resolución > aclaración, sección "Planilla Carga manual: pestañas/columnas/staging".
- `CLAUDE.md` (bullet de la planilla): una frase sobre `app_carga/`, la pestaña `Aclaraciones` y que `resoluciones.csv` gana.
- `.claude/commands/revisar-pendientes.md` paso 8: los 3 motivos nuevos y cómo se cierran.
- `app_carga/README.md`; tabla de estado de `docs/auditoria-2026-09.md`.

## Verificación end-to-end

1. Desde el celular, aclaración sobre un cargo reciente de la Cuenta Casa ("es: personal").
2. En la PC: `git pull`; `& "scripts\correr_pipeline.cmd"` (tool PowerShell, no Bash).
3. Salida de `leer_carga_manual`: "1 aclaración matcheada"; `data/staging/aclaraciones_carga.csv` tiene la fila; `data/ledger.csv` muestra `ambito=personal` en ese id; `cierre_casa` del mes lo resta de `aportes_joaco`.
4. `python -m pytest -q` verde. Segunda corrida: `git status` sin cambios nuevos.
5. Borrar la fila de prueba en la planilla (1 fila < `MAX_BAJA`).

## Riesgos
- Scope: el paso diario sigue `spreadsheets.readonly`; la app escribe con la identidad del deploy, no con el token del fetch.
- Guarda anti-vaciado (:526-531) cuenta filas del snapshot; la app nunca borra.
- `--preparar-planilla` solo agrega columnas al final y reescribe `Que Va`/`Listas`: nunca pisa filas cargadas.
- Identidad en Apps Script (ver Fase 3): validar primero.
- Dos cargos idénticos quedan fuera de la app a propósito (pendiente ambiguo + `gastos.py`).
