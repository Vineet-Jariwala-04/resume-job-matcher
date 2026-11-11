"""Reads text out of images (PNG / JPG / JPEG) and scanned PDFs.

Uses RapidOCR (`pip install rapidocr-onnxruntime`): pure pip, runs fully offline
on the CPU, no Tesseract install needed. It is loaded only when an image is
actually uploaded, so the rest of the app starts as fast as before.
"""
import io

MAX_IMAGE_BYTES = 10 * 1024 * 1024     # reject absurdly large uploads
MAX_PIXELS = 40_000_000                # guards against "decompression bomb" images
MAX_SIDE = 3000                        # bigger images are shrunk before OCR (faster, same accuracy)
TARGET_SIDE = 2400                     # small images are enlarged: the OCR drops word spaces on small text
SCANNED_PDF_MAX_PAGES = 4

_engine = None


class OCRUnavailable(RuntimeError):
    """The OCR package is not installed."""


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise OCRUnavailable(
                "Reading images needs the OCR package. Run:  pip install -r requirements.txt"
            ) from exc
        _engine = RapidOCR()
    return _engine


def _to_array(image_bytes: bytes):
    from PIL import Image, ImageOps
    import numpy as np

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("The image is too large (max 10 MB).")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("This file is not a valid PNG/JPG image.") from exc
    img = ImageOps.exif_transpose(img).convert("RGB")      # phone photos: fix rotation
    longest = max(img.size)
    if longest > MAX_SIDE:
        scale = MAX_SIDE / longest
    elif longest < TARGET_SIDE:
        scale = min(2.5, TARGET_SIDE / longest)
    else:
        scale = 1.0
    if abs(scale - 1.0) > 0.05:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    return np.array(img)


def _lines_from_boxes(items):
    """Turns OCR boxes [(box, text, score), ...] into text lines in reading order."""
    boxes = []
    for box, text, _score in items:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append({"x": min(xs), "y": (min(ys) + max(ys)) / 2, "h": max(ys) - min(ys), "text": str(text).strip()})
    boxes = [b for b in boxes if b["text"]]
    if not boxes:
        return []
    heights = sorted(b["h"] for b in boxes)
    tol = max(6.0, 0.6 * heights[len(heights) // 2])
    boxes.sort(key=lambda b: b["y"])
    lines, current = [], [boxes[0]]
    for b in boxes[1:]:
        if abs(b["y"] - sum(c["y"] for c in current) / len(current)) <= tol:
            current.append(b)
        else:
            lines.append(current)
            current = [b]
    lines.append(current)
    return ["  ".join(c["text"] for c in sorted(line, key=lambda c: c["x"])) for line in lines]


def extract_image_text(image_bytes: bytes) -> str:
    """Text of a PNG/JPG image, one line per text line. Raises ValueError if nothing readable."""
    array = _to_array(image_bytes)
    result, _ = _get_engine()(array)
    text = "\n".join(_lines_from_boxes(result or []))
    if len(text.strip()) < 15:
        raise ValueError("No readable text was found in the image. Use a sharper, higher-resolution picture.")
    return text


def extract_scanned_pdf_text(pdf_bytes: bytes) -> str:
    """OCR for a PDF that only contains page pictures (first few pages). '' if nothing found."""
    import fitz

    parts = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in list(doc)[:SCANNED_PDF_MAX_PAGES]:
            png = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
            try:
                parts.append(extract_image_text(png))
            except ValueError:
                continue
    return "\n".join(parts)
