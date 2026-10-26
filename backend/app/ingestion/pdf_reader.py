"""Extract page text locally; OCR output remains subject to human review."""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

import pypdfium2 as pdfium


logger = logging.getLogger(__name__)


def _run_tesseract(command: list[str], timeout: float) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except FileNotFoundError as error:
        raise ValueError(
            "Tesseract не найден. Установите его и укажите --tesseract."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("Превышено время работы Tesseract.") from error

    if result.returncode:
        raise ValueError(f"Ошибка Tesseract: {result.stderr.strip()[:500]}")
    return result.stdout


def extract_pdf(
    path: Path,
    *,
    tesseract: str = "tesseract",
    tessdata_dir: Path | None = None,
    language: str = "rus+eng",
    dpi: int = 300,
    timeout: float = 120,
) -> dict:
    """Keep the original extracted text, page numbers and SHA-256 hashes.

    PDF metadata and filenames are deliberately not used as publication dates.
    OCR does not verify the source or the accuracy of numbers in its text.
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Файл не найден: {path}")
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("PDF превышает ограничение 100 МБ.")
    if not 150 <= dpi <= 400:
        raise ValueError("Разрешение должно быть от 150 до 400 dpi.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Тайм-аут должен быть положительным числом.")
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", language):
        raise ValueError("Некорректный список языков OCR.")

    with path.open("rb") as stream:
        source_hash = hashlib.file_digest(stream, "sha256").hexdigest()

    ocr_options = []
    if tessdata_dir is not None:
        ocr_options = ["--tessdata-dir", str(tessdata_dir.resolve())]

    pages = []
    ocr_version = None
    with closing(pdfium.PdfDocument(path)) as document:
        if not 1 <= len(document) <= 300:
            raise ValueError("Поддерживаются PDF от 1 до 300 страниц.")

        with tempfile.TemporaryDirectory(prefix="alfa-pdf-") as temporary:
            image_path = Path(temporary) / "page.png"
            for index in range(len(document)):
                with closing(document[index]) as page:
                    with closing(page.get_textpage()) as text_page:
                        text = text_page.get_text_bounded()
                    method = "native"

                    if sum(character.isalnum() for character in text) < 40:
                        if ocr_version is None:
                            available = _run_tesseract(
                                [tesseract, "--list-langs", *ocr_options], 20
                            ).splitlines()
                            missing = set(language.split("+")) - {
                                line.strip() for line in available
                            }
                            if missing:
                                raise ValueError(
                                    "Не установлены языки Tesseract: "
                                    + ", ".join(sorted(missing))
                                )
                            ocr_version = _run_tesseract(
                                [tesseract, "--version"], 20
                            ).splitlines()[0]

                        width, height = page.get_size()
                        if width * height * (dpi / 72) ** 2 > 40_000_000:
                            raise ValueError(f"Слишком большая страница: {index + 1}")
                        with closing(page.render(scale=dpi / 72)) as bitmap:
                            with bitmap.to_pil() as image:
                                image.save(image_path)

                        text = _run_tesseract(
                            [
                                tesseract, str(image_path), "stdout",
                                "-l", language, "--dpi", str(dpi),
                                "--psm", "3", *ocr_options,
                            ],
                            timeout,
                        )
                        method = "ocr"

                pages.append({
                    "page_number": index + 1,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "extraction_method": method,
                })
                logger.info(
                    "%s: страница %s/%s, %s, символов %s",
                    path.name, index + 1, len(document), method, len(text),
                )

    if not any(any(character.isalnum() for character in p["text"]) for p in pages):
        raise ValueError(f"Текст не распознан: {path.name}")

    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != source_hash:
            raise ValueError("PDF изменился во время обработки. Повторите запуск.")

    return {
        "schema_version": 1,
        "source_file": path.name,
        "source_sha256": source_hash,
        "page_count": len(pages),
        "extractor": {
            "pypdfium2": version("pypdfium2"),
            "pillow": version("Pillow"),
            "ocr_engine": ocr_version,
            "ocr_language": language if ocr_version else None,
            "ocr_dpi": dpi if ocr_version else None,
        },
        "requires_review": any(p["extraction_method"] == "ocr" for p in pages),
        "empty_pages": [p["page_number"] for p in pages if not p["text"].strip()],
        "pages": pages,
    }


def save_extraction(document: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{document['source_sha256']}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_dir,
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Извлечение текста банковских PDF")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tesseract", default="tesseract")
    parser.add_argument("--tessdata-dir", type=Path)
    parser.add_argument("--language", default="rus+eng")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        paths = (
            sorted(p for p in args.input.iterdir() if p.suffix.lower() == ".pdf")
            if args.input.is_dir() else [args.input]
        )
        if not paths:
            raise ValueError("В папке нет PDF.")
        for path in paths:
            document = extract_pdf(
                path, tesseract=args.tesseract, tessdata_dir=args.tessdata_dir,
                language=args.language, dpi=args.dpi, timeout=args.timeout,
            )
            destination = save_extraction(document, args.output)
            print(json.dumps({
                "file": path.name,
                "pages": document["page_count"],
                "empty_pages": document["empty_pages"],
                "requires_review": document["requires_review"],
                "output": str(destination),
            }, ensure_ascii=False), flush=True)
    except (OSError, ValueError, pdfium.PdfiumError) as error:
        parser.exit(1, f"Ошибка: {error}\n")


if __name__ == "__main__":
    main()