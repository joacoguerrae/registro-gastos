@echo off
rem Corrida desatendida del sync de gastos (Programador de tareas de Windows).
rem 100%% DETERMINISTICA: sin Claude en el camino critico.
rem   1. git pull   (trae lo que haya commiteado la nube / otra sesion)
rem   2. fetch_mails.py  (Gmail API; usa config/gmail_token.json)
rem   3. correr_pipeline.cmd  (procesar -> splitwise -> validar -> cierre casa -> dashboards -> Sheets -> commit)
rem   4. git push   (el pipeline commitea local; aca se sube, si no GitHub se atrasa)
rem Reemplaza a sync_gastos_headless.cmd (que usaba Claude headless para el fetch).
cd /d "C:\Users\joaco\Desktop\proyectos claude\gastos"
echo ==================== %date% %time% ==================== >> out\sync_log.txt

rem --- 1) pull: si hay conflicto, abortamos; el watermark garantiza que no se pierde nada
git pull --rebase >> out\sync_log.txt 2>&1
if errorlevel 1 (
  echo [ABORT] git pull --rebase fallo -- se resuelve a mano; la proxima corrida reintenta >> out\sync_log.txt
  exit /b 1
)

rem --- 2) fetch deterministico (si falla, el pipeline igual procesa lo que ya hay)
C:\Users\joaco\anaconda3\python.exe scripts\fetch_mails.py >> out\sync_log.txt 2>&1
echo [fetch exit %errorlevel%] >> out\sync_log.txt

rem --- 3) pipeline completo (commitea al final)
call scripts\correr_pipeline.cmd >> out\sync_log.txt 2>&1
echo [pipeline exit %errorlevel%] >> out\sync_log.txt

rem --- 4) push (con reintento si el remoto se movio mientras tanto)
git push >> out\sync_log.txt 2>&1
if errorlevel 1 (
  echo [aviso] push fallo, reintento con pull --rebase >> out\sync_log.txt
  git pull --rebase >> out\sync_log.txt 2>&1
  git push >> out\sync_log.txt 2>&1
)
echo [push exit %errorlevel%] >> out\sync_log.txt
