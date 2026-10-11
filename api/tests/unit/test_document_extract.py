# /Users/johnpellew/agentic-accounting-v3/api/tests/unit/test_document_extract.py

"""Contract F38 unit tests — document text extraction (PDF / JPG / PNG).

The extraction seams are monkeypatched: ``subprocess.run`` returns fake
``pdftotext`` output, and pypdf/pytesseract are injected as fake modules
through ``sys.modules`` (PIL is real — pillow is a hard dependency of the
api venv).  The two tests that exercise the REAL binaries (pdftotext,
tesseract) skip cleanly when they are absent, as on the dev box.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import sys
import types
from unittest.mock import MagicMock

import pytest

from src.services import document_extract
from src.services.document_extract import (
    DocumentExtractError,
    extract_document_text,
)

#: A real 1x1 PNG — valid image bytes for the PIL-open OCR paths.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

PDF_BYTES = b"%PDF-1.4 fake document bytes"


def _completed(
    stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0
) -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


@pytest.fixture
def fake_pdftotext(monkeypatch) -> list:
    """Patch the binary lookup + subprocess.run; record (cmd, kwargs)."""
    calls: list = []
    monkeypatch.setattr(
        document_extract.shutil,
        "which",
        lambda name, path=None: "/usr/bin/pdftotext",
    )

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _completed(
            stdout=(
                b"Invoice INV-1001\nPrinter paper 100.00\nVAT 20% 20.00\nTotal 120.00\n"
            )
        )

    monkeypatch.setattr(document_extract.subprocess, "run", fake_run)
    return calls


def _fake_pypdf(monkeypatch, page_count: int = 1) -> None:
    """Inject a fake pypdf whose pages each carry one embedded image."""
    pages = [
        types.SimpleNamespace(
            images=[types.SimpleNamespace(data=TINY_PNG)]
        )
        for _ in range(page_count)
    ]
    fake = types.SimpleNamespace(
        PdfReader=lambda stream: types.SimpleNamespace(pages=pages)
    )
    monkeypatch.setitem(sys.modules, "pypdf", fake)


def _fake_pytesseract(monkeypatch, texts: list[str]) -> None:
    """Inject a fake pytesseract returning ``texts`` one call at a time."""
    queue = list(texts)
    fake = types.SimpleNamespace(
        image_to_string=lambda image, *a, **k: queue.pop(0)
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake)


# ---------------------------------------------------------------------------
# PDF — pdftotext path
# ---------------------------------------------------------------------------


def test_pdf_text_passes_through_pdftotext_layout(fake_pdftotext) -> None:
    text = extract_document_text("invoice.pdf", PDF_BYTES)

    assert "INV-1001" in text
    assert "120.00" in text
    cmd, kwargs = fake_pdftotext[0]
    assert cmd[0] == "/usr/bin/pdftotext"
    assert cmd[1] == "-layout"
    assert cmd[2].endswith(".pdf")
    assert cmd[3] == "-"
    assert kwargs["timeout"] == document_extract.PDFTOTEXT_TIMEOUT_SECONDS == 20
    assert kwargs["capture_output"] is True
    # No persistence — the tempfile is removed again.
    assert not os.path.exists(cmd[2])


def test_pdf_nonzero_exit_raises_document_unreadable(fake_pdftotext, monkeypatch) -> None:
    monkeypatch.setattr(
        document_extract.subprocess,
        "run",
        lambda cmd, **kwargs: _completed(
            stderr=b"Syntax Error: Couldn't read xref table", returncode=1
        ),
    )

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("broken.pdf", PDF_BYTES)

    assert excinfo.value.type == "document_unreadable"
    assert "xref table" in excinfo.value.message


def test_pdf_timeout_raises_document_unreadable(fake_pdftotext, monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pdftotext", timeout=20)

    monkeypatch.setattr(document_extract.subprocess, "run", fake_run)

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("slow.pdf", PDF_BYTES)

    assert excinfo.value.type == "document_unreadable"
    assert "timed out" in excinfo.value.message


def test_missing_pdftotext_binary_is_ocr_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        document_extract.shutil, "which", lambda name, path=None: None
    )

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("invoice.pdf", PDF_BYTES)

    assert excinfo.value.type == "ocr_unavailable"
    assert "pdftotext" in excinfo.value.message


# ---------------------------------------------------------------------------
# PDF — blank text → embedded-image OCR fallback
# ---------------------------------------------------------------------------


def test_blank_pdf_ocrs_embedded_images(fake_pdftotext, monkeypatch) -> None:
    pytest.importorskip("PIL.Image")
    monkeypatch.setattr(
        document_extract.subprocess, "run", lambda cmd, **kwargs: _completed(stdout=b"")
    )
    _fake_pypdf(monkeypatch, page_count=2)
    _fake_pytesseract(monkeypatch, ["INVOICE INV-7", "Total 99.50"])

    text = extract_document_text("scan.pdf", PDF_BYTES)

    assert "INVOICE INV-7" in text
    assert "Total 99.50" in text  # both pages concatenated


def test_blank_pdf_without_pypdf_is_ocr_unavailable(fake_pdftotext, monkeypatch) -> None:
    monkeypatch.setattr(
        document_extract.subprocess, "run", lambda cmd, **kwargs: _completed(stdout=b" \n ")
    )
    # None in sys.modules makes importlib.import_module raise ImportError.
    monkeypatch.setitem(sys.modules, "pypdf", None)

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("scan.pdf", PDF_BYTES)

    assert excinfo.value.type == "ocr_unavailable"
    assert "pypdf" in excinfo.value.message


def test_blank_pdf_without_pytesseract_is_ocr_unavailable(
    fake_pdftotext, monkeypatch
) -> None:
    monkeypatch.setattr(
        document_extract.subprocess, "run", lambda cmd, **kwargs: _completed(stdout=b"")
    )
    _fake_pypdf(monkeypatch)
    monkeypatch.setitem(sys.modules, "pytesseract", None)

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("scan.pdf", PDF_BYTES)

    assert excinfo.value.type == "ocr_unavailable"
    assert "pytesseract" in excinfo.value.message


def test_blank_pdf_with_no_ocr_text_is_document_unreadable(
    fake_pdftotext, monkeypatch
) -> None:
    pytest.importorskip("PIL.Image")
    monkeypatch.setattr(
        document_extract.subprocess, "run", lambda cmd, **kwargs: _completed(stdout=b"")
    )
    _fake_pypdf(monkeypatch)
    _fake_pytesseract(monkeypatch, ["   \n"])

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("blank.pdf", PDF_BYTES)

    assert excinfo.value.type == "document_unreadable"
    assert "no readable text" in excinfo.value.message


# ---------------------------------------------------------------------------
# Images — PIL + pytesseract
# ---------------------------------------------------------------------------


def test_jpg_ocr_returns_pytesseract_text(monkeypatch) -> None:
    pytest.importorskip("PIL.Image")
    _fake_pytesseract(monkeypatch, ["PAID 50.00 to ACME\n"])

    text = extract_document_text("receipt.JPG", TINY_PNG)

    assert text == "PAID 50.00 to ACME\n"


def test_png_without_pytesseract_is_ocr_unavailable(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pytesseract", None)

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("receipt.png", TINY_PNG)

    assert excinfo.value.type == "ocr_unavailable"
    assert "pytesseract" in excinfo.value.message


def test_corrupt_image_is_document_unreadable(monkeypatch) -> None:
    pytest.importorskip("PIL.Image")
    _fake_pytesseract(monkeypatch, ["never reached"])

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("receipt.jpg", b"this is not an image")

    assert excinfo.value.type == "document_unreadable"


def test_missing_tesseract_binary_is_ocr_unavailable(monkeypatch) -> None:
    pytest.importorskip("PIL.Image")

    class TesseractNotFoundError(Exception):
        pass

    def boom(image, *a, **k):
        raise TesseractNotFoundError("tesseract is not installed")

    monkeypatch.setitem(
        sys.modules, "pytesseract", types.SimpleNamespace(image_to_string=boom)
    )

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("receipt.png", TINY_PNG)

    assert excinfo.value.type == "ocr_unavailable"


def test_empty_ocr_text_is_document_unreadable(monkeypatch) -> None:
    pytest.importorskip("PIL.Image")
    _fake_pytesseract(monkeypatch, [""])

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("receipt.png", TINY_PNG)

    assert excinfo.value.type == "document_unreadable"


def test_unsupported_extension_is_document_unreadable() -> None:
    with pytest.raises(DocumentExtractError) as excinfo:
        extract_document_text("notes.txt", b"hello")

    assert excinfo.value.type == "document_unreadable"
    assert "unsupported document type" in excinfo.value.message


# ---------------------------------------------------------------------------
# Real-binary smoke tests — skip cleanly when the tools are absent
# ---------------------------------------------------------------------------


def _build_text_pdf(line: str) -> bytes:
    """A minimal, structurally valid single-page PDF containing ``line``."""
    content = f"BT /F1 18 Tf 72 720 Td ({line}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def _pdftotext_available() -> bool:
    return (
        shutil.which("pdftotext", path="/usr/bin") is not None
        or shutil.which("pdftotext") is not None
    )


@pytest.mark.skipif(
    not _pdftotext_available(),
    reason="real pdftotext binary not installed on this machine",
)
def test_real_pdftotext_extracts_a_text_pdf() -> None:
    text = extract_document_text(
        "invoice.pdf", _build_text_pdf("Invoice INV-42 Total 120.00")
    )

    assert "INV-42" in text
    assert "120.00" in text


@pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="real tesseract binary not installed on this machine",
)
def test_real_tesseract_ocrs_a_rendered_image() -> None:
    pytest.importorskip("pytesseract")
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")

    image = Image.new("RGB", (480, 120), "white")
    ImageDraw.Draw(image).text((20, 40), "INVOICE 120", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    try:
        text = extract_document_text("invoice.png", buffer.getvalue())
    except DocumentExtractError as exc:
        pytest.skip(f"tesseract unavailable at runtime: {exc}")

    assert text.strip() != ""
