@echo off
rem Punto de entrada unico del pipeline (lo invoca /sync-gastos, tambien sirve a mano).
rem Corre: sync Splitwise -> procesar mails -> validacion (escribe resumen_mensual.csv)
rem       -> datos de dashboard (LEE resumen_mensual.csv, debe ir despues) -> push a Sheets -> commit.
cd /d "C:\Users\joaco\Desktop\proyectos claude\gastos"
rem Los pasos NO CRITICOS van envueltos en salud_pasos.py (igual que en Actions):
rem no frenan la corrida, pero la racha queda registrada y a las 3 seguidas se
rem abre un Issue. Los criticos (procesar_mails, merge_y_validar,
rem generar_dashboard_data) cortan con exit /b 1: si el ledger no valida, no se
rem commitea.
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py correr sync_splitwise -- C:\Users\joaco\anaconda3\python.exe scripts\sync_splitwise.py
C:\Users\joaco\anaconda3\python.exe scripts\procesar_mails.py || exit /b 1
C:\Users\joaco\anaconda3\python.exe scripts\merge_y_validar.py || exit /b 1
rem posicion de casa: deterministica (ledger + ingresos.csv + aportes_lu.csv) -> refresca la
rem posicion del mes anterior y el actual en cada corrida.
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py correr cierre_casa -- C:\Users\joaco\anaconda3\python.exe scripts\cierre_casa.py
C:\Users\joaco\anaconda3\python.exe scripts\generar_dashboard_data.py || exit /b 1
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py correr generar_dashboard_html -- C:\Users\joaco\anaconda3\python.exe scripts\generar_dashboard_html.py
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py correr actualizar_sheets -- C:\Users\joaco\anaconda3\python.exe scripts\actualizar_sheets.py
rem escalar necesita gh autenticado; en local puede no estarlo -> no es critico
C:\Users\joaco\anaconda3\python.exe scripts\salud_pasos.py escalar || echo (aviso: escalar fallo -- se reintenta en la proxima corrida)
git add -A
git commit -m "sync automatico %date% %time%" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>" || echo (sin cambios para commitear)
exit /b 0
