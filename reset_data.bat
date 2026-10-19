@echo off
cd /d %~dp0
echo This deletes the local database, uploaded images and backups, then re-seeds from the CSV files.
set /p ok=Type YES to continue: 
if /i not "%ok%"=="YES" exit /b
rmdir /s /q data 2>nul
call run.bat
