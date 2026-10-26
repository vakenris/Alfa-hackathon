# Сервис новостной аналитики для крупного бизнеса (Альфа-Банк)
> Контур: **Backend & DevOps (Python / FastAPI / Docker)**

Сервис преобразует поток внешних новостей и аналитических материалов Центра макроэкономических исследований Альфа-Банка в персонализированные бизнес-инсайты для лиц, принимающих решения (ЛПР) в компаниях с выручкой свыше 15 млрд рублей.

---

## Архитектура бэкенда

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

## Старт

### Запуск в Docker Compose (Production / Оценка жюри)

```bash
docker compose -f deploy/docker-compose.yml up --build
```
Поднимает:
- `backend` (FastAPI + Uvicorn) на порту `8000`
- `postgres` (PostgreSQL 16) на порту `5432`
- `redis` (Redis 7) на порту `6379`
