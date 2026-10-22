"""Organizer API smoke check. The default mode uses MockTransport, not the network."""

import argparse
import asyncio
import json
import os

import httpx
from pydantic import ValidationError

from .api_client import ChatCompletionsClient, LLMAPIError, LLMHTTPError
from .config import ChatCompletionsConfig


API_URL = "https://changellenge.up.railway.app/v1/chat/completions"
MODEL = "deepseek/deepseek-v4.1-flash"
PROMPT = "Reply only OK."


def smoke_config() -> ChatCompletionsConfig:
    """Keep the approved target and small limits fixed; never infer authentication."""
    return ChatCompletionsConfig(
        api_url=API_URL,
        model=MODEL,
        auth_header=os.environ.get("AGENT_LLM_AUTH_HEADER"),
        auth_value=os.environ.get("AGENT_LLM_AUTH_VALUE"),
        timeout_seconds=30,
        max_output_tokens=32,
        token_limit_field=os.environ.get("AGENT_LLM_TOKEN_LIMIT_FIELD", "max_tokens"),
    )


async def run_check(config: ChatCompletionsConfig, *, send: bool = False) -> dict:
    if send and config.auth_header is None:
        raise ValueError("Configure the team's authentication header before a live check")

    headers = {"Content-Type": "application/json"}
    if config.auth_header is not None:
        headers[config.auth_header] = "<redacted>"
    body = {
        "model": config.model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        config.token_limit_field: config.max_output_tokens,
    }
    report = {
        "mode": "live" if send else "mock",
        "authorization_configured": config.auth_header is not None,
        "request": {"method": "POST", "url": config.api_url, "headers": headers, "json": body},
        "timeout_seconds": config.timeout_seconds,
        "automatic_retries": 0,
    }

    def mock_response(request: httpx.Request) -> httpx.Response:
        # Inspect the request serialized by the actual client, not just the preview.
        assert request.method == "POST"
        assert str(request.url) == config.api_url
        assert json.loads(request.content) == body
        if config.auth_header is not None:
            assert request.headers[config.auth_header] == config.auth_value.get_secret_value()
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]},
        )

    client = ChatCompletionsClient(config, transport=None if send else httpx.MockTransport(mock_response))
    try:
        text = await client.generate(PROMPT)
        report.update(success=True, reply_matches_expected=text.strip() == "OK")
    except LLMAPIError as error:
        report.update(success=False, error=type(error).__name__)
        if isinstance(error, LLMHTTPError):
            report["http_status"] = error.status_code

    # Never print model text, raw server bodies or arbitrary provider usage fields.
    # Preserve missing statistics as unknown, including after a timeout.
    usage = client.last_usage
    report["usage"] = None if usage is None else {
        name: usage[name]
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(usage.get(name)) is int and usage[name] >= 0
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send one real request; requires explicit approval")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_check(smoke_config(), send=args.send))
    except (ValidationError, ValueError):
        print(json.dumps({"success": False, "error": "ConfigurationError: check authentication and limits"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
