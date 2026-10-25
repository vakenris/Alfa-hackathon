"""Narrow qualitative coverage hints, never evidence of actual financial exposure."""

import re


MANUFACTURING_CODE = re.compile(r"(?:1\d|2\d|3[0-3])(?:\.\d{1,2}){0,2}\Z")
POLICY_DECISION = re.compile(
    r"^\s*(?:\d{1,2}\s+[а-яё]+\s+\d{4}\s+года\s+)?"
    r"(?:Банк\s+России|ЦБ\s+РФ|Центральный\s+банк\s+России)\s+"
    r"(?:сохранил|повысил|снизил|оставил|решил\s+(?:сохранить|повысить|снизить|оставить))\b"
    r"[^.!?\n]{0,80}ключев\w*\s+ставк\w*", re.I,
)
NON_DECISION = re.compile(
    r"\bне\b|неверн|ложн|опроверг|опроверж|отриц|неподтвержд|"
    r"\bесли\b|гипотет|сценари|ожида|прогноз|предполож", re.I,
)


def qualitative_channels(known_features: dict, event_text: str) -> list[str]:
    """Recognize an explicit policy decision and a manufacturing industry only.

    This is a coverage floor for conditional analysis, not a universal relevance
    classifier. Other event/industry combinations remain the model's judgement.
    The industry does not establish debt, CAPEX plans or contract terms.
    """
    code = known_features.get("mainokved_code")
    sentences = re.split(r"(?<=[.!?])\s+|\n", event_text)
    affirmative = any(POLICY_DECISION.search(text) and not NON_DECISION.search(text) for text in sentences)
    if isinstance(code, str) and MANUFACTURING_CODE.fullmatch(code.strip()) and affirmative:
        return ["financing", "investment"]
    return []


def event_qualitative_channels(known_features: dict, event: dict) -> list[str]:
    # A headline must not override a full text denying that decision. This
    # conservative floor prefers the available body; the model still sees all.
    text = next((event.get(field) for field in ("content", "summary", "title") if event.get(field)), "")
    return qualitative_channels(known_features, text)
