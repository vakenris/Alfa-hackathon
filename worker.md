# Запуск воркера

Очередь хранится в существующей таблице job_records.
API сохраняет PENDING, отдельный процесс атомарно забирает задание.

API и воркер должны использовать одну БД и один Redis.
Redis передаёт события для SSE. Состояние заданий хранится в БД.

Переменные окружения:

```dotenv
DATABASE_URL=sqlite+aiosqlite:///./alfa_worker_smoke.db
REDIS_URL=redis://127.0.0.1:6379/0
USE_IN_MEMORY_BUS=false
AUTO_SEED_DEMO=false
WORKER_MODE=database
WORKER_TIMEOUT_SECONDS=180
WORKER_POLL_SECONDS=1
```

Демонстрационная БД alfa_worker_smoke.db отделена от рабочей
alfa_real_data.db, чтобы синтетические результаты не кешировались
как результаты настоящего анализа.

Из корня проекта:

```powershell
docker compose up -d redis
.\.venv\Scripts\python.exe -m backend.app.cli seed-demo
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --port 8000
```

В другом терминале с теми же переменными:

```powershell
.\.venv\Scripts\python.exe -m backend.app.worker.runner --mock
```

--mock явно включает синтетический анализ.
--once обрабатывает не более одного ожидающего задания и завершает процесс.

Для настоящего анализа:
WORKER_ENGINE_FACTORY=module:function

Фабрика без аргументов возвращает движок с async generate_insight.
Тогда воркер запускается без --mock.
Фабрику RuntimeAgentEngine с клиентом LLM и BankPdfAnalyticsSource
согласуют с ML. Неявного переключения внешнего воркера на mock нет.

Таймаут по умолчанию — 180 секунд, максимум — 540.
Для агентного ограничения 400 секунд задаётся WORKER_TIMEOUT_SECONDS=450.

PROCESSING старше 10 минут без сохранённого результата становится FAILED.
Повторный запрос через API создаёт новое задание.
Сохранённый результат восстанавливает SUCCESS.
Автоматический повтор платного LLM-вызова не выполняется.

После аварии статус доступен через GET /api/v1/jobs/{job_id}.
Для старой SSE-подписки может понадобиться переподключение.

Тесты:
python -m pytest backend/tests/test_worker.py -q

conftest.py принудительно задаёт WORKER_MODE=inline для тестов.
Новые таблицы и миграции для этого блока не требуются.