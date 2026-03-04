#!/bin/bash
set -e

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║       MoA 문서 변환기 - 빌드 스크립트 (macOS/Linux)     ║"
echo "║                                                          ║"
echo "║  필요: Rust, Python 3.10+                                ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Check Rust
if ! command -v rustc &> /dev/null; then
    echo "[오류] Rust가 설치되어 있지 않습니다!"
    echo "       curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    exit 1
fi
echo "[확인] $(rustc --version)"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "[오류] Python3가 설치되어 있지 않습니다!"
    exit 1
fi
echo "[확인] $(python3 --version)"

# Install Python deps
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " [1/3] Python 라이브러리 설치 중..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

pip3 install --quiet \
    pymupdf Pillow pytesseract opencv-python-headless numpy scikit-learn \
    fastapi 'uvicorn[standard]' python-multipart websockets \
    google-generativeai httpx pydantic pyyaml tqdm aiofiles

pip3 install --quiet torch torchvision --index-url https://download.pytorch.org/whl/cpu 2>/dev/null || true
pip3 install --quiet surya-ocr 2>/dev/null || true

# Install Tauri CLI
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " [2/3] Tauri CLI 확인..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if ! command -v cargo-tauri &> /dev/null; then
    echo "Tauri CLI 설치 중..."
    cargo install tauri-cli --locked
fi

# Build
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " [3/3] Tauri 앱 빌드 중..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

cd "$PROJECT_ROOT"
cargo tauri build

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  빌드 완료!                                              ║"
echo "║                                                          ║"
echo "║  결과물: src-tauri/target/release/bundle/                ║"
echo "╚══════════════════════════════════════════════════════════╝"
