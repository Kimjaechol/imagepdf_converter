@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║     PDF 변환기 - 원클릭 설치파일 빌드 스크립트      ║
echo  ║                                                      ║
echo  ║  이 스크립트가 자동으로:                             ║
echo  ║  1. Python 포터블 버전 다운로드                      ║
echo  ║  2. 모든 라이브러리 설치                             ║
echo  ║  3. 설치파일(.exe) 생성                              ║
echo  ║                                                      ║
echo  ║  필요 사항: Node.js 18+ (https://nodejs.org)         ║
echo  ╚══════════════════════════════════════════════════════╝
echo.

:: ──────────────────────────────────────────────
:: 사전 확인
:: ──────────────────────────────────────────────
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo [오류] Node.js가 설치되어 있지 않습니다!
    echo        https://nodejs.org 에서 LTS 버전을 설치해주세요.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('node --version') do set NODE_VER=%%v
echo [확인] Node.js %NODE_VER% 감지됨

:: ──────────────────────────────────────────────
:: 설정
:: ──────────────────────────────────────────────
set PYTHON_VERSION=3.11.9
set PYTHON_SHORT=311
set PROJECT_ROOT=%~dp0
set BUILD_DIR=%PROJECT_ROOT%build_output
set PORTABLE_DIR=%BUILD_DIR%\portable_python
set BACKEND_DIR=%BUILD_DIR%\app_backend
set PYTHON_ZIP=python-%PYTHON_VERSION%-embed-amd64.zip
set PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/%PYTHON_ZIP%

:: ──────────────────────────────────────────────
:: 1단계: 빌드 폴더 초기화
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [1/6] 빌드 폴더 초기화...
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if exist "%BUILD_DIR%" (
    echo 기존 빌드 폴더 삭제 중...
    rmdir /s /q "%BUILD_DIR%"
)
mkdir "%BUILD_DIR%"
mkdir "%PORTABLE_DIR%"
mkdir "%BACKEND_DIR%"
echo 완료!

:: ──────────────────────────────────────────────
:: 2단계: Python Embeddable 다운로드 및 설치
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [2/6] Python %PYTHON_VERSION% 다운로드 중...
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

powershell -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%BUILD_DIR%\%PYTHON_ZIP%'"

if not exist "%BUILD_DIR%\%PYTHON_ZIP%" (
    echo [오류] Python 다운로드 실패! 인터넷 연결을 확인하세요.
    pause
    exit /b 1
)

echo 압축 해제 중...
powershell -Command "Expand-Archive -Path '%BUILD_DIR%\%PYTHON_ZIP%' -DestinationPath '%PORTABLE_DIR%' -Force"
del "%BUILD_DIR%\%PYTHON_ZIP%"

:: python._pth 수정 - pip 사용을 위해 import site 활성화
echo python%PYTHON_SHORT%.zip> "%PORTABLE_DIR%\python%PYTHON_SHORT%._pth"
echo .>> "%PORTABLE_DIR%\python%PYTHON_SHORT%._pth"
echo Lib\site-packages>> "%PORTABLE_DIR%\python%PYTHON_SHORT%._pth"
echo import site>> "%PORTABLE_DIR%\python%PYTHON_SHORT%._pth"

:: pip 설치
echo pip 설치 중...
powershell -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%BUILD_DIR%\get-pip.py'"
"%PORTABLE_DIR%\python.exe" "%BUILD_DIR%\get-pip.py" --no-warn-script-location >nul 2>&1
del "%BUILD_DIR%\get-pip.py"
echo Python 설치 완료!

:: ──────────────────────────────────────────────
:: 3단계: Python 라이브러리 설치
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [3/6] Python 라이브러리 설치 중...
echo         (약 5~15분 소요 - 인터넷 속도에 따라 다름)
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo [3a] 기본 패키지 설치 중...
"%PORTABLE_DIR%\python.exe" -m pip install --no-warn-script-location --quiet ^
    pymupdf Pillow pytesseract ^
    opencv-python-headless numpy scikit-learn ^
    fastapi "uvicorn[standard]" python-multipart websockets ^
    google-generativeai httpx ^
    pydantic pyyaml tqdm aiofiles

if %errorlevel% neq 0 (
    echo [오류] 기본 패키지 설치 실패!
    pause
    exit /b 1
)
echo 기본 패키지 설치 완료!

echo [3b] PyTorch CPU 설치 중... (약 200MB 다운로드)
"%PORTABLE_DIR%\python.exe" -m pip install --no-warn-script-location --quiet ^
    torch torchvision --index-url https://download.pytorch.org/whl/cpu

if %errorlevel% neq 0 (
    echo [경고] PyTorch 설치 실패 - AI 기능 없이 계속합니다
) else (
    echo PyTorch 설치 완료!
)

echo [3c] surya-ocr 설치 시도 중...
"%PORTABLE_DIR%\python.exe" -m pip install --no-warn-script-location --quiet surya-ocr 2>nul
if %errorlevel% neq 0 (
    echo [안내] surya-ocr 건너뜀 (Tesseract로 대체)
) else (
    echo surya-ocr 설치 완료!
)

:: ──────────────────────────────────────────────
:: 4단계: 백엔드 코드 복사
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [4/6] 백엔드 코드 복사 중...
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

xcopy /s /e /q /y "%PROJECT_ROOT%backend" "%BACKEND_DIR%\backend\" >nul
xcopy /s /e /q /y "%PROJECT_ROOT%config" "%BACKEND_DIR%\config\" >nul
if exist "%PROJECT_ROOT%run_server.py" copy /y "%PROJECT_ROOT%run_server.py" "%BACKEND_DIR%\" >nul
if exist "%PROJECT_ROOT%run_cli.py" copy /y "%PROJECT_ROOT%run_cli.py" "%BACKEND_DIR%\" >nul
echo 완료!

:: ──────────────────────────────────────────────
:: 5단계: Electron 의존성 설치
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [5/6] Electron 앱 의존성 설치 중...
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

cd /d "%PROJECT_ROOT%electron"
call npm install --silent 2>nul
if %errorlevel% neq 0 (
    echo npm install 재시도...
    call npm install
)
echo 완료!

:: ──────────────────────────────────────────────
:: 6단계: 설치파일 빌드
:: ──────────────────────────────────────────────
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  [6/6] 설치파일 빌드 중...
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

call npx electron-builder --win --x64

if %errorlevel% neq 0 (
    echo [오류] 설치파일 빌드 실패!
    pause
    exit /b 1
)

:: ──────────────────────────────────────────────
:: 완료!
:: ──────────────────────────────────────────────
echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║                                                      ║
echo  ║               빌드 완료!                             ║
echo  ║                                                      ║
echo  ║   설치파일 위치:                                     ║
echo  ║   electron\dist\PDF-변환기-Setup-1.0.0.exe          ║
echo  ║                                                      ║
echo  ║   이 파일을 다른 사람에게 공유하면                   ║
echo  ║   원클릭으로 설치하여 바로 사용할 수 있습니다!      ║
echo  ║                                                      ║
echo  ╚══════════════════════════════════════════════════════╝
echo.

:: 결과 폴더 열기
explorer "%PROJECT_ROOT%electron\dist"
pause
