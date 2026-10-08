@echo off
REM ============================================================================
REM CallNotifier - release script
REM Usage: make_release.bat 1.1.0
REM ============================================================================

setlocal enabledelayedexpansion

set VERSION=%1
if "%VERSION%"=="" (
    echo.
    echo Usage: make_release.bat ^<version^>
    echo Example: make_release.bat 1.1.0
    echo.
    exit /b 1
)

REM --- Date in YYYY-MM-DD via PowerShell (locale-independent) ---
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set DATE=%%i

set RELEASE_DIR=C:\Dev\CallNotifier_Releases\v%VERSION%_%DATE%
set EXE_NAME=CallNotifierClient-%VERSION%.exe
set EXE_PATH=static\client\%EXE_NAME%

echo.
echo ============================================================
echo  Creating release v%VERSION% dated %DATE%
echo  Target dir: %RELEASE_DIR%
echo ============================================================
echo.

REM --- 1. Check .exe ---
echo [1/7] Checking .exe...
if not exist "%EXE_PATH%" (
    echo.
    echo ERROR: %EXE_PATH% not found.
    echo Build .exe via PyInstaller and place it into static\client\
    echo.
    exit /b 1
)
echo     OK: %EXE_PATH%

REM --- 2. Check CHANGELOG.md ---
echo [2/7] Checking CHANGELOG.md...
if not exist "CHANGELOG.md" (
    echo ERROR: CHANGELOG.md not found in project root.
    exit /b 1
)
echo     OK: CHANGELOG.md

REM --- 3. Check PROJECT_STATUS.md ---
echo [3/7] Checking PROJECT_STATUS.md...
if not exist "PROJECT_STATUS.md" (
    echo     WARN: PROJECT_STATUS.md not found, skipping.
)

REM --- 4. Create release dir (no prompt) ---
echo [4/7] Creating release dir...
if exist "%RELEASE_DIR%" (
    echo     WARN: dir already exists, overwriting files.
)
mkdir "%RELEASE_DIR%" 2>nul

REM --- 5. sha256 ---
echo [5/7] Computing sha256...
certutil -hashfile "%EXE_PATH%" SHA256 | findstr /r "^[0-9a-f]" > "%RELEASE_DIR%\%EXE_NAME%.sha256"
echo     sha256:
type "%RELEASE_DIR%\%EXE_NAME%.sha256"

REM --- 6. Copy artifacts ---
echo [6/7] Copying artifacts...
copy /Y "%EXE_PATH%" "%RELEASE_DIR%\" >nul
copy /Y "CHANGELOG.md" "%RELEASE_DIR%\" >nul
if exist "PROJECT_STATUS.md" copy /Y "PROJECT_STATUS.md" "%RELEASE_DIR%\" >nul
if exist "README.md" copy /Y "README.md" "%RELEASE_DIR%\" >nul

REM --- 7. requirements_locked ---
echo [7/7] Generating requirements_*_locked.txt...
python -m pip freeze > "%RELEASE_DIR%\requirements_server_locked.txt"

if exist ".\venv_client\Scripts\activate.bat" (
    call .\venv_client\Scripts\activate.bat
    python -m pip freeze > "%RELEASE_DIR%\requirements_client_locked.txt"
    call deactivate
) else (
    echo     WARN: venv_client not found, skipping client requirements.
)

REM --- Source zip ---
echo.
echo Creating source.zip...
powershell -NoProfile -ExecutionPolicy Bypass -File make_source_zip.ps1 -ReleaseDir "%RELEASE_DIR%"

echo.
echo ============================================================
echo  RELEASE v%VERSION% COMPLETE
echo ============================================================
echo.
echo Contents of %RELEASE_DIR%:
dir /b "%RELEASE_DIR%"
echo.
echo Next steps:
echo   1. Verify archive contents
echo   2. git add . ^&^& git commit -m "Release v%VERSION%"
echo   3. git tag -a v%VERSION% -m "Release v%VERSION%"
echo   4. git push origin master ^&^& git push origin v%VERSION%
echo.

endlocal