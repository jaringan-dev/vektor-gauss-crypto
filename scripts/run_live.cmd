@echo off
REM Peluncur scanner live (loop tiap 15 menit). Dipakai oleh Task Scheduler.
cd /d "%~dp0.."
"C:\Users\NUC 11ATKC4\AppData\Local\Programs\Python\Python312\python.exe" -m smcbot.live run >> data\live_stdout.log 2>&1
