# Сервис новостной аналитики для крупного бизнеса (Альфа-Банк)
> Контур: **Backend & DevOps (Python / FastAPI / Docker)**

Сервис преобразует поток внешних новостей и аналитических материалов Центра макроэкономических исследований Альфа-Банка в персонализированные бизнес-инсайты для лиц, принимающих решения (ЛПР) в компаниях с выручкой свыше 15 млрд рублей.

---

## 🏛️ Архитектура бэкенда

```
[Пользователь / Интернет-Банк Альфа-Бизнес]
                     │
                     ▼
             [React Frontend]
               │           ▲
   POST /insights          │ GET /jobs/{id}/stream (SSE: живой стрим шагов)
               │           │ GET /insights/{id}
               ▼           │
     [FastAPI API Gateway (Backend)]
               │
        (Job created in DB)
               │
               ▼
     [Asynchronous Worker]
               │
               ├─► [Database (PostgreSQL / SQLite)]
               │     • Профиль компании (ОКВЭД, выручка, продукты банка)
               │     • Граф связей и контрагентов (df_Fid_Uid_link)
               │     • Аналитика Альфа-Банка & Новости
               │
               ├─► [Event Bus (Redis / In-Memory)]
               │     • Публикация промежуточных шагов рассуждений (Trace)
               │
               └─► [ML-Интерфейс (MLInsightEngine)]
                     • Планирование шагов анализа
                     • Запросы к разрешенным моделям (Qwen / GLM / DeepSeek)
                     • Синтез выводов (Факты, Влияние, Банк vs AI, Рекомендация)
```

---

## 🚀 Быстрый старт

### Вариант 1. Локальный запуск без Docker (мгновенно)

1. **Активируйте виртуальное окружение:**
   ```bash
   # Windows PowerShell
   .venv\Scripts\activate
   ```

2. **Установите зависимости (если еще не установлены):**
   ```bash
   pip install -e ".[dev]"
   ```

3. **Запустите сервер (база данных SQLite инициализируется и наполнится автоматически):**
   ```bash
   uvicorn backend.app.main:app --reload --port 8000
   ```

4. Откройте интерактивную документацию Swagger UI:
   👉 **http://localhost:8000/docs**

---

### Вариант 2. Запуск в Docker Compose (Production / Оценка жюри)

```bash
docker compose -f deploy/docker-compose.yml up --build
```
Поднимает:
- `backend` (FastAPI + Uvicorn) на порту `8000`
- `postgres` (PostgreSQL 16) на порту `5432`
- `redis` (Redis 7) на порту `6379`

---

## 🧪 Сквозной тестовый сценарий (End-to-End Verification)

Запуск автоматических тестов:
```bash
pytest -v
```

### Проверка через `curl` / HTTP:

1. **Получить список компаний крупного бизнеса (> 15 млрд руб.):**
   ```bash
   curl -X GET "http://localhost:8000/api/v1/companies"
   ```

2. **Получить граф контрагентов компании:**
   ```bash
   curl -X GET "http://localhost:8000/api/v1/companies/1/counterparties"
   ```

3. **Получить персонализированную новостную ленту компании:**
   ```bash
   curl -X GET "http://localhost:8000/api/v1/companies/1/feed" -H "X-User-Role: CFO"
   ```

4. **Запустить расчет инсайта (создание асинхронной Job):**
   ```bash
   curl -X POST "http://localhost:8000/api/v1/insights" \
        -H "Content-Type: application/json" \
        -H "X-User-Role: CFO" \
        -d '{"company_id": 1, "event_id": 1}'
   ```
   *Возвращает: `{"job_id": "...", "status": "PENDING"}`.*

5. **Подключиться к SSE-стриму рассуждений агента в реальном времени:**
   ```bash
   curl -N -X GET "http://localhost:8000/api/v1/jobs/<JOB_ID>/stream"
   ```
   *Фронтенд получает события: `event: step`, `event: completed`.*

6. **Получить готовый инсайт с фактами, позицией банка и источниками (Evidence):**
   ```bash
   curl -X GET "http://localhost:8000/api/v1/insights/1"
   ```

7. **Задать уточняющий вопрос (Критерий 10):**
   ```bash
   curl -X POST "http://localhost:8000/api/v1/questions" \
        -H "Content-Type: application/json" \
        -H "X-User-Role: CFO" \
        -d '{"insight_id": 1, "question": "Как это повлияет на кредитный портфель?"}'
   ```

---

## 🤝 Контракт для команды ML

Для подключения реального алгоритма ML-инженеру достаточно реализовать интерфейс в `backend/app/ml/interface.py`:

```python
class MLInsightEngineProtocol(Protocol):
    async def generate_insight(
        self,
        company: CompanyResponse,
        event: NewsArticleResponse,
        counterparties: List[CounterpartyItem],
        tracer: AgentTracerProtocol
    ) -> MLInsightResult:
        ...
```
- Во время рассуждений ML-модуль вызывает `await tracer.emit_step("Название шага", "Описание")` — бэкенд автоматически сохраняет шаг в БД и транслирует его на фронтенд через SSE!
- По умолчанию в бэкенде включен `MockMLInsightEngine`, благодаря чему бэкенд и фронтенд могут полностью тестироваться и демонстрироваться автономно.

## Фронтенд и бэкенд вместе

Локально из корня: `uvicorn backend.app.main:app --reload --port 8000`.
Во втором терминале: `cd frontend`, `npm ci`, `npm run dev`.
Интерфейс: http://localhost:5173. Vite проксирует `/api` на порт 8000.
Настройки адреса API: `frontend/.env.example`.

Docker: `docker compose up --build` из корня либо
`docker compose -f deploy/docker-compose.yml up --build`.
Интерфейс: http://localhost, API: /api/v1, Swagger: /docs.

Компании, новости, аналитика, ход генерации, инсайты и ответы на вопросы
загружаются из API. Профиль хранится в браузере: серверного метода
его сохранения пока нет. В анализе показаны рекомендации по выбранному
событию и связанные банковские продукты.
Бэкенд по умолчанию наполняет БД демонстрационными записями и использует
MockMLInsightEngine. Настоящие источники новостей и ML подключаются отдельно.

### Автоматический анализ новостей

При загрузке ленты сервер автоматически готовит недостающие анализы всех
новостей для выбранной компании, включая новости за пределами текущей страницы.
Готовые результаты и выполняющиеся задачи переиспользуются. После ошибки
следующее обновление ленты повторяет попытку. Интерфейс обновляет ленту раз
в минуту и получает общий снимок результатов через
`GET /api/v1/companies/{company_id}/analytics/automatic-insights`.
Кнопка запуска анализа не нужна: карточка новости и сводный анализ используют
одни и те же результаты. `POST /api/v1/insights` сохранён для API-интеграций и
переиспользует ту же задачу вместо повторного запуска.
