@echo off
cd /d C:\Users\DELL\Documents\AGENT\agent-01
call venv\Scripts\activate
start http://127.0.0.1:8001
uvicorn app:app --host 127.0.0.1 --port 8001
pause