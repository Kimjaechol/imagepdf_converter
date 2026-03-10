@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo.
echo  ========================================================
echo    MoA Document Converter - One-Click Installer Build
echo  ========================================================
echo    Requirements:
echo    - Rust (https://rustup.rs)
echo    - Node.js 18+ (https://nodejs.org) [optional]
echo    - Python 3.10+ (for PDF conversion) [auto-bundled]
echo  ========================================================
echo.

:: Check Rust
where rustc >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Rust is not installed!
    echo         Please install from https://rustup.rs
    echo.
    echo         After installation, reopen terminal and run this script again.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('rustc --version') do set RUST_VER=%%v
echo [OK] %RUST_VER%

:: Settings
set PROJECT_ROOT=%~dp0
set BUILD_DIR=%PROJECT_ROOT%build_output
set PORTABLE_PYTHON=%BUILD_DIR%\portable_python
set PYTHON_VERSION=3.11.9
set PYTHON_SHORT=311

:: Step 1: Portable Python
echo.
echo ----------------------------------------
echo  [1/4] Setting up portable Python...
echo ----------------------------------------

if exist "%PORTABLE_PYTHON%\python.exe" (
    echo Portable Python already exists - skipping
) else (
    if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
    mkdir "%BUILD_DIR%"
    mkdir "%PORTABLE_PYTHON%"

    echo Downloading Python %PYTHON_VERSION%...
    set PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-embed-amd64.zip
    powershell -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '!PYTHON_URL!' -OutFile '%BUILD_DIR%\python.zip'"
    powershell -Command "Expand-Archive -Path '%BUILD_DIR%\python.zip' -DestinationPath '%PORTABLE_PYTHON%' -Force"
    del "%BUILD_DIR%\python.zip"

    :: Enable pip
    echo python%PYTHON_SHORT%.zip> "%PORTABLE_PYTHON%\python%PYTHON_SHORT%._pth"
    echo .>> "%PORTABLE_PYTHON%\python%PYTHON_SHORT%._pth"
    echo Lib\site-packages>> "%PORTABLE_PYTHON%\python%PYTHON_SHORT%._pth"
    echo import site>> "%PORTABLE_PYTHON%\python%PYTHON_SHORT%._pth"

    echo Installing pip...
    powershell -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%BUILD_DIR%\get-pip.py'"
    "%PORTABLE_PYTHON%\python.exe" "%BUILD_DIR%\get-pip.py" --no-warn-script-location >nul 2>&1
    del "%BUILD_DIR%\get-pip.py"

    echo Installing Python libraries... (5-15 min)
    "%PORTABLE_PYTHON%\python.exe" -m pip install --no-warn-script-location --quiet ^
        pymupdf Pillow pytesseract opencv-python-headless numpy scikit-learn ^
        fastapi "uvicorn[standard]" python-multipart websockets ^
        google-generativeai httpx pydantic pyyaml tqdm aiofiles stripe

    echo Installing PyTorch CPU...
    "%PORTABLE_PYTHON%\python.exe" -m pip install --no-warn-script-location --quiet ^
        torch torchvision --index-url https://download.pytorch.org/whl/cpu 2>nul

    echo Installing surya-ocr...
    "%PORTABLE_PYTHON%\python.exe" -m pip install --no-warn-script-location --quiet surya-ocr 2>nul

    echo Cleaning up portable Python to reduce size...
    :: Remove __pycache__ directories
    for /d /r "%PORTABLE_PYTHON%" %%d in (__pycache__) do (
        if exist "%%d" rmdir /s /q "%%d"
    )
    :: Remove .dist-info directories
    for /d /r "%PORTABLE_PYTHON%" %%d in (*.dist-info) do (
        if exist "%%d" rmdir /s /q "%%d"
    )
    :: Remove test directories from packages
    for /d /r "%PORTABLE_PYTHON%\Lib\site-packages" %%d in (tests test) do (
        if exist "%%d" rmdir /s /q "%%d"
    )
    :: Remove pip cache
    if exist "%PORTABLE_PYTHON%\Lib\site-packages\pip" rmdir /s /q "%PORTABLE_PYTHON%\Lib\site-packages\pip"
    if exist "%PORTABLE_PYTHON%\Scripts\pip*.exe" del /q "%PORTABLE_PYTHON%\Scripts\pip*.exe"
    echo Cleanup complete!

    echo Python environment ready!
)

:: Step 2: Tauri CLI
echo.
echo ----------------------------------------
echo  [2/4] Checking Tauri CLI...
echo ----------------------------------------

where cargo-tauri >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing Tauri CLI...
    cargo install tauri-cli --locked
)
echo Tauri CLI ready!

:: Step 3: Build Tauri app
echo.
echo ----------------------------------------
echo  [3/4] Building Tauri app... (5-10 min for first build)
echo ----------------------------------------

cd /d "%PROJECT_ROOT%"
cargo tauri build

if %errorlevel% neq 0 (
    echo [ERROR] Tauri build failed!
    echo         Check the error above and try again.
    pause
    exit /b 1
)

:: Step 4: Bundle portable Python
echo.
echo ----------------------------------------
echo  [4/4] Bundling portable Python...
echo ----------------------------------------

:: Already included via tauri.conf.json resources
echo Done!

:: Complete
echo.
echo  ========================================================
echo                     BUILD COMPLETE!
echo  ========================================================
echo    Installer location:
echo    src-tauri\target\release\bundle\nsis\
echo.
echo    Portable executable:
echo    src-tauri\target\release\pdf-converter.exe
echo.
echo    Tauri vs Electron:
echo    - Installer 10x smaller
echo    - Memory usage 4-5x less
echo    - Startup speed 3x faster
echo  ========================================================
echo.

explorer "%PROJECT_ROOT%src-tauri\target\release\bundle\nsis"
pause
