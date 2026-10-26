# Подключение ML к отдельному воркеру

Проверенные версии: Data 7da2881, ML ce3f290 плюс локально
подготовленные импорт RSS и отдельный worker.

Проверочное объединение remote-веток прошло без Git-конфликтов.
Объединение на GitHub должно выполняться согласованно с командой.

## Подключение

backend/app/worker/bootstrap.py предоставляет create_engine().

Он связывает RuntimeAgentEngine с BankPdfAnalyticsSource
и фабрикой BudgetedChatCompletionsClient.

Фабрика должна синхронно возвращать новый бюджетный клиент
с настроенными общим BudgetStore, окном учёта, тарифами
и достоверной верхней границей токенов запроса.

Эти зависимости предоставляет владелец LLM-контура.

Нужны подтверждённые значения:

- WORKER_CLIENT_FACTORY: путь module:function.
- WORKER_APPROVED_MODEL_IDS: JSON-массив точных разрешённых model ID.

Дополнительные настройки:

```dotenv
WORKER_MODE=database
WORKER_TIMEOUT_SECONDS=450
WORKER_ENGINE_FACTORY=backend.app.worker.bootstrap:create_engine
USE_IN_MEMORY_BUS=false
AUTO_SEED_DEMO=false
```

DATABASE_URL для API и воркера указывает на одну рабочую БД.
REDIS_URL указывает на один Redis.

Model ID и client_factory предоставляет ML.
Ключ остаётся в локальной конфигурации владельца клиента.

Для настоящего запуска должны быть объединены модули agent/ и llm/.

## Первый запуск

Один выбранный PENDING job:

```text
python -m backend.app.worker.runner --job-id JOB_ID
```

--job-id завершает процесс после выбранного задания.
Остальные PENDING-задания не обрабатываются.

Для демонстрационной проверки можно добавить --mock.
Настоящий запуск выполняется без --mock.

SUCCESS-задание повторно не анализируется.
Если job не найден или уже не PENDING, проверь его статус через API.

Настройки runtime:
max_llm_calls=1, max_recovery_attempts=0.

Эти ограничения не заменяют общий денежный BudgetStore.

До постоянной обработки очереди Backend1 должен подключить
отбор к schedule_company_news(): существующий /feed
планирует задания по всему корпусу.

## Проверка

```text
python -m pytest backend/tests/test_worker.py backend/tests/test_worker_bootstrap.py -q
```

Bootstrap-тесты требуют ML-ветку.
До её объединения модуль тестов пропускается.

Тесты используют HTTP MockTransport и синтетические тарифы/store.
Настоящая платная модель и производственный BudgetStore
этими тестами не проверяются.