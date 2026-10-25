"""Normalize CSV strings without guessing units or missing values."""

import re
from decimal import Decimal


_MISSING_VALUES = {"", "null", "none", "nan", "n/a", "na", "<na>"}
_TRUE_VALUES = {"1", "1.0", "true", "yes", "да"}
_FALSE_VALUES = {"0", "0.0", "false", "no", "нет"}

# Разделители тысяч: обычный, неразрывный и узкий пробелы.
_INTEGER_PATTERN = (
    r"(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+)"
)

_NUMBER_PATTERNS = {
    ".": re.compile(
        rf"[+-]?{_INTEGER_PATTERN}(?:\.[0-9]+)?"
    ),
    ",": re.compile(
        rf"[+-]?{_INTEGER_PATTERN}(?:,[0-9]+)?"
    ),
}


def normalize_text(value: str | None) -> str | None:
    """Убрать крайние пробелы и распознать пропущенное значение."""
    if value is None:
        return None

    if not isinstance(value, str):
        raise TypeError("Ожидалась строка CSV или None")

    text = value.strip()

    if text.casefold() in _MISSING_VALUES:
        return None

    return text


def parse_identifier(
    value: str | None,
    *,
    field_name: str = "identifier",
    required: bool = True,
) -> str | None:
    """Сохранить идентификатор строкой, включая ведущие нули."""
    text = normalize_text(value)

    if text is None and required:
        raise ValueError(
            f"Обязательное поле {field_name} не заполнено"
        )

    return text


def parse_flag(value: str | None) -> bool | None:
    """Распознать флаг. Пропущенное значение остаётся None."""
    text = normalize_text(value)

    if text is None:
        return None

    normalized = text.casefold()

    if normalized in _TRUE_VALUES:
        return True

    if normalized in _FALSE_VALUES:
        return False

    raise ValueError(
        f"Неизвестное значение флага: {text!r}"
    )


def parse_decimal(
    value: str | None,
    *,
    decimal_separator: str = ".",
) -> Decimal | None:
    """Разобрать число с явно указанным десятичным разделителем."""
    if decimal_separator not in _NUMBER_PATTERNS:
        raise ValueError(
            "Десятичный разделитель должен быть '.' или ','"
        )

    text = normalize_text(value)

    if text is None:
        return None

    pattern = _NUMBER_PATTERNS[decimal_separator]

    if pattern.fullmatch(text) is None:
        raise ValueError(
            f"Некорректное число: {text!r}"
        )

    normalized = (
        text.replace(" ", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(decimal_separator, ".")
    )

    return Decimal(normalized)


def parse_count(
    value: str | None,
    *,
    decimal_separator: str = ".",
) -> int | None:
    """Разобрать целое неотрицательное количество без округления."""
    number = parse_decimal(
        value,
        decimal_separator=decimal_separator,
    )

    if number is None:
        return None

    if number < 0 or number != number.to_integral_value():
        raise ValueError(
            f"Ожидалось целое неотрицательное число: {value!r}"
        )

    return int(number)