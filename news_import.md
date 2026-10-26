# Внешние новости: официальный RSS Банка России

Источник: https://www.cbr.ru/rss/RssPress, опубликованный в каталоге
https://www.cbr.ru/development/RSS/.

Импортируются тексты description, предоставленные самим RSS.
Полная веб-страница дополнительно не скачивается.
Тип текста сохранён как content_scope=rss_description.

Из корня проекта:

```powershell
$env:DATABASE_URL = "sqlite+aiosqlite:///./alfa_real_data.db"
.\.venv\Scripts\python.exe -m backend.app.ingestion.news --dry-run
.\.venv\Scripts\python.exe -m backend.app.ingestion.news
```

По умолчанию выбираются до 30 последних пригодных элементов.
--limit: 1..100.
--input PATH позволяет импортировать локальную копию RSS XML.
Dry-run проверяет RSS без подключения к БД.

Новости сохраняются в news_articles с is_bank_analytics=false.
В external_news_sources сохраняются ссылки, guid, исходный HTML,
хеши и область текста. Даты хранятся как naive UTC.
Даты публикаций и ОКВЭД не угадываются.

Список для ML:
GET /api/v1/companies/{company_id}/news?limit=10&days=90

Источник выбранной новости:
GET /api/v1/news/{news_id}/source

Общий DTO списка NewsArticleResponse не изменяется.

Повтор того же URL и содержимого пропускается.
Изменение ранее загруженной публикации блокирует импорт целиком:
для замены версии нужна отдельная сверка.

API источника возвращает 409 при нарушении целостности записи.

Это первая макроэкономическая и регуляторная лента.
Она не обеспечивает полного покрытия всех отраслей.
Отбор списка по-прежнему rules-v1.

Новая таблица создаётся через init_db().
Общие колонки существующих таблиц не меняются.

Тесты:
python -m pytest backend/tests/test_news_ingestion.py -q