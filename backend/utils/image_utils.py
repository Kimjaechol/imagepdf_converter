"""Image processing helper utilities."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


def ensure_rgb(img: Image.Image) -> Image.Image:
    """Convert image to RGB if needed."""
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def crop_region(
    img: Image.Image,
    x0: int, y0: int, x1: int, y1: int,
) -> Image.Image:
    """Safely crop a region from an image, clamping to image bounds."""
    w, h = img.size
    x0 = max(0, min(x0, w))
    y0 = max(0, min(y0, h))
    x1 = max(0, min(x1, w))
    y1 = max(0, min(y1, h))
    if x1 <= x0 or y1 <= y0:
        return Image.new("RGB", (1, 1), (255, 255, 255))
    return img.crop((x0, y0, x1, y1))


def image_to_numpy(img: Image.Image) -> np.ndarray:
    """Convert PIL Image to numpy array (RGB)."""
    return np.array(ensure_rgb(img))


def is_mostly_blank(img: Image.Image, threshold: float = 0.95) -> bool:
    """Check if an image is mostly white / blank."""
    arr = np.array(img.convert("L"))
    white_ratio = np.mean(arr > 240)
    return white_ratio > threshold


def save_image(img: Image.Image, path: Path, quality: int = 90) -> None:
    """Save image, auto-detecting format from extension."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        img.save(str(path), "JPEG", quality=quality)
    else:
        img.save(str(path), "PNG")
