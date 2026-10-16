@echo off
REM Peluncur perekam data posisi (loop tiap 15 menit). Dipakai oleh Task Scheduler saat logon.
REM Bila PC mati/tidur, siklus yang terlewat terisi lagi otomatis (jeda < 2 jam) berkat upsert.
cd /d "%~dp0.."
"C:\Users\NUC 11ATKC4\AppData\Local\Programs\Python\Python312\python.exe" -m smcbot.recorder run >> data\recorder_stdout.log 2>&1
