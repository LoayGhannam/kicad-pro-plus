@echo off
rem KODAT PCB - AI window. Uses the Python that ships with KODAT PCB / KiCad (it has wxPython).
setlocal
set "PYW=%~dp0..\bin\pythonw.exe"
if not exist "%PYW%" set "PYW=D:\programs\kicad\bin\pythonw.exe"
if not exist "%PYW%" set "PYW=C:\Program Files\KiCad\10.0\bin\pythonw.exe"
if not exist "%PYW%" (echo Could not find KiCad's pythonw.exe & pause & exit /b 1)
start "" "%PYW%" "%~dp0kodat_ai_gui.py"
