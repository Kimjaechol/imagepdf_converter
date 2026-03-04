# PDF 변환기 설치 및 사용 가이드 (초보자용)

## 목차
1. [필수 프로그램 설치](#1-필수-프로그램-설치)
2. [PDF 변환기 다운로드](#2-pdf-변환기-다운로드)
3. [Python 라이브러리 설치](#3-python-라이브러리-설치)
4. [데스크톱 앱 실행](#4-데스크톱-앱-실행)
5. [사용 방법](#5-사용-방법)
6. [모바일 앱 빌드](#6-모바일-앱-빌드)
7. [문제 해결](#7-문제-해결)

---

## 1. 필수 프로그램 설치

### Windows

1. **Python 3.10 이상 설치**
   - https://www.python.org/downloads/ 접속
   - "Download Python 3.12.x" 버튼 클릭
   - 설치할 때 반드시 "Add Python to PATH" 체크박스를 선택하세요
   - 설치 완료 후 명령 프롬프트(CMD)에서 확인:
     ```
     python --version
     ```

2. **Node.js 18 이상 설치**
   - https://nodejs.org/ 접속
   - "LTS" 버전 다운로드
   - 기본 설정으로 설치
   - 설치 확인:
     ```
     node --version
     npm --version
     ```

3. **Tesseract OCR 설치 (선택사항)**
   - https://github.com/UB-Mannheim/tesseract/wiki 에서 설치파일 다운로드
   - 설치 시 "Additional language data" 에서 "Korean" 선택
   - 환경변수 PATH에 Tesseract 경로 추가

### macOS

1. **Homebrew 설치** (터미널에서):
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

2. **Python, Node.js 설치**:
   ```bash
   brew install python@3.12 node
   ```

3. **Tesseract 설치 (선택사항)**:
   ```bash
   brew install tesseract tesseract-lang
   ```

### Linux (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv nodejs npm
# Tesseract (선택사항)
sudo apt install tesseract-ocr tesseract-ocr-kor
```

---

## 2. PDF 변환기 다운로드

### Git이 설치된 경우:
```bash
git clone https://github.com/Kimjaechol/imagepdf_converter.git
cd imagepdf_converter
```

### Git이 없는 경우:
- GitHub 페이지에서 "Code" > "Download ZIP" 클릭
- 압축 해제 후 해당 폴더로 이동

---

## 3. Python 라이브러리 설치

### Windows (명령 프롬프트 CMD 또는 PowerShell)

> **중요:** Windows에서는 `python3`이 아닌 **`python`** 명령을 사용합니다!

```cmd
:: 프로젝트 폴더로 이동
cd imagepdf_converter

:: 가상환경 생성
python -m venv venv

:: 가상환경 활성화
venv\Scripts\activate

:: 라이브러리 설치 (약 5-10분 소요)
pip install -r backend/requirements.txt
```

**`python`도 안 될 때:**
1. `where python` 실행하여 Python 경로 확인
2. Python이 설치되어 있지만 PATH에 없는 경우:
   - Windows 검색 → "환경 변수" → "환경 변수 편집"
   - Path에 Python 설치 경로 추가 (예: `C:\Users\사용자이름\AppData\Local\Programs\Python\Python312\`)
3. Poetry로 Python을 설치한 경우:
   ```cmd
   C:\Users\사용자이름\AppData\Roaming\pypoetry\venv\Scripts\python.exe -m venv venv
   venv\Scripts\activate
   pip install -r backend/requirements.txt
   ```

### macOS / Linux

```bash
# 프로젝트 폴더로 이동
cd imagepdf_converter

# 가상환경 생성
python3 -m venv venv

# 가상환경 활성화
source venv/bin/activate

# 라이브러리 설치 (약 5-10분 소요)
pip install -r backend/requirements.txt
```

**설치가 안 될 때:**
- `pip install --upgrade pip` 실행 후 다시 시도
- Windows에서 에러 시: Visual C++ Build Tools 설치 필요
  - https://visualstudio.microsoft.com/visual-cpp-build-tools/

---

## 4. 데스크톱 앱 실행

### 방법 1: 데스크톱 앱 (GUI)

```bash
# Electron 앱 의존성 설치
cd electron
npm install

# 앱 실행
npm start
```

앱이 자동으로 열리며, Python 백엔드 서버가 함께 시작됩니다.

### 방법 2: 명령줄 (CLI)

```bash
# 단일 파일 변환
python run_cli.py input.pdf -o output_folder/

# 폴더 내 모든 PDF 변환
python run_cli.py /path/to/pdfs/ -o output_folder/ --recursive

# HTML만 출력
python run_cli.py input.pdf -o output/ -f html

# Markdown만 출력
python run_cli.py input.pdf -o output/ -f markdown

# 워커 수 변경 (빠른 처리)
python run_cli.py input.pdf -o output/ --workers 8
```

---

## 5. 사용 방법

### 데스크톱 앱 사용 (3단계)

**1단계: 파일 선택**
- "단일 파일" 또는 "폴더 일괄 변환" 모드 선택
- 파일 탐색기에서 PDF를 앱으로 드래그 앤 드롭
- 또는 드롭 영역 클릭하여 파일 탐색기에서 선택

**2단계: 저장 위치 선택**
- "찾아보기" 버튼 클릭
- 변환 결과를 저장할 폴더 선택

**3단계: 변환 시작**
- 출력 형식 선택 (HTML, Markdown 또는 둘 다)
- "변환 시작" 버튼 클릭
- 오른쪽 "작업 목록"에서 진행률 확인
- 완료 후 "결과 폴더 열기" 클릭

### 설정 옵션 안내

| 설정 | 설명 | 추천 |
|------|------|------|
| OCR 엔진 | 텍스트 인식 엔진 | Surya (한국어 최적) |
| 읽기 순서 AI | 다단/표 읽기 순서 결정 | 하이브리드 |
| 오탈자 교정 | 갑을병정 등 교정 모드 | 하이브리드 |
| 동시 처리 수 | 병렬 처리 워커 수 | CPU 코어 수 (보통 4) |
| 청크 크기 | 한번에 처리할 페이지 수 | 10 |
| DPI | 이미지 해상도 | 300 |

---

## 6. 모바일 앱 빌드

### Android 빌드 (Capacitor)
```bash
cd mobile
npm install
npx cap add android
npx cap sync android
npx cap open android
# Android Studio에서 빌드
```

### iOS 빌드 (Capacitor, macOS 필요)
```bash
cd mobile
npm install
npx cap add ios
npx cap sync ios
npx cap open ios
# Xcode에서 빌드
```

---

## 7. 문제 해결

### "백엔드 오프라인" 표시될 때
1. Python 가상환경이 활성화되어 있는지 확인
2. 터미널에서 수동으로 서버 시작:
   ```bash
   python run_server.py
   ```
3. http://127.0.0.1:8765/api/health 접속하여 응답 확인

### OCR 인식률이 낮을 때
- DPI를 400으로 올려보세요
- OCR 엔진을 "하이브리드"로 변경
- 스캔 품질이 좋은 PDF를 사용하세요

### 한글이 깨질 때
- Tesseract 한국어 데이터가 설치되어 있는지 확인
- Surya OCR 엔진으로 변경

### 메모리 부족 에러
- 동시 처리 수(워커)를 줄이세요 (2-3개)
- 청크 크기를 5로 줄이세요
- DPI를 200으로 낮추세요

### Gemini API 관련
- VLM/LLM 모드 사용 시 필요
- https://aistudio.google.com/ 에서 API 키 발급
- 설정 탭에서 API 키 입력
- 무료 모드(규칙 기반 + 사전 교정)만으로도 기본 변환 가능
