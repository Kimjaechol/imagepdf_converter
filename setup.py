"""Setup configuration for the PDF converter package."""

from setuptools import setup, find_packages

setup(
    name="pdf-to-html-md-converter",
    version="1.0.0",
    description="AI-powered PDF to HTML/Markdown converter",
    packages=find_packages(),
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "pdfconv=run_cli:main",
            "pdfconv-server=run_server:main",
        ],
    },
)
