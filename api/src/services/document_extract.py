# /Users/johnpellew/agentic-accounting-v3/api/src/services/document_extract.py

"""Contract F38 — document text extraction (PDF / JPG / PNG → plain text).

The uploaded document NEVER goes to the LLM as an image: this module turns
it into text, and the text rides the SAME classify → construct → preview
pipeline as a chat message, so the preview/approve flow is unchanged and
the LLM never does the money math.

Pipeline:

- ``.pdf`` → ``pdftotext -layout <file> -`` via subprocess (the deploy box
  ships ``/usr/bin/pdftotext``; timeout :data:`PDFTOTEXT_TIMEOUT_SECONDS`).
  A blank/short result (typically a scanned PDF) falls back to pypdf:
  extract the embedded images → OCR each with pytesseract → concatenate.
- ``.jpg/.jpeg/.png`` → PIL open → ``pytesseract.image_to_string``.

The optional dependencies (pypdf, pytesseract, PIL) are imported AT CALL
TIME; a missing library raises a structured :class:`DocumentExtractError`
with ``type="ocr_unavailable"`` — never a crash.  Nothing is persisted:
extraction is in-memory / a single tempfile that is always removed.
"""

from __future__ import annotations

import importlib
import io
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "IMAGE_EXTENSIONS",
    "MIN_PDF_TEXT_CHARS",
    "MAX_OCR_PAGES",
    "PDFTOTEXT_TIMEOUT_SECONDS",
    "DocumentExtractError",
    "extract_document_text",
]

#: pdftotext subprocess timeout (seconds) — the route's request budget is
#: larger, so a hung binary fails structured instead of hanging the proxy.
PDFTOTEXT_TIMEOUT_SECONDS = 20

#: Below this many non-whitespace characters a PDF is treated as
#: scanned/blank and the embedded-image OCR pass runs.
MIN_PDF_TEXT_CHARS = 40
MAX_OCR_PAGES = 20  # verifier fix-list: bounded OCR budget

#: OCR'd image formats (extension → PIL/pytesseract path).
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


class DocumentExtractError(Exception):
    """Structured extraction failure — the route maps it to 422.

    ``type`` is ``document_unreadable`` (no text / corrupt file / tool
    failed) or ``ocr_unavailable`` (an extraction dependency is missing on
    this deployment).
    """

    def __init__(self, message: str, type: str = "document_unreadable") -> None:
        super().__init__(message)
        self.message = message
        self.type = type


def _extension(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _pdftotext_binary() -> str | None:
    """The pdftotext binary: /usr/bin first (the deploy box), then PATH."""
    return shutil.which("pdftotext", path="/usr/bin") or shutil.which("pdftotext")


def _require(module_name: str) -> Any:
    """Import an optional dependency at call time.

    Missing library → structured ``ocr_unavailable`` (never an ImportError
    escaping into the route).
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise DocumentExtractError(
            f"{module_name} is not installed — document OCR is unavailable "
            "on this deployment",
            "ocr_unavailable",
        ) from exc


def _pdftotext(data: bytes) -> str:
    """Run ``pdftotext -layout <tmpfile> -`` and return its stdout text."""
    binary = _pdftotext_binary()
    if binary is None:
        raise DocumentExtractError(
            "pdftotext binary not found — PDF text extraction is unavailable "
            "on this deployment",
            "ocr_unavailable",
        )

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(data)
        try:
            completed = subprocess.run(
                [binary, "-layout", tmp_path, "-"],
                capture_output=True,
                timeout=PDFTOTEXT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentExtractError(
                f"pdftotext timed out after {PDFTOTEXT_TIMEOUT_SECONDS}s",
                "document_unreadable",
            ) from exc
        except OSError as exc:
            raise DocumentExtractError(
                f"pdftotext could not run: {exc.__class__.__name__}: {exc}",
                "document_unreadable",
            ) from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or b"").decode(
                "utf-8", errors="replace"
            ).strip()
            raise DocumentExtractError(
                f"pdftotext failed (exit {completed.returncode}): "
                f"{stderr or 'no stderr'}",
                "document_unreadable",
            )
        return (completed.stdout or b"").decode("utf-8", errors="replace")
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:  # best-effort cleanup only
                logger.warning("could not remove extraction tempfile %s", tmp_path)


def _ocr_pil_image(pytesseract: Any, image: Any) -> str:
    """OCR one PIL image; a missing tesseract binary is ``ocr_unavailable``."""
    try:
        return str(pytesseract.image_to_string(image) or "")
    except Exception as exc:  # never leak a raw OCR crash
        error_type = (
            "ocr_unavailable"
            if "tesseractnotfound" in exc.__class__.__name__.lower()
            else "document_unreadable"
        )
        raise DocumentExtractError(
            f"OCR failed: {exc.__class__.__name__}: {exc}", error_type
        ) from exc


def _ocr_image(data: bytes) -> str:
    """jpg/jpeg/png → PIL open → pytesseract.image_to_string."""
    Image = _require("PIL.Image")
    pytesseract = _require("pytesseract")
    try:
        image = Image.open(io.BytesIO(data))
    except DocumentExtractError:
        raise
    except Exception as exc:  # corrupt image, not a crash
        raise DocumentExtractError(
            f"image could not be read: {exc.__class__.__name__}: {exc}",
            "document_unreadable",
        ) from exc
    return _ocr_pil_image(pytesseract, image)


def _ocr_pdf_images(data: bytes) -> str:
    """Scanned-PDF fallback: pypdf embedded images → pytesseract each."""
    pypdf = _require("pypdf")
    pytesseract = _require("pytesseract")
    Image = _require("PIL.Image")

    chunks: list[str] = []
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        for page in reader.pages[:MAX_OCR_PAGES]:
            for image_file in getattr(page, "images", []) or []:
                image = Image.open(io.BytesIO(image_file.data))
                chunks.append(_ocr_pil_image(pytesseract, image))
    except DocumentExtractError:
        raise
    except Exception as exc:  # corrupt PDF, not a crash
        raise DocumentExtractError(
            f"embedded-image extraction failed: {exc.__class__.__name__}: {exc}",
            "document_unreadable",
        ) from exc
    return "\n".join(chunks)


def extract_document_text(filename: str, data: bytes) -> str:
    """Extract the plain text of an uploaded document (Contract F38).

    ``.pdf`` → pdftotext; blank/short output → pypdf images + OCR.
    ``.jpg/.jpeg/.png`` → PIL + pytesseract.  Raises
    :class:`DocumentExtractError` (``document_unreadable`` /
    ``ocr_unavailable``) — never a raw exception.  Nothing is persisted.
    """
    extension = _extension(filename)

    if extension == ".pdf":
        text = _pdftotext(data)
        if len(text.strip()) >= MIN_PDF_TEXT_CHARS:
            return text
        logger.info(
            "pdftotext returned %d chars for %r — trying embedded-image OCR",
            len(text.strip()),
            filename,
        )
        ocr_text = _ocr_pdf_images(data)
        if not ocr_text.strip():
            if text.strip():
                # Verifier fix-list: a short-but-valid PDF (e.g. a sparse
                # one-page note) keeps its real text when OCR finds nothing.
                return text
            raise DocumentExtractError(
                "no readable text found in the PDF (it may be blank or the "
                "scan unreadable)",
                "document_unreadable",
            )
        return ocr_text

    if extension in IMAGE_EXTENSIONS:
        text = _ocr_image(data)
        if not text.strip():
            raise DocumentExtractError(
                "OCR found no readable text in the image",
                "document_unreadable",
            )
        return text

    raise DocumentExtractError(
        f"unsupported document type {extension or filename!r} — expected "
        "pdf, jpg, jpeg or png",
        "document_unreadable",
    )
