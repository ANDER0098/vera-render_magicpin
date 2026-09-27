@echo off
rem Local runner: loads .env into the environment, then serves on :8080.
rem The app reads plain environment variables (no dotenv dependency).
setlocal
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%~dp0.env") do set "%%a=%%b"
py -X utf8 -m uvicorn app:app --host 0.0.0.0 --port 8080
