@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo.
echo  ╔══════════════════════════════════════════════════════════╗
echo  ║       MoA 문서 변환기 - 원클릭 설치파일 빌드            ║
echo  ║                                                          ║
echo  ║  필요 사항:                                              ║
echo  ║  - Rust (https://rustup.rs)                              ║
echo  ║  - Node.js 18+ (https://nodejs.org) [선택]               ║
echo  ║  - Python 3.10+ (PDF 변환용) [자동 번들]                 ║
echo  ╚══════════════════════════════════════════════════════════╝
echo.

:: ──────────────────────────────────────────────
:: 사전 확인
:: ──────────────────────────────────────────────
where rustc >nul 2>&1
if %errorlevel% neq 0 (
    echo [오류] Rust가 설치되어 있지 않습니다!
    echo        https://rustup.rs 에서 설치해주세요.
    echo.
    echo        설치 후 터미널을 다시 열고 이 스크립트를 실행하세요.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('rustc --version') do set RUST_VER=%%v
echo [확인] %RUST_VER%

:: ──────────────────────────────────────────────
:: 설정
:: ──────────────────────────────────────────────
set PROJECT_ROOT=%~dp0
set BUILD_DIR=%PROJECT_ROOT%build_output
set PORTABLE_PYTHON=%BUILD_DIR%\portable_python
set PYTHON_VERSION=3.11.9
set PYTHON_SHORT=311

:: ──────────────────────────────────────────────
:: 1단계: Python 포터블 다운로드 및 설정
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [1/4] Python 포터블 환경 구성 중...
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if exist "%PORTABLE_PYTHON%\python.exe" (
    echo Python 포터블 이미 존재 - 건너뜀
) else (
    if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
    mkdir "%BUILD_DIR%"
    mkdir "%PORTABLE_PYTHON%"

    echo Python %PYTHON_VERSION% 다운로드 중...
    set PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-embed-amd64.zip
    powershell -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '!PYTHON_URL!' -OutFile '%BUILD_DIR%\python.zip'"
    powershell -Command "Expand-Archive -Path '%BUILD_DIR%\python.zip' -DestinationPath '%PORTABLE_PYTHON%' -Force"
    del "%BUILD_DIR%\python.zip"

    :: Enable pip
    echo python%PYTHON_SHORT%.zip> "%PORTABLE_PYTHON%\python%PYTHON_SHORT%._pth"
    echo .>> "%PORTABLE_PYTHON%\python%PYTHON_SHORT%._pth"
    echo Lib\site-packages>> "%PORTABLE_PYTHON%\python%PYTHON_SHORT%._pth"
    echo import site>> "%PORTABLE_PYTHON%\python%PYTHON_SHORT%._pth"

    echo pip 설치 중...
    powershell -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%BUILD_DIR%\get-pip.py'"
    "%PORTABLE_PYTHON%\python.exe" "%BUILD_DIR%\get-pip.py" --no-warn-script-location >nul 2>&1
    del "%BUILD_DIR%\get-pip.py"

    echo Python 라이브러리 설치 중... (5~15분)
    "%PORTABLE_PYTHON%\python.exe" -m pip install --no-warn-script-location --quiet ^
        pymupdf Pillow pytesseract opencv-python-headless numpy scikit-learn ^
        fastapi "uvicorn[standard]" python-multipart websockets ^
        google-generativeai httpx pydantic pyyaml tqdm aiofiles

    echo PyTorch CPU 설치 중...
    "%PORTABLE_PYTHON%\python.exe" -m pip install --no-warn-script-location --quiet ^
        torch torchvision --index-url https://download.pytorch.org/whl/cpu 2>nul

    echo surya-ocr 설치 시도...
    "%PORTABLE_PYTHON%\python.exe" -m pip install --no-warn-script-location --quiet surya-ocr 2>nul

    echo Python 환경 준비 완료!
)

:: ──────────────────────────────────────────────
:: 2단계: Tauri 의존성 설치
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [2/4] Tauri CLI 확인 중...
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

where cargo-tauri >nul 2>&1
if %errorlevel% neq 0 (
    echo Tauri CLI 설치 중...
    cargo install tauri-cli --locked
)
echo Tauri CLI 준비 완료!

:: ──────────────────────────────────────────────
:: 3단계: Tauri 앱 빌드
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [3/4] Tauri 앱 빌드 중... (첫 빌드 시 5~10분)
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

cd /d "%PROJECT_ROOT%"
cargo tauri build

if %errorlevel% neq 0 (
    echo [오류] Tauri 빌드 실패!
    echo        오류 내용을 확인하고 다시 시도하세요.
    pause
    exit /b 1
)

:: ──────────────────────────────────────────────
:: 4단계: 포터블 Python을 설치파일에 포함
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [4/4] 포터블 Python 번들링...
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

:: tauri.conf.json의 resources에 이미 포함되어 있으므로
:: 빌드 시 자동으로 번들됨
echo 완료!

:: ──────────────────────────────────────────────
:: 완료!
:: ──────────────────────────────────────────────
echo.
echo  ╔══════════════════════════════════════════════════════════╗
echo  ║                                                          ║
echo  ║                    빌드 완료!                            ║
echo  ║                                                          ║
echo  ║  설치파일 위치:                                          ║
echo  ║  src-tauri\target\release\bundle\nsis\                   ║
echo  ║                                                          ║
echo  ║  포터블 실행파일:                                        ║
echo  ║  src-tauri\target\release\pdf-converter.exe              ║
echo  ║                                                          ║
echo  ║  Tauri 앱은 Electron 대비:                               ║
echo  ║  - 설치파일 10배 이상 작음                               ║
echo  ║  - 메모리 4~5배 적게 사용                                ║
echo  ║  - 시작 속도 3배 이상 빠름                               ║
echo  ║                                                          ║
echo  ╚══════════════════════════════════════════════════════════╝
echo.

explorer "%PROJECT_ROOT%src-tauri\target\release\bundle\nsis"
pause
