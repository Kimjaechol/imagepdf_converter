# PDF → HTML/Markdown Converter

AI-powered PDF document converter that transforms both image PDFs (scanned documents requiring OCR) and digital PDFs into faithful HTML and Markdown output — preserving original layout, formatting, tables, multi-column structures, figures, footnotes, and annotations.

## Features

- **Layout-aware conversion**: Detects headings, paragraphs, tables, figures, equations, multi-column layouts, footnotes, balloons, and annotations
- **Multi-engine OCR**: Surya OCR (recommended) + Tesseract fallback, with Korean/Chinese/English support
- **Table structure recognition**: Handles visible borders, invisible/transparent borders, cell merging (rowspan/colspan), and multi-page table continuation
- **AI reading order**: Rule-based + VLM (Gemini/Ollama) hybrid for correct multi-column and complex layout reading sequence
- **Heading hierarchy**: Automatic heading level detection (H1-H6) based on font size, bold, alignment, and contextual analysis
- **Korean-optimized correction**: Dictionary-based + LLM post-OCR correction for Hanja (甲乙丙丁), Roman numerals, legal terms, and common OCR errors
- **Parallel processing**: PDFs split into 10-page chunks processed concurrently for maximum speed
- **Batch conversion**: Process entire folders of PDFs at once
- **Desktop app**: Electron-based GUI with real-time progress tracking
- **CLI mode**: Command-line interface for scripting and automation

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Electron Desktop App                  │
│              (File selection, Progress UI)                │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP/WebSocket
┌──────────────────────▼──────────────────────────────────┐
│                  FastAPI Backend Server                   │
│              (Job queue, Config, WebSocket)               │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                   Pipeline Orchestrator                   │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ PDF Split │→ │ Renderer │→ │ Layout   │              │
│  │ (10pg)   │  │ (300dpi) │  │ Detector │              │
│  └──────────┘  └──────────┘  └──────┬───┘              │
│                                      │                   │
│  ┌──────────┐  ┌──────────┐  ┌──────▼───┐              │
│  │ Image    │← │ Table    │← │  OCR     │              │
│  │ Extract  │  │ Recogn.  │  │ Engine   │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐                     │
│  │ Reading Order │→ │   Heading    │                     │
│  │ (Rule+VLM)   │  │ Classifier   │                     │
│  └──────────────┘  └──────┬───────┘                     │
│                            │                             │
│  ┌──────────────┐  ┌──────▼───────┐                     │
│  │  Correction  │→ │ HTML / MD    │                     │
│  │ (Dict+LLM)   │  │  Renderer   │                     │
│  └──────────────┘  └─────────────┘                      │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+ (for Electron desktop app)
- Tesseract OCR (optional, for fallback OCR)
- Ollama (optional, for local LLM heading/correction)

### 1. Install Python dependencies

```bash
pip install -r backend/requirements.txt
```

### 2. CLI Usage

```bash
# Single file
python run_cli.py input.pdf -o output/

# Batch folder
python run_cli.py /path/to/pdfs/ -o output/ --recursive

# Custom settings
python run_cli.py input.pdf -o output/ --workers 8 --dpi 400 -f html markdown
```

### 3. Desktop App

```bash
cd electron
npm install
npm start
```

### 4. API Server (standalone)

```bash
python run_server.py
# Server runs at http://127.0.0.1:8765
```

## Configuration

Edit `config/pipeline_config.yaml` to customize:

- OCR engine and languages
- Layout analysis confidence threshold
- Reading order mode (rule_based / hybrid / vlm)
- Heading classification mode
- Correction aggressiveness (conservative / moderate / aggressive)
- Parallel worker count
- Output format options

### AI Provider Setup

**Gemini (Cloud VLM for reading order + correction):**
```bash
export GEMINI_API_KEY="your-api-key"
```

**Ollama (Local LLM for heading classification + correction):**
```bash
# Install Ollama: https://ollama.com
ollama pull qwen2.5:0.5b-instruct  # For heading classification
ollama pull qwen2.5:1.5b            # For text correction
```

## Korean OCR Correction Dictionary

The file `config/correction_dict.json` contains:

- **Hanja corrections**: 甲乙丙丁戊己庚辛壬癸 and their commonly confused characters
- **Roman numerals**: Ⅰ-Ⅴ and similar-looking characters
- **Korean numbering**: (가)(나)(다)... patterns
- **Legal terms**: 채무자, 채권자, 판결 etc.
- **Common OCR errors**: 의/외, 를/릎, 은/운 etc.
- **Symbol corrections**: §, ①-⑤, ※, → etc.
- **User custom**: Add your own terms via API or UI

## Project Structure

```
imagepdf_converter/
├── backend/
│   ├── core/
│   │   ├── pipeline.py          # Main orchestrator
│   │   ├── pdf_splitter.py      # Split PDF into chunks
│   │   ├── page_renderer.py     # Render pages to images
│   │   ├── layout_detector.py   # Layout analysis (Surya)
│   │   ├── ocr_engine.py        # OCR (Surya/Tesseract)
│   │   ├── table_recognizer.py  # Table structure recognition
│   │   ├── reading_order.py     # Reading order refinement
│   │   ├── heading_classifier.py# Heading level detection
│   │   ├── correction.py        # Language correction
│   │   ├── image_extractor.py   # Extract figures/equations
│   │   ├── html_renderer.py     # Generate HTML output
│   │   ├── md_renderer.py       # Generate Markdown output
│   │   └── merger.py            # Merge chunk results
│   ├── models/
│   │   └── schema.py            # Data models / types
│   ├── services/
│   ├── utils/
│   │   ├── config_loader.py
│   │   └── image_utils.py
│   ├── server.py                # FastAPI backend server
│   └── requirements.txt
├── electron/
│   ├── src/
│   │   ├── main.js              # Electron main process
│   │   └── preload.js           # IPC bridge
│   ├── public/
│   │   └── index.html           # Desktop UI
│   └── package.json
├── config/
│   ├── pipeline_config.yaml     # Pipeline configuration
│   └── correction_dict.json     # Korean OCR correction dictionary
├── run_cli.py                   # CLI entry point
├── run_server.py                # Server entry point
└── README.md
```

## Processing Pipeline

1. **PDF Split**: Divide PDF into 10-page chunks for parallel processing
2. **Render**: Convert each page to 300 DPI PNG image
3. **Layout Detection**: Identify blocks (headings, paragraphs, tables, figures, etc.)
4. **OCR**: Extract text from image regions (skip digital text)
5. **Table Recognition**: Detect table structure (rows, columns, spans, borders)
6. **Image Extraction**: Crop and save figures, charts, equations
7. **Reading Order**: Determine natural reading sequence (rule-based + VLM)
8. **Heading Classification**: Assign heading levels (H1-H6) based on style + LLM
9. **Text Correction**: Dictionary-based + LLM context-aware OCR error correction
10. **Merge**: Combine chunk results, merge multi-page tables
11. **Render**: Generate HTML and/or Markdown output
