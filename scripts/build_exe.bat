@echo off
rem Build standalone Windows executables with PyInstaller:
rem   dist\email_date_range.exe   date range / distribution / gaps scanner
rem   dist\recover_pst.exe        chain-of-custody recovery of deleted items
rem
rem Needs Python 3.10+ (py launcher or python on PATH). No compiler is needed:
rem libpff-python ships prebuilt wheels for Windows.
rem Optional: set DIST / WORK to change the output and scratch folders.

setlocal
cd /d "%~dp0.."
if "%DIST%"=="" set "DIST=dist"
if "%WORK%"=="" set "WORK=build"

rem PY can be set by the caller (CI sets PY=python)
if "%PY%"=="" set "PY=py -3"
%PY% --version >nul 2>&1 || set "PY=python"
%PY% --version >nul 2>&1 || (echo ERROR: Python 3.10+ not found. & exit /b 1)

echo [1/3] Creating build environment in %WORK%\venv ...
if exist "%WORK%\venv" rmdir /s /q "%WORK%\venv"
%PY% -m venv "%WORK%\venv" || exit /b 1
set "VPY=%WORK%\venv\Scripts\python.exe"
"%VPY%" -m pip install --quiet --upgrade pip
"%VPY%" -m pip install --quiet ".[build]" || (echo ERROR: pip install failed. & exit /b 1)

echo [2/3] Building executables ...
for %%N in (email_date_range recover_pst) do (
    "%VPY%" -m PyInstaller --noconfirm --clean --onefile --console ^
        --name %%N --distpath "%DIST%" --workpath "%WORK%\%%N" --specpath "%WORK%" ^
        "packaging\entry_%%N.py" || (echo ERROR: building %%N failed. & exit /b 1)
)

echo [3/3] Smoke test ...
"%DIST%\email_date_range.exe" --version || exit /b 1
"%DIST%\recover_pst.exe" --version || exit /b 1

echo.
echo Done. Executables are in %DIST%\
endlocal
