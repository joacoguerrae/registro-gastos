# Auditoría e2e — sistema de gastos (2026-07-16)

Auditoría completa del sistema el mismo día del **cutover** (Actions pasó a ser el único
dueño del pipeline, tareas de Windows deshabilitadas), antes de la primera corrida
automática sin red local. Alcance: pipeline, workflow de Actions, integridad de datos,
seguridad, configuración del repo, y costos/límites.

Este documento es el relevamiento y el plan. Las correcciones van por fases (ver el final),
cada una con su gate.

## Estado de las correcciones

| Fase | Cierra | Estado |
|---|---|---|
| 1 — Red de seguridad | A1, M5, B4 | ✅ **hecha** (2026-07-16, commit `9096f0c`) |
| 2 — Bombas de agosto | M1, M2, M3, A1 | ✅ **hecha** (2026-07-16, commit `f8d09a9`) |
| 3 — Documentos de proceso | A2, M6, B10 | ✅ **hecha** (2026-07-16) |
| 4 — Higiene de datos y deps | B1, B3, B5, B6, B9, B12, M4 | ✅ **hecha** (2026-07-16) |
| 5 — Hardening de GitHub | B2, B7, B8 | ✅ **hecha** (2026-07-19) — auditoría completa, 5/5 |

Hallazgo nuevo, encontrado *por* la Fase 2 (ver el detalle en su sección): **la semilla de
`posicion_casa.json` no estaba protegida** — `cierre_casa.py <mes-de-la-semilla>` contaba los
gastos de ese mes dos veces, en silencio. Corregido con un guard en el mismo commit.

Menor, sin asignar a fase: el `generado` del dashboard usa hora local naive, así que una
corrida de Actions lo estampa en UTC (dice "18:11" para algo generado 15:11 en Uruguay).

## Método

Lo que se verificó de verdad, no por lectura:

- **Datos:** recálculo independiente de los agregados desde `data/ledger.csv` (4.613 filas)
  reproduciendo la regla de `merge_y_validar.py` desde cero, y comparación celda a celda
  contra los CSV publicados. Chequeos de esquema, duplicados, invariantes y rangos.
- **Seguridad:** los 64 commits de **todas las refs + reflog**, buscando los *valores reales*
  de los 11 secretos locales (leídos de los archivos gitignored, comparados enmascarados)
  además de 14 patrones genéricos. Grep binario sobre `MyFinance.db`, el `.mmbackup` y los PDF.
- **GitHub:** API vía `gh` — visibilidad, forks, Pages, colaboradores, webhooks, deploy keys,
  secrets, variables, protección de branch, permisos del token, historial de corridas.
- **Código:** lectura completa de los scripts del pipeline + `py_compile` de todos, test del
  motor de conciliación, y validación del YAML con resolución de `MODE` en los tres casos.

## Veredicto

**El núcleo es confiable.** Los datos están sanos al peso (0 ids duplicados en 4.613 filas;
los tres agregados publicados se reproducen exactos desde el ledger — 642 celdas de resumen,
311 de presupuesto, toda la serie de casa, diferencia máxima $0,01 por redondeo). El historial
de git está limpio de secretos y **no hay forks** de la ventana en que el repo estuvo público.
Las 5 corridas de Actions salieron exitosas.

**El riesgo no está en lo que corre hoy**, sino en tres frentes: agujeros en la red de
seguridad (cosas que fallarían sin avisar), bombas de tiempo de calendario (agosto), y
documentos de proceso que enseñan prácticas que corrompen datos.

## Lo verificado sano

| Área | Evidencia |
|---|---|
| Integridad | 0 ids duplicados (4.613 filas); 0 filas malformadas (fecha/monto/mes/moneda/`en_analisis`); agregados = ledger en 642+311 celdas + 39 meses de casa; 0 fechas fuera de rango (2021-11-01 → 2026-07-15); `fecha_corte` única en `saldos_iniciales.csv` |
| Resoluciones | 119 filas, todas aplicadas al ledger; 0 campos inválidos. Las 8 "huérfanas" son `motivo_resuelto` de pendientes Splitwise que **por diseño nunca insertan fila** — borrarlas los resucitaría |
| Pendientes | 18 filas, **todas resueltas**; 0 abiertos, 0 con >14 días |
| Seguridad | 0 valores de secreto en 64 commits + reflog; 0 apariciones de la cédula en cualquier archivo trackeado; scopes mínimos (`gmail.readonly`, solo `spreadsheets`); secrets pasados por paso; sin `set -x`/`echo` de secrets; sin triggers que permitan ejecución por terceros |
| Repo | Privado; **0 forks**; 0 webhooks; 0 deploy keys; Pages desactivado; 1 solo colaborador (admin); token default de solo-lectura |
| Operación | 5/5 corridas exitosas, ~1 min c/u; ≈60-90 min/mes sobre 2.000 gratis; repo ~7 MB creciendo ~1-2 MB/mes |
| Código | Todos los scripts compilan; test del motor de conciliación pasa entero; paridad del fetch 57/57; idempotencia y watermark sólidos; core de dependencias pinneado |

**Puntualidad del cron (medido):** dispara con **+2 h a +3,3 h de atraso** (corridas de hoy:
+123 min y +199 min). Es el comportamiento normal de GitHub y el watermark lo absorbe sin
pérdida de datos — pero significa que "la corrida de las 21:23" puede caer a las 23-24 h.

**`SYNC_MODE` nunca existió como variable de repo** (`gh variable list` → vacío). Durante la
semana shadow el modo salía del default `'shadow'` en el YAML. No hay ninguna variable que
borrar; el cutover del 16/7 (default → `live`) es lo único que gobierna el modo.

## Hallazgos

Los IDs son estables: sirven para referenciarlos en los commits que los cierren.

### Alta — la red de seguridad tiene agujeros

| ID | Hallazgo | Impacto |
|---|---|---|
| **A1** | El gate del workflow filtra `[ERROR]`/`[DESVIO]` pero **no `[FALLO]`**, que es el estado que `merge_y_validar.py` reserva para lo peor: ids duplicados en el ledger y muestreo que no rastrea a la fuente | Un ledger corrupto se commitearía con tick verde |
| **A2** | `.claude/commands/revisar-pendientes.md` (pasos 3 y 5b) instruye **editar `data/ledger.csv` a mano** — el comando es anterior a que existiera `resoluciones.csv` | Una sesión que lo siga hace correcciones que *parecen* guardadas pero se borran en el próximo `--reprocesar`. Viola el invariante central del proyecto |

### Media — bombas de tiempo y robustez

| ID | Hallazgo | Detalle |
|---|---|---|
| **M1** | Un mail con layout nuevo **crashea el pipeline entero**: `procesar_mails.py` no tiene try/except por mail, y `parse_importe`/`campo_bloque` lanzan excepción | El diseño prometía pendiente `formato_nuevo`; nunca se implementó. Es ruidoso (no silencioso), pero bloquea Splitwise, dashboard y Sheets hasta arreglo manual |
| **M2** | El gate valida un mundo congelado: `2e.presupuesto` exige **exactamente 19 meses**, `2a.conteos` exige 2853/207 | **El 1/8, al cargar el presupuesto de agosto, la corrida se pone roja por diseño.** La validación de migración (una vez) y los invariantes del pipeline (siempre) están mezclados en un solo script |
| **M3** | Cambio de mes en UTC: la corrida PM del 31/7 (00:23 UTC = ya 1/8) computa agosto | Julio queda congelado en su snapshot de la mañana y agosto arrastra el desfase hasta que `/cierre-mes` recierre julio. Autosanable, pero silencioso mientras tanto |
| **M4** | **6 grupos de posibles dobles conteos legado**: misma (fecha, monto, cuenta, descripción) con `en_analisis=1` en ambas, todos `origen=planilla_casa` con `fuente_id` distinto | Exposición máxima **3.345,28 UYU** (7 filas extra) entre 2025-03 y 2026-01. No toca meses corrientes. Vienen de filas distintas de la fuente cruda → pueden ser compras repetidas reales: **requiere criterio humano** |
| **M5** | Los fallos no críticos (Sheets, dashboard, `cierre_casa`) son `::warning::` que nadie ve si no abre la corrida | Si algo falla todos los días, nadie se entera. Política elegida: escalar a Issue si persiste — falta implementarla |
| **M6** | `.claude/commands/sync-gastos.md` enseña el fetch viejo (conector + `search_threads`) como flujo principal; el determinístico está relegado a una nota | Es el método que causó los incidentes de julio (mails perdidos) |

**Detalle de M4** — los 6 grupos: `Cookies` 22,50 ×2 (2025-03-17) · `Lavado Auto` 188,14 ×2
(2025-04-28) · `Pantalon` 359,00 ×2 (2025-08-15) · `Nafta` 1.512,25 ×2 (2025-10-29) ·
`Productos Limpieza` 298,07 **×3** (2025-11-30) · `Picada` 667,25 ×2 (2026-01-02).
(Otros 20 grupos con monto coincidente pero descripción distinta se revisaron y son legítimos.)

### Baja

| ID | Hallazgo |
|---|---|
| **B1** | `pypdf>=5,<7` y `google-api-python-client>=2.100,<3` sin pin exacto: una versión nueva comprometida correría con los secrets del job (supply chain). El resto ya está en `==` |
| **B2** | `git add -A` (workflow y `correr_pipeline.cmd`): un archivo sensible futuro no previsto en `.gitignore` se commitearía solo |
| **B3** | Un crash entre la escritura de `ledger.csv` y la de `procesados.csv` duplica filas en la corrida siguiente (falta skip por id ya presente) |
| **B4** | Sin reintentos ante errores transitorios de la API de Gmail |
| **B5** | `extraer_cuerpo()` decodifica UTF-8 hardcodeado; un mail en otro charset corrompería acentos en vez de fallar |
| **B6** | **"Mercado Pago" usada en el ledger pero sin definir en `config/cuentas.csv`** — 2 filas de julio, una con `en_analisis=1`. No altera agregados, pero la deja sin `tipo` para conciliación de saldos (las otras 18 cuentas sin definir son legado 1money congelado, previo al corte de saldos) |
| **B7** | `master` sin protección contra force-push ni borrado |
| **B8** | Actions referenciadas por tag (`@v4`) en vez de SHA; policy `allowed_actions: all` |
| **B9** | Conviven **dos modelados de devolución**: histórico (141 filas `tipo=ingreso`, categoría "Devoluciones & cashback", 2023-03→2026-06) y nuevo (gasto negativo). El histórico no descuenta de la categoría del gasto; el nuevo sí. No viola ningún invariante, pero sesga comparaciones inter-era por categoría → **documentar, no tocar** |
| **B10** | `conciliar_saldos.py` compara contra `saldos_reales.csv`, que **no tiene fecha**: con un baseline viejo inventa diferencias que parecen descuadres (pasó el 16/7: 6 falsas alarmas, eran 4 días de gastos) |
| **B11** | `get_gmail_token.py:63-68` imprime client_secret y refresh_token a stdout **por diseño** (one-shot local; no puede correr en Actions). Quedan en el historial de la consola local |
| **B12** | Colisión teórica de nombres de PDF: Santander se salva por el nro de cuenta en el nombre; BBVA (siempre `EC Visa .pdf`) depende de la fecha del mail |
| **B13** | 48 filas con `monto_uyu` vacío — todas `transferencia USD en_analisis=0` de la era 1money. Ninguna entra en agregados; críticas (gasto + `en_analisis=1` + sin `monto_uyu`): **0** |

### Condicional — solo si el repo volviera a ser público

**No es una acción pendiente: es el inventario de por qué el repo NUNCA debe volver a ser
público** (ni usar GitHub Pages).

- Los PDF de Santander están cifrados con una clave **numérica corta**, crackeable offline en
  horas con hardware común; y el repo documenta en config el formato de esa clave. No cuentan
  como protegidos.
- Hay datos de **terceros** trackeados (el ingreso de Lu en `config/ingresos.csv`, nombres y
  deudas de amigos en `data/splitwise.csv`).
- Trackeado además: historia financiera completa (`MyFinance.db`, `.mmbackup`, `casa.xlsx`,
  `ledger.csv`), 108 mails bancarios crudos, números de cuenta reales, y el dashboard con la
  posición de casa.

**Cierre del incidente de exposición (11/7 20:48 → 15/7):** lo que estuvo efectivamente
público fue historia financiera (`MyFinance.db`, `.mmbackup`, `casa.xlsx`, `ledger.csv`,
72 mails, `ingresos.csv`, `splitwise.csv`, `saldos.csv`). **No** estuvieron públicos los PDF,
`cuentas_bancarias.csv` ni el dashboard (se agregaron el 16/7, ya en privado). **Cero
credenciales, cero cédula, cero forks.** No se requiere rotar nada. Las credenciales actuales
se crearon el 15-16/7, después del flip a privado. Único residual: los datos expuestos no son
rotables — si alguno sirve como pregunta de seguridad bancaria, tratarlo como conocido.

## Workflow de corrección

Cada fase cierra con un gate verificable. Las fases son independientes salvo el orden
sugerido (1 protege a las demás; 2 es la urgente por calendario).

### Fase 1 — Tapar los agujeros de la red de seguridad · cierra A1, M5, B4 ✅ HECHA (commit `9096f0c`)

1. ~~Gate del workflow: incluir `[FALLO]` en el grep.~~ **Cerrado mejor en la Fase 2**: el
   grep desapareció; `merge_y_validar.py` sale ≠0 y el workflow confía en el exit code.
2. Escalado: `scripts/salud_pasos.py` envuelve cada paso no crítico
   (`salud_pasos.py correr <paso> -- <comando>`), registra la racha y a las 3 seguidas abre un
   Issue. Un solo Issue por racha; se cierra solo al recuperarse el paso.
3. Reintentos en el fetch: los 5 llamados a la API de Gmail con `num_retries=3`.

**Corrección al plan:** la racha **no** podía ir en `config/estado_sync.json` como decía
arriba — `procesar_mails.py:825` reescribe ese archivo entero en cada corrida
(`json.dump({"ultimo_sync", "ultima_fecha_mail"})`), así que la racha se habría borrado sola y
nunca habría llegado a 3. El escalado habría quedado *mudo*, que es exactamente lo que venía a
arreglar. Vive en `data/staging/salud_pasos.json`, archivo propio, también commiteado.

Detalle de orden: el paso `escalar` corre **antes** del commit, porque marca el Issue como
abierto en ese JSON; si no se commiteara, la corrida siguiente abriría otro Issue por la misma
racha. Por eso "Dashboards + Sheets" y "Commit + push" quedaron como pasos separados — lo que
además mantiene `GH_TOKEN` y `GCP_SA_JSON` en pasos distintos.

**Gate — pasado.** Máquina de estados probada corrida por corrida: 1 y 2 → solo warning
(diciendo cuánto falta para escalar); 3 → abre Issue; 4 y 5 → **no** abren otro; recuperación →
lo cierra; racha nueva → vuelve a escalar. Camino real de `gh` probado de punta a punta contra
el repo (Issue #1, abierto y cerrado por el propio script sobre un paso de prueba). Shadow
verde en la nube.

**Lo que el gate destapó** (segundo bug propio cazado por una prueba, después del de junio en
la Fase 2): si `gh` devolvía una salida no parseable, el estado quedaba `issue_abierto=true`
**sin** `issue_num` — y entonces no cerraba nunca (el cierre pide el número) ni volvía a
escalar (la apertura ve que ya hay Issue). El paso se apagaba para siempre, en silencio: el
mismo modo de falla que este script existe para eliminar. Ahora `issue_abierto` se resetea al
recuperarse aunque falte el número, avisando que se cierre a mano.

### Fase 2 — Desactivar las bombas de agosto · cierra M1, M2, M3 ✅ HECHA (commit `f8d09a9`)

1. Partir `merge_y_validar.py`: los invariantes de pipeline (dupes, esquema, categorías,
   agregados) corren siempre; la validación de migración (conteos congelados, presupuesto de
   19 meses, 2b/2c/2f) pasa a `--migracion`, on-demand.
2. `cierre_casa.py`: la corrida diaria recomputa también el **mes anterior** (es idempotente y
   determinístico — julio queda vivo hasta que sus insumos dejen de cambiar).
3. `procesar_mails.py`: try/except por mail → pendiente `formato_nuevo` con el subject; el
   resto de la corrida sigue.

**Gate — pasado.** Resultados reales:

- **Agosto simulado** (presupuesto con 20 meses + un gasto de 2026-08): modo pipeline
  **verde**; `--migracion` **exit 1** cazando `2e`. O sea: la bomba está desactivada y la
  validación sigue viva, solo salió del camino diario. Ese `exit 1` es literalmente lo que
  habría puesto la corrida en rojo el 1/8.
- **Mail malformado** (un aviso real con la etiqueta `Importe` renombrada): el código viejo
  tira `ValueError: importe no parseable: ''` y sale 1; el nuevo deja el pendiente
  `formato_nuevo`, sigue con el resto de los mails y sale 0.
- **1/8 simulado** (`dt.date.today()` mockeado + un gasto de casa el 31/7 a la noche): julio
  se recalcula primero y levanta ese gasto (62.741 → 66.741), y agosto arranca de
  `cierre 2026-07`, no de la semilla. Sin el fix, ese gasto quedaba fuera del cierre de julio.
- **Sin cambio de datos**: `ledger.csv` y `resumen_mensual.csv` idénticos byte a byte al
  baseline; `dashboard.html` idéntico salvo el timestamp; `validacion-migracion.md` solo mueve
  3 filas de la tabla resumen (los invariantes ahora corren antes). `test_conciliacion` pasa.
- **En la nube**: corrida shadow verde antes del cron.

**Lo que el gate destapó** (y por qué vale la pena tener gates): el paso 2 del plan,
"recomputar el mes anterior", habría **corrompido junio en la primera corrida**. La semilla
`posicion_casa.json` está fechada 2026-06-30, o sea que ya *es* la posición al cierre de
junio; recalcular junio encima sumaba sus gastos por segunda vez, y julio heredaba esa
posición envenenada. Se agregó el guard que faltaba (`mes <= mes_semilla` → saltear), que
además tapa un agujero preexistente: `cierre_casa.py 2026-06` ya corrompía los datos en
silencio, sin que nada lo avisara.

### Fase 3 — Corregir los documentos de proceso · cierra A2, M6, B10 ✅ HECHA

1. Reescribir `/revisar-pendientes`: toda corrección vía `resoluciones.csv`, nunca el ledger,
   con el mapeo campo/valor por caso (categoría, ámbito, `en_analisis`, splits).
2. Reescribir `/sync-gastos` alrededor de `fetch_mails.py`; el conector queda como fallback
   explícitamente marcado "solo si la API falla".
3. `/cierre-mes`: conciliar a la **fecha del extracto** (no "hoy"); agregar fecha a
   `saldos_reales.csv` y hacer que `conciliar_saldos.py` **aborte** si el baseline está viejo
   (abortar, no advertir: el output silencioso-pero-erróneo es peor que no correr).

**Gate — pasado.** Resultados reales:

- `conciliar_saldos.py` sin argumento contra saldos del 12/7 → **aborta** con el mensaje que
  explica la falsa diferencia y las dos salidas; a la fecha correcta (2026-07-12) → corre y da
  12 OK / 0 diferencias (es la semilla); archivo en formato viejo → mensaje que pide el
  formato nuevo. Probado en copia aislada (`GASTOS_BASE`).
- `data/staging/saldos_reales.csv` migrado: 12 filas con `fecha_corte=2026-07-12` (su cosecha
  real, la de la semilla).
- Los tres comandos releídos de punta a punta contra los invariantes de CLAUDE.md:
  `/revisar-pendientes` ya no toca el ledger (todo por `resoluciones.csv`, con la tabla
  campo/valor por caso, `motivo_resuelto`, splits con la fila hermana `origen=manual` como
  único alta legítima, y el motivo nuevo `formato_nuevo` de la Fase 2);
  `/sync-gastos` quedó alrededor de `fetch_mails.py` con el conector como fallback marcado
  (incluida la advertencia de paridad del body_text y el gotcha `cmd /c`);
  `/cierre-mes` concilia a la fecha de los saldos, no a "hoy".

### Fase 4 — Higiene de datos y dependencias · cierra B1, B3, B5, B6, B9, B12, M4 ✅ HECHA

1. Alta de "Mercado Pago" en `config/cuentas.csv`; pin exacto de `pypdf` y
   `google-api-python-client`; skip por id ya presente en `procesar_mails`; charset del mail
   respetando el header MIME; sufijo `message_id` en los nombres de PDF.
2. Los 6 posibles dobles legado (M4) → se presentan en la próxima `/revisar-pendientes`;
   decisión de Joaco, con `en_analisis=0` vía `resoluciones.csv` si los confirma.
3. Documentar en `docs/esquema.md` la coexistencia de los dos modelados de devolución (B9).

**Gate — pasado.** Validación verde en los dos modos, `test_conciliacion` pasa, y los agregados
no se movieron: el único delta observado fue exactamente el mail que trajo el fetch en el medio
(ANCAP Trouville 2.883,50 × coef. de julio 0,630747 = 1.818,76, al centavo).

**Correcciones al plan, encontradas al ejecutarlo:**

- **B5 estaba mal y "arreglarlo" habría roto datos.** Mercado Libre y Mercado Pago declaran
  `charset=iso-8859-1` y mandan UTF-8 igual: verificado sobre los mails reales, donde los
  acentos llegan perfectos *porque* hoy se decodifica UTF-8 a la fuerza. Respetar el header
  habría convertido cada "ó" en "Ã³". El fix va al revés: UTF-8 primero, header solo como plan
  B. Paridad verificada re-bajando los 74 mails y decodificando de las dos formas: 74/74
  idénticos. (El primer intento de test comparó contra los mails guardados por el conector
  viejo y dio 34 diferencias — todas del delta cosmético conocido de la migración, ninguna
  mía. El test correcto aísla el cambio.)
- **M4 eran 7 grupos, no 6** — la auditoría se comió "Estacion" (300,81). Exposición real
  3.646,09 (parte de Joaco) / 6.373,00 (gasto total de casa).
- **M4 tenía un segundo consumidor no previsto.** Sacar las filas vía `resoluciones.csv`
  corrige el ledger, pero la pestaña de casa del dashboard se arma de `data/casa.csv`, que es
  **otro artefacto de la misma planilla** y no pasa por las resoluciones. Sin eso, el mismo
  gasto quedaba con dos números distintos según la pestaña. Se aplicó el mismo filtro en
  `generar_dashboard_data.py`.
- **M4 rompía la validación de migración, como estaba previsto**: `2b` comparaba un ledger
  corregido contra una planilla cruda (`planilla d=-2978.84`). Se agregó
  `filas_planilla_excluidas()` en `merge_y_validar.py`, espejo de `cargar_overrides_1money()`,
  y `2b` volvió a 0,00 en todos los años.
- **Las posiciones de casa NO cambiaron** con M4: los 7 grupos son de 2025-03 → 2026-01, todos
  anteriores a la semilla (2026-06-30), que sale del extracto bancario y no de sumar la
  planilla. La corrección afecta la foto histórica, no lo que cada uno debe.

**Hallazgo extra:** `docs/esquema.md` documentaba la semilla de casa con los valores **viejos**
(Lu 11.244,07 / Joaco −9.692,04), de antes de reconciliar contra el banco — el propio
`posicion_casa.json` los lista como "valores previos". Corregido, y ahora apunta al JSON como
fuente de verdad en vez de repetir el número.

### Fase 5 — Hardening de GitHub · cierra B2, B7, B8 ✅ HECHA (2026-07-19)

1. **Ruleset `proteger-master`** (id 19169294): bloquea force-push (`non_fast_forward`) y
   borrado del branch, `enforcement: active`, sin bypass para nadie — para forzar un push hay
   que editar el ruleset primero (fricción visible, a propósito). Nota de plan: los rulesets
   básicos SÍ están disponibles en repos privados del plan Free; las *branch protection rules*
   clásicas no — por eso se fue por ruleset.
2. **`git add -A` → `git add data out config/estado_sync.json`** en el workflow y en
   `correr_pipeline.cmd`: el pipeline solo escribe ahí; un archivo inesperado en el árbol ya
   no se commitea solo.
3. **Actions pinneadas por SHA** (`checkout@34e11487…`, `setup-python@a26af69b…`) y policy del
   repo en `selected` con solo GitHub-owned: un tag `v4` es movible, el SHA no.

Extra del mismo día: el workflow abre Issues de pendientes pero nada los cerraba (se
acumularon #2-#4 con la bandeja ya en 0). `/revisar-pendientes` ahora los cierra al final.

**Gate:** el push de este mismo commit pasó por el ruleset recién activo (push normal OK), y
la siguiente corrida live valida el `git add` acotado end-to-end.

## Qué NO se auditó

Para que el alcance quede honesto:

- **La lógica de negocio de los parsers** contra mails reales nuevos (se validó la paridad
  57/57 en la migración, no cada layout posible de cada banco).
- **Correctitud contable de los datos históricos** pre-migración más allá de los invariantes
  estructurales: el ledger reproduce sus fuentes, pero si una fuente ya tenía un error, se
  arrastra. M4 es el único caso donde el arrastre es detectable estructuralmente.
- **`conciliar_estado.py` / `conciliar_debito.py`** en profundidad: no están en el pipeline
  automático (corren en `/cierre-mes`). Su motor de matching sí está cubierto por
  `test_conciliacion.py`, que pasa.
- **Los Google Sheets y Looker** aguas abajo del push.

## Post-auditoría

- **2026-08-02 — doble división de la planilla (detectado por Joaco, no por un gate):**
  `merge_y_validar.py` aplicaba el coeficiente a las filas `origen=planilla_casa`, que **ya
  son la parte de Joaco** → todo su gasto personal histórico hasta jun-2026 quedaba
  subestimado 12-23k/mes, y julio parecía una anomalía contra presupuesto cuando no lo era.
  Lo destapó Joaco al insistir en que los gastos comunes "nunca saltaron" — el salto era
  comparar su parte (serie del ledger) contra el total (serie de los mails).
  **Límite del gate de agregados de esta auditoría:** el recálculo independiente reproducía
  la MISMA regla equivocada, así que daba diferencia 0,00. Los errores de semántica de los
  datos no los caza un recálculo — los caza comparar contra la fuente cruda o contra el
  dueño de los datos. Corregido en `merge_y_validar.py` (solo se divide lo que no es
  `planilla_casa`); invariante documentado en CLAUDE.md y docs/esquema.md.
