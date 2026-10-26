# Подключение AI-компонента

Компонент готов к автономной проверке и подключению через существующие DTO.
Текущее приложение всё ещё использует mock. Здесь описаны задачи владельцев;
API, worker, DB, shared schemas, LLM-инфраструктура и frontend не изменялись.

## Проверенный срез и smoke-тест: 26 октября 2026

Проверен локальный `feature/llm-agent`, HEAD `a86354b` (`Update AI agent`),
включая состояние рабочего дерева: до этой работы оно было чистым. Удалённые
ветки изучались через существующие refs; fetch, переключение и объединение не
выполнялись. Замечания об `origin/Backend` относятся к срезу `fce14a4`, а не к
непроверенному текущему состоянию сервера Git.

Новый `tests/test_backend_integration_smoke.py` проверяет две цепочки:

1. Настоящие `CompanyResponse` / `NewsArticleResponse` из объектов с атрибутами,
   `CounterpartyItem` → production `RuntimeAgentEngine` → настоящий core,
   `BackendAnalyticsSource.from_repository_factory()` и общий бюджетируемый
   HTTP-клиент → `MLInsightResult` → `InsightResponse` из объекта с атрибутами.
2. `generate_analysis()` → JSON round-trip полного `AgentAnalysisResult` →
   grounded Q&A → существующий `QuestionAnswerResponse`. Проверены банковские
   цитаты с датой/document/page/hash, платёжная выборка, `needs_data` о долге
   при наличии флага кредитного продукта, отказ для другой компании и изменённых
   metadata источника. Все вопросы обходятся без дополнительных HTTP/DB-вызовов.

Используются `httpx.MockTransport`, подставные repository/session и синтетические
ответы/metadata; это не оценка качества настоящей LLM. Реальные секреты и env
конфигурация не читаются; сетевое подключение запрещено тестом. SQLAlchemy в
локальном окружении агента отсутствует, поэтому настоящий SQL-репозиторий,
транзакции, миграции, worker и FastAPI startup здесь не исполнялись. Публичная
проекция совместима с сохранением по коду `InsightRepository.save_insight`, но
настоящая запись и чтение БД этим smoke-тестом не подтверждаются.

Из корня репозитория, PowerShell:

```powershell
.\backend\app\agent\.venv\Scripts\python.exe -B -m unittest backend.app.agent.tests.test_backend_integration_smoke -v
```

Результат: **2/2** новых smoke-теста. Вместе с существующими
`agent.tests.test_backend_source` и `llm.tests.test_agent_integration`:
**28/28**, без платных вызовов. Синтетические usage и тариф в тесте проверяют
проводку учёта, а не фактическую стоимость API организаторов.

## Неизменённый контракт

`backend/app/ml/interface.py`:

```python
async def generate_insight(
    company: CompanyResponse,
    event: NewsArticleResponse,
    counterparties: list[CounterpartyItem],
    tracer: AgentTracerProtocol,
) -> MLInsightResult: ...
```

Сохранены все восемь полей: `what_happened`, `why_important`, `consequences`,
`bank_view`, `ai_view`, `recommended_action`, `relevant_bank_product`, `evidences`.
`EvidenceItem` также прежний: claim, source_title, source_url, company_feature.
`generate_analysis(...)` дополнительно возвращает `AgentAnalysisResult` с анализом,
условиями, действиями, источниками, evidence, расчётами, verification и run report.
Общий контракт frontend не меняется: механизмы и расчёты в `consequences`,
условные сценарии и ограничения в `ai_view`.

Общий клиент остаётся тем же классом из `backend/app/llm/client.py`:
`agent.api_client` и `agent.config.ChatCompletionsConfig` — его aliases.
Клиент обязан предоставлять `async generate(prompt: str) -> str`.

## Worker и бюджет: backend / владелец LLM

В `backend/app/worker/tasks.py` заменить создание `MockMLInsightEngine` посредством
существующего `set_ml_engine(...)` в composition root процесса, где выполняется
worker. Сейчас `api/v1/insights.py:create_insight_job()` ставит
`run_insight_pipeline` в FastAPI `BackgroundTasks` того же процесса: устанавливать
engine при инициализации приложения до приёма заданий. Если worker будет вынесен
в другой процесс, DI потребуется также там. Настройка только API-процесса не
подключит отдельный worker-процесс. Низкоуровневый `AgentInsightEngine` создаёт
сам runtime; в production передавать runtime с общей защитой бюджета.

Схема зависимостей, не самостоятельная команда запуска:

```python
from backend.app.agent import AgentConfig, BackendAnalyticsSource, RuntimeAgentEngine

engine = RuntimeAgentEngine(
    client_factory=make_budgeted_client,
    source_factory=lambda: BackendAnalyticsSource.from_repository_factory(
        session_factory,
        provenance_resolver=resolve_trusted_provenance,
    ),
    approved_model_ids=confirmed_gateway_model_ids,
    config=AgentConfig(llm_timeout_seconds=180, run_timeout_seconds=400),
)
# В процессе worker:
set_ml_engine(engine)
```

`make_budgeted_client` синхронно создаёт свежий общий
`BudgetedChatCompletionsClient` с утверждёнными endpoint/model, тайм-аутом и
output cap. Не читать ключи в коде агента. Список model IDs должен приходить
из подтверждённой конфигурации организаторов; произвольные aliases не угадывать.
Production `RuntimeAgentEngine` отклоняет обычный HTTP-клиент и произвольную
обёртку без общего budget guard до первого запроса. Проверка класса не доказывает
правильность настроек хранилища/тарифа: их обязан обеспечить composition root.
Для запуска без recovery: `max_llm_calls=1, max_recovery_attempts=0`.

Companion-метод runtime принимает необязательные `financial_inputs=` и
`loan_sensitivity=` и передаёт существующему core. Это явно переданные DTO
`FinancialInputs` / `LoanSensitivityInput` с company_id и provenance, не данные,
автоматически извлечённые из новости или признаков продуктов. Публичный
`generate_insight` сохраняет прежнюю сигнатуру.

Владелец LLM обеспечивает согласованные тайм-ауты клиента, агентного задания и
worker, отмену HTTP и общий бюджет команды. По официальному кейсу бюджет моделей
общий, 500 рублей в день. Число запросов и output cap не обеспечивают денежный
предел, особенно при неизвестной тарификации reasoning. В production нужен
общий атомарный BudgetStore и актуальная подтверждённая цена модели. Настройки
клиента `input_token_bound` должны надёжно ограничивать весь provider request,
включая обёртку/system prompt; отношение символов к токенам этим не является.
Командные team_id, окно учёта и rate cards должны совпадать во всех процессах.
Настройки
180/400 и 8192/16384 — только явно выбираемые параметры локального стенда,
а не доказательство их принятия gateway.

В доступном `origin/Backend` (`fce14a4`) подтверждён дополнительный риск:
`services/automatic_insights.py:schedule_company_news()` перебирает все новости,
а `api/v1/feed.py` вызывает его при чтении ленты компании. Перед заменой mock
владелец backend должен ограничить постановку заданий, дедупликацию и денежный
бюджет. Иначе одно открытие ленты может запланировать платный анализ всего корпуса.
Эвристическая `rank_news()` агента сама по себе этот вызов API не ограничивает.

## Компании, платежи и идентификаторы: backend / ETL

`CompanyRepository.get_top_counterparties()` в
`backend/app/db/repositories/company_repo.py` возвращает отдельные ограниченные
входящие/исходящие записи без сортировки по сумме и полной агрегации. Агент принимает
эту выборку, но её нельзя называть полным top контрагентов. Уточнить договор:
направления, агрегация повторов, исключение self, период, валюта, порядок и coverage.
Дубли исходных платёжных строк должен разрешить владелец ETL; агент не может
угадать, являются они повторами или отдельными операциями.

Это состояние текущего checkout. В прочитанном `origin/feature/data-etl`
(`b42a5d5`, изменения `4cd2638`) тот же метод уже агрегирует платежи, исключает
self, присоединяет имена и выбирает крупнейших участников каждого направления;
добавлена защита повторной загрузки. Не дублировать эту реализацию в агенте.
После согласованного объединения владельцем перепроверить реальные DTO, coverage,
период/валюту и повторный импорт. Здесь ветки не переключались и не объединялись.

Передавать точный `CompanyResponse`, новость и ограниченные `CounterpartyItem`.
Не выбирать первый профиль при неоднозначном `SYNTHETIC_U`. Проверка доступа
пользователя к компании и скоуп поиска — обязанность API/repository. Агент
проверяет изоляцию полученных DTO и собственных follow-up, но не аутентифицирует
пользователя и не определяет принадлежность чужой строки без lineage входного API.

## Банковский поиск: второй backend

Поддерживаемый контракт:

```python
async def search(query: str, *, company: CompanyResponse,
                 event: NewsArticleResponse, limit: int) -> list[AnalyticsDocument]: ...
```

Возвращать `AnalyticsDocument(article=NewsArticleResponse(...), source_url=...,
provenance_status=..., source_document_id=..., source_page=..., source_hash=...)`.
Текст статьи должен быть доверенно извлечённой/сверенной выдержкой с контекстом,
без оборванного отрицания или условия. Сырой OCR не устанавливает достоверность.
`verified` задаётся только на основании доверенной процедуры проверки исходника.
Пустой список означает успешный поиск без совпадений. Ошибку сервиса нужно
возбуждать, а не маскировать пустым списком. Поиск должен соблюдать timeout/cancel.

Существующий `BackendAnalyticsSource.from_repository_factory()` лениво открывает
переданную async session только при явном поиске. Он использует
`NewsRepository.list_bank_analytics(limit)`: newest <=50, ограниченный лексический
поиск, без векторов и ACL. Если готов настоящий поиск второго backend, передать
его через этот протокол; не создавать ещё одну RAG-систему внутри агента.

Публикация должна иметь настоящую дату, устойчивый article ID, document ID и
версию/hash. Нельзя разным текстам давать один ID. Сохранять даты/периоды прогнозов;
исторический прогноз не является текущей позицией. Агент не объединяет несколько
банковских чисел в собственную числовую оценку и не доказывает, что разные
публикации согласуются семантически. Это задача источника/экспертной оценки.
Ответ с несколькими банковскими основаниями требует `source_comparisons` со
статусом `unresolved`; даты разных публикаций проверяются отдельно от целевого
периода прогноза. У адаптера не должно быть глобального изменяемого результата:
последующий запрос может добавить выдержку, но не заменить прежний текст/metadata
того же ID. Хеш полного возвращённого текста фиксируется в отчёте, отдельно от PDF hash.

## Grounded Q&A и сохранение: backend / frontend

`backend/app/api/v1/questions.py` сейчас формирует hardcoded ответ. Вместо него:

```python
from backend.app.agent import ManagementActionEngine

answer = await ManagementActionEngine(engine).answer_question(
    question,
    analysis=trusted_saved_analysis,
    company=authorized_company,
    tracer=tracer,
)
```

Загружать **серверный** `AgentAnalysisResult`, принадлежащий этой компании и
новости. Нельзя принимать произвольный companion с браузера: повторная проверка
не заменяет авторизацию или подпись доверенного результата. Q&A возвращает
источники/условные механизмы и `needs_data`, если ответа в сохранённом контексте
нет. Дополнительных LLM-вызовов и поиска нет. Для других действий использовать
`analyze_action(ManagementActionRequest(...), analysis=..., company=..., tracer=...)`.

Точный существующий API DTO — `schemas/question.py:QuestionAnswerResponse`.
Для первого подключения без изменения схемы владелец API может отобразить
результат в его строки следующим образом (после загрузки авторизованной компании
и доверенного companion; это инструкция, не внесённое изменение API):

```python
parts = [f"Статус: {answer.status}", answer.summary]
parts += [f"Нужны данные: {item}" for item in answer.missing_data]
parts += [f"Предложение: {item.title}. {item.rationale}" for item in answer.proposed_actions]
parts += answer.limitations + [answer.disclaimer]
return QuestionAnswerResponse(
    insight_id=request.insight_id,
    question=request.question,
    answer="\n".join(parts),
    relevant_source="; ".join(
        f"{ref.source_title} [{ref.fact_id}]" + (f" {ref.source_url}" if ref.source_url else "")
        for ref in answer.source_refs
    ) or None,
)
```

Нельзя оставлять нынешнее универсальное утверждение endpoint о том, что позиция
банка «подтверждает» операционную ликвидность: оно не выводится из источника.
Для показа дат/page/hash и структурированных действий потребуется согласованная
расширенная проекция API либо включение этих metadata в `relevant_source`.

Текущее `InsightRepository.save_insight()` сохраняет публичные поля отдельно.
Они не вмещают document/page/hash, полную verification, план и расчёты. Владелец
DB должен сохранять companion в доверенном JSON/sidecar вместе с company/event/run
и версией архитектуры. Если сохраняется только `MLInsightResult`, grounded Q&A
по проверенному контексту после перезапуска недоступен. Изменять общие schemas
ради первого подключения не требуется; richer API согласовать отдельно.

Frontend: постоянная точка входа в «аналитику событий», шесть текстовых блоков,
видимые условия и ограничения, отдельная позиция банка, источники/даты и форма
вопроса с `status`/`missing_data`. Не показывать `verification.passed` как знак
истинности или инвестиционную рекомендацию. Исходники frontend в проверенном
checkout отсутствуют, сквозной UI не проверялся.
В `origin/Backend` frontend уже присутствует; это не подтверждает его работу с
настоящим агентом. На прочитанном remote-срезе mock worker и заготовка Q&A остаются.

## Конкретные задачи подключения и владельцы

| Владелец | Место | Минимальное действие |
| --- | --- | --- |
| Наша часть агента | `tests/test_backend_integration_smoke.py` | Готово: контрактный smoke на DTO, repository bridge, HTTP transport и сохранённом companion. Production-код агента для этих контрактов менять не требуется. |
| Backend + владелец LLM | `main.py:lifespan`, `worker/tasks.py:set_ml_engine` | В composition root собрать runtime из фабрик свежих клиента/источника. Передать общий BudgetGuard с настоящим атомарным store, утверждёнными model ID, тарифом и корректным input bound. Согласовать HTTP timeout и AgentConfig; один только AgentConfig не меняет лимит клиента. |
| Backend worker + DB | `worker/tasks.py:run_insight_pipeline`, `db/repositories/insight_repo.py:save_insight` | Для сохранения Q&A-контекста вызвать `generate_analysis()` один раз; публичный результат взять из `.insight`, весь companion сохранить на сервере с insight/job/company/event и версией из report. Не вызывать следом `generate_insight()`: это второй платный анализ. Публичную запись и companion сохранять согласованно до SUCCESS; нынешний метод репозитория сам делает commit. |
| Backend API | `api/v1/questions.py:ask_follow_up_question`, `services/insight_service.py` | Загрузить доверенный companion и авторизованную компанию, восстановить `AgentAnalysisResult`, вызвать `ManagementActionEngine.answer_question`; отобразить summary, needs_data, ограничения и источники в текущий QuestionAnswerResponse. Не реконструировать companion из шести текстов. |
| Backend / банковские данные | `db/models/news.py:NewsArticle`, `db/repositories/news_repo.py:list_bank_analytics` | Предоставить доверенное соответствие article → URL/document/page/hash/статус проверки, например через resolver. Сейчас этих полей нет в NewsArticle. Без них поиск возможен, но источник остаётся unverified и не выдаётся за позицию банка. |
| Backend / ETL | `db/repositories/company_repo.py:get_top_counterparties` | Согласовать агрегаты и направления из ETL-ветки, исключение self, период/валюту/coverage. Текущий worker запрашивает limit=5: репозиторий даёт до пяти строк каждого направления, а не полный платёжный граф. |
| Backend / очередь | `api/v1/insights.py:create_insight_job`; в `origin/Backend` также `services/automatic_insights.py:schedule_company_news` и `api/v1/feed.py` | До платного подключения ограничить постановку заданий, дедуплицировать активные задания атомарно и включить общий бюджет. Текущий POST проверяет только уже сохранённый insight; повторный POST до его готовности может создать ещё одно задание. |
| Backend / доступ | `core/security.py:get_current_user_role`, API получения insight и Q&A | Роль из X-User-Role не подтверждает доступ к company_id/insight_id. Добавить проверку доступа к компании перед чтением анализа/источников и постановкой задания. Проверки ID внутри агента не заменяют API-авторизацию. |

В **текущем checkout** `GET /companies/{company_id}/feed` только читает и сортирует
новости через `FeedService`; генерация вызывается отдельным POST. В **прочитанном
origin/Backend** GET дополнительно вызывает `schedule_company_news`, где запрос
всех NewsArticle.id не имеет ограничения по видимой странице. Поэтому после
объединения с этой веткой простой замены mock недостаточно: открытие ленты может
поставить много платных заданий. Общий бюджет ограничит стоимость, но не делает
такую массовую постановку приемлемой. До этого исправления платную DI не включать.

На стороне агента подтверждены все обязательные поля MLInsightResult. По коду
репозиторий переносит их в Insight/Evidence, а InsightService возвращает через
InsightResponse. Служебные id/job/company/event/created_at добавляет backend.
Даты и page/hash источников, structured analysis, verification и usage в эту
публичную схему не входят: они сохраняются только в companion. Пока companion
store отсутствует, шесть блоков можно интегрировать отдельно, но grounded Q&A
после перезапуска, аудит verification и полный provenance остаются заблокированы.

Следующий шаг нашей части — совместно с владельцем backend повторить smoke в
его отдельной тестовой БД после DI и сохранения companion: запись → чтение → Q&A
после перезапуска. Подгонять DTO, менять RAG или переписывать агент до этого
оснований не найдено. Работающие локальные tests не подтверждают транзакции,
межпроцессный budget store, API ACL или frontend.

## Проверяемый порядок подключения

1. Без сети: shared HTTP `MockTransport` + реальный core + публичные DTO; эти тесты проходят.
2. Fake repository/session + поиск: dates, metadata, exception vs empty, company scope.
3. В отдельной тестовой БД владельца backend: startup worker, сохранение companion,
   два пользователя/компании, Q&A после перезапуска. Агентские проверки не трогают БД.
4. Только с новым явным согласием: B01, ручной разбор; затем B03 и S03 по очереди.
5. UI demo: данные → настоящее worker-задание → insight → источник → grounded вопрос.

До пунктов 3–5 полное приложение нельзя считать интегрированным MVP.
