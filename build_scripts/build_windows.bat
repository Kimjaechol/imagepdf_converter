@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ============================================================
echo   PDF 변환기 - Windows 설치파일 빌드 스크립트
echo ============================================================
echo.

:: ──────────────────────────────────────────────
:: 설정
:: ──────────────────────────────────────────────
set PYTHON_VERSION=3.11.9
set PYTHON_SHORT=311
set PROJECT_ROOT=%~dp0..
set BUILD_DIR=%PROJECT_ROOT%\build_output
set PORTABLE_DIR=%BUILD_DIR%\portable_python
set PYTHON_ZIP=python-%PYTHON_VERSION%-embed-amd64.zip
set PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/%PYTHON_ZIP%
set GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py

:: ──────────────────────────────────────────────
:: 1단계: 빌드 폴더 초기화
:: ──────────────────────────────────────────────
echo [1/7] 빌드 폴더 초기화...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
mkdir "%BUILD_DIR%"
mkdir "%PORTABLE_DIR%"

:: ──────────────────────────────────────────────
:: 2단계: Python Embeddable 다운로드
:: ──────────────────────────────────────────────
echo [2/7] Python %PYTHON_VERSION% Embeddable 다운로드 중...
cd /d "%BUILD_DIR%"

where curl >nul 2>&1
if %errorlevel%==0 (
    curl -L -o "%PYTHON_ZIP%" "%PYTHON_URL%"
) else (
    powershell -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_ZIP%'"
)

if not exist "%PYTHON_ZIP%" (
    echo [오류] Python 다운로드 실패!
    pause
    exit /b 1
)

:: ──────────────────────────────────────────────
:: 3단계: Python 압축 해제
:: ──────────────────────────────────────────────
echo [3/7] Python 압축 해제 중...
powershell -Command "Expand-Archive -Path '%PYTHON_ZIP%' -DestinationPath '%PORTABLE_DIR%' -Force"

:: pip 사용을 위해 python3XX._pth 파일 수정 (import site 활성화)
set PTH_FILE=%PORTABLE_DIR%\python%PYTHON_SHORT%._pth
echo python%PYTHON_SHORT%.zip> "%PTH_FILE%"
echo .>> "%PTH_FILE%"
echo Lib\site-packages>> "%PTH_FILE%"
echo import site>> "%PTH_FILE%"

:: ──────────────────────────────────────────────
:: 4단계: pip 설치
:: ──────────────────────────────────────────────
echo [4/7] pip 설치 중...
cd /d "%BUILD_DIR%"

where curl >nul 2>&1
if %errorlevel%==0 (
    curl -L -o get-pip.py "%GET_PIP_URL%"
) else (
    powershell -Command "Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile 'get-pip.py'"
)

"%PORTABLE_DIR%\python.exe" get-pip.py --no-warn-script-location

:: ──────────────────────────────────────────────
:: 5단계: Python 라이브러리 설치
:: ──────────────────────────────────────────────
echo [5/7] Python 라이브러리 설치 중 (약 5-10분 소요)...

:: 기본 패키지
"%PORTABLE_DIR%\python.exe" -m pip install --no-warn-script-location ^
    pymupdf ^
    Pillow ^
    pytesseract ^
    opencv-python-headless ^
    numpy ^
    scikit-learn ^
    fastapi ^
    "uvicorn[standard]" ^
    python-multipart ^
    websockets ^
    google-generativeai ^
    httpx ^
    pydantic ^
    pyyaml ^
    tqdm ^
    aiofiles ^
    stripe

if %errorlevel% neq 0 (
    echo [오류] 기본 패키지 설치 실패!
    pause
    exit /b 1
)

:: PyTorch CPU 버전 (가벼움)
echo [5.5/7] PyTorch CPU 버전 설치 중...
"%PORTABLE_DIR%\python.exe" -m pip install --no-warn-script-location ^
    torch torchvision --index-url https://download.pytorch.org/whl/cpu

:: surya-ocr (선택 - 실패해도 계속)
echo [5.7/7] surya-ocr 설치 시도 중...
"%PORTABLE_DIR%\python.exe" -m pip install --no-warn-script-location surya-ocr 2>nul
if %errorlevel% neq 0 (
    echo [안내] surya-ocr 설치 건너뜀 - Tesseract OCR로 대체 가능
)

:: ──────────────────────────────────────────────
:: 5.9단계: portable_python 용량 최적화
:: ──────────────────────────────────────────────
echo [5.9/7] portable_python 정리 중 (용량 최적화)...

:: __pycache__ 디렉토리 제거
for /d /r "%PORTABLE_DIR%" %%d in (__pycache__) do (
    if exist "%%d" rmdir /s /q "%%d"
)

:: .dist-info 디렉토리 제거
for /d /r "%PORTABLE_DIR%" %%d in (*.dist-info) do (
    if exist "%%d" rmdir /s /q "%%d"
)

:: 패키지 내 tests/test 디렉토리 제거
for /d /r "%PORTABLE_DIR%\Lib\site-packages" %%d in (tests test) do (
    if exist "%%d" rmdir /s /q "%%d"
)

:: pip 자체 제거 (배포 후 불필요)
if exist "%PORTABLE_DIR%\Lib\site-packages\pip" rmdir /s /q "%PORTABLE_DIR%\Lib\site-packages\pip"
if exist "%PORTABLE_DIR%\Scripts\pip*.exe" del /q "%PORTABLE_DIR%\Scripts\pip*.exe"

echo 정리 완료!

:: ──────────────────────────────────────────────
:: 6단계: 백엔드 코드 복사
:: ──────────────────────────────────────────────
echo [6/7] 백엔드 코드 복사 중...
mkdir "%BUILD_DIR%\app_backend"
xcopy /s /e /q /y "%PROJECT_ROOT%\backend" "%BUILD_DIR%\app_backend\backend\"
xcopy /s /e /q /y "%PROJECT_ROOT%\config" "%BUILD_DIR%\app_backend\config\"
copy /y "%PROJECT_ROOT%\run_server.py" "%BUILD_DIR%\app_backend\"
copy /y "%PROJECT_ROOT%\run_cli.py" "%BUILD_DIR%\app_backend\"

:: ──────────────────────────────────────────────
:: 7단계: Electron 앱 빌드
:: ──────────────────────────────────────────────
echo [7/7] Electron 앱 빌드 중...
cd /d "%PROJECT_ROOT%\electron"

:: node_modules 설치
call npm install

:: electron-builder로 빌드
call npm run build:win

echo.
echo ============================================================
echo   빌드 완료!
echo   설치파일 위치: %PROJECT_ROOT%\electron\dist\
echo ============================================================
pause
