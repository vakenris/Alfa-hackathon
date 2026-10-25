# Спецификация интеграции бэкенда с фронтендом (Frontend API Contract)

> Автоматический режим: `GET /companies/{company_id}/feed` запускает недостающие анализы всех новостей компании. Общие результаты и статусы доступны через `GET /companies/{company_id}/analytics/automatic-insights` (`items`, `jobs`). Фронтенд опрашивает снимок и показывает готовые данные без кнопки запуска.
> Проект: **Alfa Business News Analytics (Альфа-Банк)**  
> Разработано: **Бэкендер №1 (Core, API & Security)** для **Фронтендера (Роль 3)**

---

## 1. Базовые параметры подключения

| Параметр | Значение при локальной разработке | Значение в Docker Compose |
|---|---|---|
| **API Base URL** | `http://localhost:8000/api/v1` | `http://localhost:80/api/v1` (через Nginx) |
| **Swagger UI** | `http://localhost:8000/docs` | `http://localhost:80/docs` |
| **OpenAPI Schema** | `http://localhost:8000/openapi.json` | `http://localhost:80/openapi.json` |
| **Разрешенные фронтенд-ориджины** | `http://localhost:5173` (Vite), `http://localhost:3000` (CRA/Next.js) | Настроено в CORS |

### Заголовки запросов (Headers):
```http
Content-Type: application/json
X-User-Role: CEO | CFO | TREASURY | ACCOUNTANT | ANALYST
```
*Примечание:* Заголовок `X-User-Role` управляет RBAC и адаптацией ответов Q&A. Если заголовок не передан или передан некорректно, бэкенд безопасно использует `CEO` по умолчанию (без ошибок 401/403).

---

## 2. Карта эндпоинтов для фронтенда

### 2.1. Выбор компании и профиль (Шапка и карточка компании)

#### `GET /api/v1/companies`
Список компаний крупного бизнеса (> 15 млрд ₽).
- **Ответ `200 OK`**:
```json
[
  {
    "id": 1,
    "synthetic_u": "U_1001",
    "name": "ПАО «Северный Металлургический Холдинг»",
    "org_type": "ПАО",
    "okfs_name": "Частная собственность",
    "company_age_years": "15+",
    "eco_mark_label": "Зрелый бизнес",
    "mainokved_code": "24.10",
    "mainokved_name": "Производство чугуна, стали и ферросплавов",
    "amount_workers_fns": "5000+",
    "cl_type": "Действующий",
    "cl_product_rko_mark": true,
    "cl_product_gar_mark": true,
    "cl_product_loa_mark": true,
    "cl_product_lea_mark": true,
    "cl_product_dep_mark": false,
    "cl_product_zpp_mark": true,
    "factoring_mark": false,
    "fts_flag": true,
    "fts_country_name": "Китай, Индия, Турция",
    "revenue_company": 68500000000.0,
    "city": "Череповец",
    "region": "Вологодская область"
  }
]
```

#### `GET /api/v1/companies/{company_id}`
Детальный профиль выбранной компании.

#### `GET /api/v1/companies/{company_id}/counterparties`
Граф взаимосвязей и ключевых контрагентов (df_Fid_Uid_link).
- **Ответ `200 OK`**:
```json
{
  "company_synthetic_u": "U_1001",
  "top_counterparties": [
    {
      "synthetic_u": "U_CUST_1",
      "direction": "CUSTOMER",
      "total_amount": 8900000000.0,
      "transaction_count": 210
    },
    {
      "synthetic_u": "U_SUPP_1",
      "direction": "SUPPLIER",
      "total_amount": 5200000000.0,
      "transaction_count": 142
    }
  ]
}
```

---

### 2.2. Персонализированная лента новостей и аналитики Альфа-Банка

#### `GET /api/v1/companies/{company_id}/feed?limit=50`
Персонализированная лента под компанию с приоритизацией по ее отрасли (ОКВЭД) и макроэкономике.
- **Ответ `200 OK`**:
```json
{
  "company_id": 1,
  "total": 4,
  "items": [
    {
      "id": 1,
      "title": "Ключевая ставка ЦБ РФ: сохранение на уровне 14,0%",
      "source": "Центр макроэкономических исследований Альфа-Банка",
      "category": "Макроэкономика",
      "published_at": "2026-10-24T12:00:00",
      "summary": "ЦБ сохранил ставку 14%. Период дорогого кредитования продолжается.",
      "is_bank_analytics": true,
      "relevance_score": 1.2
    }
  ]
}
```

#### `GET /api/v1/analytics/bank`
Публикации и макроэкономические исследования Центра исследований Альфа-Банка.

---

### 2.3. Генерация инсайта и живой стриминг рассуждений (Agent Trace)

#### `POST /api/v1/insights`
Запуск асинхронного расчета инсайта для пары (Компания + Новость).
- **Тело запроса:**
```json
{
  "company_id": 1,
  "event_id": 1
}
```
- **Ответ `200 OK`**:
```json
{
  "job_id": "f364565b-d98d-476f-9b7f-e80cf86deefd",
  "status": "PENDING",
  "message": "Insight generation pipeline started"
}
```
*(Если инсайт уже был сгенерирован ранее, возвращается кэшированный результат со статусом `SUCCESS`).*

#### `GET /api/v1/streams/jobs/{job_id}` (или `/api/v1/jobs/{job_id}/stream`) — SSE Stream
Server-Sent Events (SSE) поток рассуждений агента в реальном времени.

**Пример использования в React / TypeScript:**
```typescript
const eventSource = new EventSource(`http://localhost:8000/api/v1/streams/jobs/${jobId}`);

eventSource.addEventListener("step", (e: MessageEvent) => {
  const step = JSON.parse(e.data);
  // step = { step_number: 1, step_name: "Шаг 1: Сбор контекста", description: "...", timestamp: "..." }
  addTraceStepToUI(step);
});

eventSource.addEventListener("completed", (e: MessageEvent) => {
  const data = JSON.parse(e.data);
  // data = { event: "completed", status: "SUCCESS", insight_id: 123 }
  eventSource.close();
  loadFullInsight(data.insight_id);
});

eventSource.addEventListener("failed", (e: MessageEvent) => {
  const data = JSON.parse(e.data);
  eventSource.close();
  showError(data.error);
});
```

#### `GET /api/v1/jobs/{job_id}` или `GET /api/v1/streams/{job_id}/history`
Статический JSON со статусом джоба и списком всех пройденных шагов рассуждений (для рендеринга истории после перезагрузки страницы).

---

### 2.4. Отображение готового инсайта

#### `GET /api/v1/insights/{insight_id}`
Полный структурированный инсайт с 6 обязательными блоками ответов, позицией банка и источниками (Evidence).
- **Ответ `200 OK`**:
```json
{
  "id": 1,
  "job_id": "f364565b-d98d-476f-9b7f-e80cf86deefd",
  "company_id": 1,
  "event_id": 1,
  "what_happened": "Опубликовано решение ЦБ РФ по ключевой ставке 14%...",
  "why_important": "Для бизнеса ПАО «Северный Металлургический Холдинг» событие имеет критический характер. Компания имеет действующие кредитные обязательства...",
  "consequences": "1. Риск изменения стоимости заемного финансирования.\n2. Возможность оптимизации оборотного капитала...",
  "bank_view": "Позиция Центра макроэкономических исследований Альфа-Банка: в условиях жесткой ДКП ключевым приоритетом становится контроль ликвидности...",
  "ai_view": "Сгенерированный AI-анализ: компании рекомендуется актуализировать платежный календарь...",
  "recommended_action": "Провести внеочередной финансовый комитет казначейства, пересмотреть лимиты овердрафта...",
  "relevant_bank_product": "Факторинг и Овердрафт для крупного бизнеса",
  "created_at": "2026-10-24T14:30:00",
  "evidences": [
    {
      "claim": "Ключевая ставка 14%",
      "source_title": "Банк России / Пресс-релиз",
      "source_url": null,
      "company_feature": "cl_product_loa_mark = true"
    }
  ]
}
```

---

### 2.5. Интерактивный Q&A Drilldown по инсайту (Критерий 10)

#### `POST /api/v1/questions`
Задать уточняющий вопрос по инсайту. Ответ автоматически адаптируется под роль, указанную в заголовке `X-User-Role`.
- **Заголовки:**
  ```http
  X-User-Role: CFO
  ```
- **Тело запроса:**
```json
{
  "insight_id": 1,
  "question": "Как это повлияет на кредитный портфель и процентные расходы?"
}
```
- **Ответ `200 OK`**:
```json
{
  "insight_id": 1,
  "question": "Как это повлияет на кредитный портфель и процентные расходы?",
  "answer": "С точки зрения CFO (Финансового директора): Рекомендуется стресс-тестировать финансовую модель при ставке +200 б.п., оценить ковенанты по чистому долгу к EBITDA и рассмотреть возможность субсидируемого фондирования.",
  "relevant_source": "Анализ финансового профиля компании"
}
```

---

### 2.6. Аналитический дашборд компании (Блок A)

- `GET /api/v1/companies/{company_id}/analytics/summary` — финансовая сводка, активные продукты, количество инсайтов за 7 дней.
- `GET /api/v1/companies/{company_id}/analytics/insights-history` — история ранее сформированных инсайтов компании.
- `GET /api/v1/companies/{company_id}/analytics/counterparty-exposure` — входящие/исходящие денежные потоки и концентрация рисков по контрагентам.

---

### 2.7. Мониторинг бюджета и здоровья системы (Критерий 08, Блок B)

- `GET /api/v1/metrics/budget` — мониторинг Budget Guardian:
```json
{
  "daily_budget_rub": 500.0,
  "spent_budget_rub": 14.50,
  "remaining_budget_rub": 485.50,
  "total_tokens_used": 18450,
  "budget_exhausted": false
}
```
- `GET /api/v1/metrics/pipeline` — конверсия джобов (успех/ошибки/в процессе, % успеха, среднее время).
- `GET /api/v1/metrics/system` — статус `HEALTHY`, аптайм сервера в секундах, счетчики компаний и новостей.
