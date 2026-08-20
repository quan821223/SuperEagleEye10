@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS1_PATH=%SCRIPT_DIR%build_SuperEagleEye.ps1"

if not exist "%PS1_PATH%" (
    echo [SEE][ERROR] build_SuperEagleEye.ps1 not found.
    exit /b 1
)

echo [SEE] Building SuperEagleEye runtime...
powershell -ExecutionPolicy Bypass -File "%PS1_PATH%"
if errorlevel 1 (
    echo [SEE][ERROR] Build failed.
    exit /b 1
)

echo [SEE] Build complete.
set "SEE_DIST_DIR=%SCRIPT_DIR%dist\SuperEagleEye_dist"
call echo [SEE] Output: %%SEE_DIST_DIR%%
call echo [SEE] Executable: %%SEE_DIST_DIR%%\SuperEagleEye.exe
echo [SEE] Executables:
if defined SEE_DIST_DIR call dir /b "%%SEE_DIST_DIR%%\SuperEagleEye*.exe"
echo [SEE] Archives:
dir /b "%SCRIPT_DIR%dist\SuperEagleEye*.7z"
exit /b 0
