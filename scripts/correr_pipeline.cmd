@echo off
rem Punto de entrada unico del pipeline (lo invoca /sync-gastos, tambien sirve a mano).
rem Corre: procesar mails -> sync Splitwise -> validacion (escribe resumen_mensual.csv)
rem       -> datos de dashboard (LEE resumen_mensual.csv, debe ir despues) -> push a Sheets -> commit.
cd /d "C:\Users\joaco\Desktop\proyectos claude\gastos"
rem Los pasos NO CRITICOS van envueltos en salud_pasos.py (igual que en Actions):
rem no frenan la corrida, pero la racha queda registrada y a las 3 seguidas se
rem abre un Issue. Los criticos (procesar_mails, merge_y_validar,
rem generar_dashboard_data) cortan con exit /b 1: si el ledger no valida, no se
rem commitea.
C:\Users\joaco\anaconda3\python.exe scripts\procesar_mails.py || exit /b 1
rem sync_splitwise va DESPUES de procesar_mails (auditoria 2026-09 SM6): arma
rem `cargos` desde el ledger, y si corre antes, un gasto de Splitwise cuyo aviso
rem de tarjeta llega el mismo dia no matchea y genera un pendiente flotante que
rem la corrida siguiente duplica sobre el id del cargo. Queda ANTES de
rem merge_y_validar para que la validacion cubra tambien lo que inserto.
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py correr sync_splitwise -- C:\Users\joaco\anaconda3\python.exe scripts\sync_splitwise.py
rem planilla "Carga manual" (auditoria 2026-09 Fase 1.3): no critica, igual que en Actions
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py correr leer_carga_manual -- C:\Users\joaco\anaconda3\python.exe scripts\leer_carga_manual.py
C:\Users\joaco\anaconda3\python.exe scripts\merge_y_validar.py || exit /b 1
rem posicion de casa: deterministica (ledger + ingresos.csv + aportes_lu.csv) -> refresca la
rem posicion del mes anterior y el actual en cada corrida.
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py correr cierre_casa -- C:\Users\joaco\anaconda3\python.exe scripts\cierre_casa.py
C:\Users\joaco\anaconda3\python.exe scripts\generar_dashboard_data.py || exit /b 1
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py correr generar_dashboard_html -- C:\Users\joaco\anaconda3\python.exe scripts\generar_dashboard_html.py
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py correr actualizar_sheets -- C:\Users\joaco\anaconda3\python.exe scripts\actualizar_sheets.py
rem escalar necesita gh autenticado; en local puede no estarlo -> no es critico
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py escalar || echo (aviso: escalar fallo -- se reintenta en la proxima corrida)
rem paths explicitos, no -A (auditoria B2): igual que en Actions, para que un
rem archivo inesperado en el arbol no se commitee solo.
git add data out config/estado_sync.json
git commit -m "sync automatico %date% %time%" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>" || echo (sin cambios para commitear)
exit /b 0
