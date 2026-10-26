import hashlib
import json

import pypdfium2 as pdfium
import pytest
from PIL import Image

from backend.app.ingestion.pdf_reader import extract_pdf, save_extraction


def make_pdf(tmp_path, text=None):
    path = tmp_path / "report.pdf"
    document = pdfium.PdfDocument.new()
    page = document.new_page(300, 400)
    if text is not None:
        # A small ASCII fixture needs no system font or real analytical report.
        stream = (
            b"1 0 0 1 0 0 cm BT /F1 8 Tf 20 350 Td (" + text + b") Tj ET"
        )
        page.close()
        document.close()
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream",
        ]
        data = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for number, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
        xref = len(data)
        data.extend(b"xref\n0 6\n0000000000 65535 f \n")
        for offset in offsets[1:]:
            data.extend(f"{offset:010} 00000 n \n".encode())
        data.extend(
            f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        )
        path.write_bytes(data)
    else:
        document.save(path)
        page.close()
        document.close()
    return path


def test_native_text_is_preserved_without_running_ocr(tmp_path, monkeypatch):
    text = b"The bank expects stable prices and unchanged demand in September."
    path = make_pdf(tmp_path, text)

    def forbidden(*args):
        pytest.fail("OCR must not run for a page with sufficient native text")

    monkeypatch.setattr("backend.app.ingestion.pdf_reader._run_tesseract", forbidden)
    result = extract_pdf(path)
    page = result["pages"][0]
    assert text.decode() in page["text"]
    assert page["extraction_method"] == "native"
    assert page["page_number"] == 1
    assert page["text_sha256"] == hashlib.sha256(page["text"].encode()).hexdigest()
    assert result["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["requires_review"] is False


def test_scanned_page_is_rendered_and_ocr_text_is_preserved(tmp_path, monkeypatch):
    path = make_pdf(tmp_path)
    original = "Ключевая ставка не изменилась.\n14,0%\n"

    def fake_tesseract(command, timeout):
        if "--list-langs" in command:
            return "List of available languages (2):\nrus\neng\n"
        if "--version" in command:
            return "tesseract test\n"
        with Image.open(command[1]) as image:
            assert image.width > 1000
        return original

    monkeypatch.setattr("backend.app.ingestion.pdf_reader._run_tesseract", fake_tesseract)
    result = extract_pdf(path)
    assert result["pages"][0]["text"] == original
    assert result["pages"][0]["extraction_method"] == "ocr"
    assert result["requires_review"] is True
    saved = save_extraction(result, tmp_path / "output")
    assert json.loads(saved.read_text(encoding="utf-8")) == result
    assert saved.name == result["source_sha256"] + ".json"


def test_missing_russian_language_does_not_produce_an_empty_success(tmp_path, monkeypatch):
    path = make_pdf(tmp_path)
    monkeypatch.setattr(
        "backend.app.ingestion.pdf_reader._run_tesseract",
        lambda command, timeout: "List of available languages (1):\neng\n",
    )
    with pytest.raises(ValueError, match="rus"):
        extract_pdf(path)


def test_file_change_during_extraction_is_rejected(tmp_path, monkeypatch):
    path = make_pdf(tmp_path)

    def fake_tesseract(command, timeout):
        if "--list-langs" in command:
            return "rus\neng\n"
        if "--version" in command:
            return "tesseract test\n"
        with path.open("ab") as stream:
            stream.write(b"\n% file changed\n")
        return "Распознанный текст"

    monkeypatch.setattr("backend.app.ingestion.pdf_reader._run_tesseract", fake_tesseract)
    with pytest.raises(ValueError, match="изменился"):
        extract_pdf(path)


def test_completely_empty_ocr_is_an_error(tmp_path, monkeypatch):
    path = make_pdf(tmp_path)

    def fake_tesseract(command, timeout):
        if "--list-langs" in command:
            return "rus\neng\n"
        if "--version" in command:
            return "tesseract test\n"
        return "\n"

    monkeypatch.setattr("backend.app.ingestion.pdf_reader._run_tesseract", fake_tesseract)
    with pytest.raises(ValueError, match="Текст не распознан"):
        extract_pdf(path)