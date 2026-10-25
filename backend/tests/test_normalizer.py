from decimal import Decimal

import pytest

from backend.app.ingestion.normalizer import (
    normalize_text,
    parse_count,
    parse_decimal,
    parse_flag,
    parse_identifier,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        (" NULL ", None),
        ("NaN", None),
        ("  Москва  ", "Москва"),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


def test_identifiers_preserve_leading_zeros():
    assert parse_identifier(" 000123 ") == "000123"
    assert parse_identifier("01.11") == "01.11"


def test_required_identifier_cannot_be_missing():
    with pytest.raises(ValueError, match="SYNTHETIC_U"):
        parse_identifier(
            "",
            field_name="SYNTHETIC_U",
        )


def test_optional_identifier_can_be_missing():
    assert parse_identifier(
        None,
        required=False,
    ) is None


def test_numeric_identifier_is_rejected_instead_of_coerced():
    with pytest.raises(TypeError):
        parse_identifier(123)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("1.0", True),
        (" TRUE ", True),
        ("Да", True),
        ("0", False),
        ("0.0", False),
        ("false", False),
        ("Нет", False),
        (None, None),
        ("", None),
        ("NaN", None),
    ],
)
def test_parse_flag(raw, expected):
    assert parse_flag(raw) is expected


@pytest.mark.parametrize(
    "raw",
    ["2", "возможно", "-"],
)
def test_unknown_flags_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_flag(raw)


@pytest.mark.parametrize(
    ("raw", "separator", "expected"),
    [
        (
            "1234.56",
            ".",
            Decimal("1234.56"),
        ),
        (
            "1 234,56",
            ",",
            Decimal("1234.56"),
        ),
        (
            "1\u00a0234,56",
            ",",
            Decimal("1234.56"),
        ),
        (
            "1\u202f234.56",
            ".",
            Decimal("1234.56"),
        ),
        (
            "-10.50",
            ".",
            Decimal("-10.50"),
        ),
        (
            "0",
            ".",
            Decimal("0"),
        ),
        (
            "9007199254740993.01",
            ".",
            Decimal("9007199254740993.01"),
        ),
        (
            None,
            ".",
            None,
        ),
        (
            "NaN",
            ".",
            None,
        ),
    ],
)
def test_parse_decimal(raw, separator, expected):
    assert parse_decimal(
        raw,
        decimal_separator=separator,
    ) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "12 34",
        "1,234.56",
        "1.234,56",
        "1_000",
        "1e6",
        "Infinity",
        "100 руб.",
    ],
)
def test_ambiguous_or_invalid_numbers_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_decimal(raw)


def test_decimal_separator_is_explicit():
    with pytest.raises(ValueError):
        parse_decimal("123,45")


def test_invalid_separator_is_rejected():
    with pytest.raises(ValueError):
        parse_decimal(
            "123",
            decimal_separator=";",
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("15", 15),
        ("15.0", 15),
        ("1 000", 1000),
        (None, None),
    ],
)
def test_parse_count(raw, expected):
    assert parse_count(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["-1", "1.5", "abc"],
)
def test_invalid_counts_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_count(raw)