@echo off
rem KiCad Pro+ portable launcher.
rem Keeps its own settings (so your normal KiCad is never touched) and reuses the
rem libraries of an existing KiCad 10 install when one is found.
setlocal
set "HERE=%~dp0"
set "KICAD_CONFIG_HOME=%APPDATA%\kicad-pro-plus"

set "LIBROOT="
for %%D in ("D:\programs\kicad\share\kicad" "C:\Program Files\KiCad\10.0\share\kicad") do (
  if not defined LIBROOT if exist "%%~D\symbols" set "LIBROOT=%%~D"
)
if not defined LIBROOT set "LIBROOT=%HERE%share\kicad"

set "KICAD10_SYMBOL_DIR=%LIBROOT%\symbols"
set "KICAD10_FOOTPRINT_DIR=%LIBROOT%\footprints"
set "KICAD10_3DMODEL_DIR=%LIBROOT%\3dmodels"
set "KICAD10_TEMPLATE_DIR=%LIBROOT%\template"

start "" "%HERE%bin\kicad.exe" %*
