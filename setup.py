"""Setup configuration for the PDF converter package."""

from setuptools import setup, find_packages

setup(
    name="pdf-to-html-md-converter",
    version="1.0.0",
    description="AI-powered PDF to HTML/Markdown converter",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "pymupdf>=1.25.3",
        "Pillow>=9.0.0",
        "pytesseract>=0.3.10",
        "opencv-python-headless>=4.8.0",
        "numpy>=1.24.0",
        "scikit-learn>=1.3.0",
        "fastapi>=0.100.0",
        "uvicorn[standard]>=0.23.0",
        "python-multipart>=0.0.6",
        "websockets>=11.0",
        "google-generativeai>=0.8.0",
        "httpx>=0.24.0",
        "pydantic>=2.0.0",
        "pyyaml>=6.0",
        "tqdm>=4.65.0",
        "aiofiles>=23.0.0",
        "stripe>=7.0.0",
    ],
    entry_points={
        "console_scripts": [
            "pdfconv=run_cli:main",
            "pdfconv-server=run_server:main",
        ],
    },
)
