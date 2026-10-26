"""Import official CBR RSS descriptions, preserving source identity and dates."""

import argparse
import asyncio
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models.news import NewsArticle
from backend.app.db.models.news_source import ExternalNewsSource


FEED_URL = "https://www.cbr.ru/rss/RssPress"
MAX_BYTES = 2 * 1024 * 1024


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RssText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

        if not self.hidden:
            if tag in {
                "p", "div", "br", "li", "tr", "h1", "h2", "h3",
            }:
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {
            "p", "div", "li", "tr", "h1", "h2", "h3",
        }:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        lines = [
            re.sub(r"[ \u00a0\u202f]+", " ", line).strip()
            for line in "".join(self.parts).splitlines()
        ]
        return "\n".join(line for line in lines if line)


def canonical_url(value: str) -> str:
    url = urlsplit(value.strip())

    if (
        url.scheme not in {"http", "https"}
        or url.hostname not in {"cbr.ru", "www.cbr.ru"}
        or url.username is not None
        or url.password is not None
        or url.port not in {None, 80, 443}
        or not url.path.lower().startswith("/press/")
    ):
        raise ValueError(
            "Ожидалась ссылка на публикацию cbr.ru/press/"
        )

    result = urlunsplit((
        "https",
        "www.cbr.ru",
        url.path,
        url.query,
        "",
    ))

    if len(result) > 2048:
        raise ValueError("Слишком длинная ссылка")

    return result


def payload_hash(values: dict) -> str:
    return sha256(json.dumps(
        {
            "title": values["title"],
            "content": values["content"],
            "published_at": values["published_at"].isoformat(),
            "category": values["category"],
            "source": values["source"],
            "is_bank_analytics": values["is_bank_analytics"],
        },
        sort_keys=True,
        ensure_ascii=False,
    ))


@dataclass
class NewsImportReport:
    feed_sha256: str = ""
    read_items: int = 0
    selected_items: int = 0
    duplicate_items: int = 0
    rejected_items: int = 0
    future_items: int = 0
    inserted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def parse_feed(
    payload: bytes,
    *,
    limit: int = 30,
    now: datetime | None = None,
):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit должен быть от 1 до 100")

    if len(payload) > MAX_BYTES:
        raise ValueError("RSS превышает 2 МБ")

    xml = payload.decode("utf-8-sig")

    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("DTD и XML entities не поддерживаются")

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ValueError("Некорректный RSS XML") from error

    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Ожидалась лента RSS 2.0")

    items = root.findall("./channel/item")
    if len(items) > 500:
        raise ValueError("В RSS больше 500 элементов")

    report = NewsImportReport(
        feed_sha256=hashlib.sha256(payload).hexdigest()
    )

    cutoff = now or datetime.now(timezone.utc)
    cutoff = (
        cutoff.replace(tzinfo=timezone.utc)
        if cutoff.tzinfo is None
        else cutoff.astimezone(timezone.utc)
    )

    prepared = {}

    for number, item in enumerate(items, start=1):
        report.read_items += 1

        try:
            title = (item.findtext("title") or "").strip()
            url = canonical_url(item.findtext("link") or "")

            raw_date = (item.findtext("pubDate") or "").strip()
            published = parsedate_to_datetime(raw_date)

            if published.tzinfo is None:
                raise ValueError(
                    "Дата RSS не содержит часовой пояс"
                )

            published = published.astimezone(timezone.utc)

            if published > cutoff:
                report.future_items += 1
                continue

            original_html = item.findtext("description") or ""
            parser = RssText()
            parser.feed(original_html)
            parser.close()
            content = parser.text()

            category = (
                (item.findtext("category") or "").strip()
                or "Регулирование"
            )
            guid = (item.findtext("guid") or "").strip() or None

            if (
                not title
                or len(title) > 512
                or len(content) < 80
                or len(category) > 128
            ):
                raise ValueError(
                    "Нет пригодного заголовка, текста или категории"
                )

            if guid is not None and len(guid) > 512:
                raise ValueError("Слишком длинный RSS guid")

            values = {
                "title": title,
                "content": content,
                "source": "Банк России",
                "published_at": published.replace(tzinfo=None),
                "category": category,
                "summary": None,
                "target_okved_prefix": None,
                "is_bank_analytics": False,
            }

        except (
            ValueError,
            TypeError,
            IndexError,
            OverflowError,
        ) as error:
            report.rejected_items += 1

            if len(report.errors) < 20:
                report.errors.append(
                    f"Элемент {number}: {error}"
                )
            continue

        fingerprint = payload_hash(values)

        if url in prepared:
            if prepared[url]["payload_sha256"] != fingerprint:
                raise ValueError(
                    "В RSS разные тексты для одной ссылки"
                )
            report.duplicate_items += 1
            continue

        prepared[url] = {
            "id": sha256(url),
            "source_url": url,
            "source_item_id": guid,
            "feed_url": FEED_URL,
            "feed_sha256": report.feed_sha256,
            "payload_sha256": fingerprint,
            "content_sha256": sha256(content),
            "content_scope": "rss_description",
            "original_html": original_html,
            "article": values,
        }

    records = sorted(
        prepared.values(),
        key=lambda item: item["article"]["published_at"],
        reverse=True,
    )[:limit]

    report.selected_items = len(records)

    if not records:
        raise ValueError(
            f"Нет пригодных публикаций: {asdict(report)}"
        )

    return records, report


async def download_feed(client: httpx.AsyncClient) -> bytes:
    async with asyncio.timeout(30):
        async with client.stream(
            "GET",
            FEED_URL,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()

            chunks = []
            size = 0

            async for chunk in response.aiter_bytes():
                size += len(chunk)

                if size > MAX_BYTES:
                    raise ValueError("RSS превышает 2 МБ")

                chunks.append(chunk)

            return b"".join(chunks)


async def save_news(
    session: AsyncSession,
    records: list[dict],
    report: NewsImportReport,
):
    inserted = 0
    skipped = 0

    try:
        for item in records:
            previous = await session.get(
                ExternalNewsSource,
                item["id"],
            )

            if previous is not None:
                article = await session.get(
                    NewsArticle,
                    previous.article_id,
                )

                if (
                    article is None
                    or sha256(article.content)
                    != previous.content_sha256
                ):
                    raise ValueError(
                        "Нарушена целостность ранее загруженной новости"
                    )

                stored = {
                    name: getattr(article, name)
                    for name in (
                        "title",
                        "content",
                        "published_at",
                        "category",
                        "source",
                        "is_bank_analytics",
                    )
                }

                if payload_hash(stored) != previous.payload_sha256:
                    raise ValueError(
                        "Изменены поля ранее загруженной новости"
                    )

                if (
                    previous.payload_sha256
                    != item["payload_sha256"]
                ):
                    raise ValueError(
                        "Источник изменил публикацию; "
                        "нужна отдельная сверка: "
                        f"{item['source_url']}"
                    )

                skipped += 1
                continue

            article = NewsArticle(**item["article"])
            session.add(article)
            await session.flush()

            session.add(ExternalNewsSource(
                **{
                    key: value
                    for key, value in item.items()
                    if key != "article"
                },
                article_id=article.id,
            ))
            await session.flush()
            inserted += 1

        await session.commit()

    except Exception:
        await session.rollback()
        raise

    report.inserted = inserted
    report.skipped = skipped

    return report


async def run(args):
    if args.input is not None:
        with args.input.open("rb") as stream:
            payload = stream.read(MAX_BYTES + 1)
    else:
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "AlfaHack-NewsETL/1.0"},
        ) as client:
            payload = await download_feed(client)

    records, report = parse_feed(payload, limit=args.limit)

    if not args.dry_run:
        from backend.app.db.session import (
            async_session_factory,
            engine,
            init_db,
        )

        try:
            await init_db()
            async with async_session_factory() as session:
                await save_news(session, records, report)
        finally:
            await engine.dispose()

    print(json.dumps(
        {
            "dry_run": args.dry_run,
            **asdict(report),
        },
        ensure_ascii=False,
        indent=2,
    ))


def main():
    parser = argparse.ArgumentParser(
        description="Импорт пресс-релизов Банка России"
    )
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument(
        "--input",
        type=Path,
        help="Локальная копия официального RSS XML",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except (
        OSError,
        ValueError,
        httpx.HTTPError,
        TimeoutError,
        ImportError,
    ) as error:
        parser.exit(
            1,
            f"Ошибка импорта новостей: {error}\n",
        )


if __name__ == "__main__":
    main()