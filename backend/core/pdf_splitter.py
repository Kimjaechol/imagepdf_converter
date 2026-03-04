"""Split a PDF file into fixed-size page chunks for parallel processing."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

from backend.models.schema import PdfChunk


class PdfSplitter:
    """Split PDF into N-page chunks and save each as a separate file."""

    def __init__(self, pages_per_chunk: int = 10):
        self.pages_per_chunk = pages_per_chunk

    def split(self, pdf_path: Path, work_dir: Path | None = None) -> list[PdfChunk]:
        """Split *pdf_path* into chunks.  Returns list of PdfChunk."""
        doc = fitz.open(str(pdf_path))
        total = len(doc)
        num_chunks = math.ceil(total / self.pages_per_chunk)

        if work_dir is None:
            work_dir = Path(tempfile.mkdtemp(prefix="pdfconv_"))
        work_dir.mkdir(parents=True, exist_ok=True)

        chunks: list[PdfChunk] = []
        for i in range(num_chunks):
            start = i * self.pages_per_chunk
            end = min(start + self.pages_per_chunk, total)

            chunk_pdf = fitz.open()  # new empty PDF
            chunk_pdf.insert_pdf(doc, from_page=start, to_page=end - 1)
            chunk_path = work_dir / f"chunk_{i:04d}_p{start+1:04d}_{end:04d}.pdf"
            chunk_pdf.save(str(chunk_path))
            chunk_pdf.close()

            chunks.append(PdfChunk(
                chunk_index=i,
                start_page=start,
                end_page=end,
                pdf_path=chunk_path,
                total_pages_in_doc=total,
            ))

        doc.close()
        return chunks

    def get_page_count(self, pdf_path: Path) -> int:
        doc = fitz.open(str(pdf_path))
        count = len(doc)
        doc.close()
        return count
