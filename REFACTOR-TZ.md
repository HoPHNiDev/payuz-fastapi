# ТЗ: `payuz-fastapi` — FastAPI-only форк `paytechuz`

> Цель: из форка `paytechuz` сделать чистую third-party библиотеку **только под FastAPI
> (async)**, без Django, без лицензионных проверок, с Base-агностичной моделью транзакции и
> вебхуками под все шлюзы. Затем подключить её в Cupid вместо самописной интеграции
> payme/click. Дистрибутив — `payuz-fastapi`, import root — `payuz`.

Решения (зафиксированы):
- **Async-native only** — FastAPI-интеграция работает на `AsyncSession` (`await` к БД).
- **Import name `payuz`** — `from payuz.integrations.fastapi import ...`; distribution `payuz-fastapi`.

---

## Часть A. Рефакторинг библиотеки `payuz-fastapi`

### A0. Структура и упаковка
- `git mv src payuz` — корень пакета `payuz/` (import = `payuz`).
- Во ВСЕХ файлах заменить `paytechuz` → `payuz` в импортах (`from payuz.core...`, и т.д.).
- Добавить корневой `pyproject.toml`:
  - `[project] name="payuz-fastapi"`, version из `payuz/__init__.py`.
  - Зависимости (runtime): `fastapi`, `sqlalchemy>=2`, `httpx`, `pydantic>=2`, `python-multipart`,
    `requests` (для исходящих gateway-клиентов), `environs`.
  - `[tool.setuptools.packages.find]` → находит `payuz*`.
- `README.md` — минимальная инструкция установки/использования.
- Удалить `python` mention о Flask/Django из метаданных и `__init__`.

### A1. Удалить Django-интеграцию
- Удалить директорию `payuz/integrations/django/` целиком — **после** переноса логики
  вебхуков uzum/paynet/octo во FastAPI (см. A5).
- `payuz/__init__.py`: убрать `HAS_DJANGO`, `HAS_FLASK` (оставить `HAS_FASTAPI` либо убрать
  все флаги), убрать импорт/экспорт license-функций.
- `payuz/core/dependencies.py`: убрать ветки `django`/`flask`; оставить только `fastapi`
  (его deps: fastapi, sqlalchemy, httpx, pydantic, python-multipart). Сообщения без
  `pip install paytechuz[...]` → `payuz-fastapi`.
- `payuz/factory.py`: оставить как есть (gateway-клиенты framework-agnostic).

### A2. Удалить лицензионные проверки (paytechuz license)
- Удалить файл `payuz/license.py`.
- `payuz/core/exceptions.py`: удалить классы `MissingLicenseKeyError`, `InvalidLicenseKeyError`
  и убрать их из `exception_whitelist`.
- Gateway internals — убрать `from ...license import _validate_license_api_key` и вызов
  `_validate_license_api_key()` в `__init__`:
  - `payuz/gateways/payme/internal.py` (импорт стр.10, вызов стр.22)
  - `payuz/gateways/click/internal.py` (импорт стр.9, вызов стр.22)
  - `payuz/gateways/paynet/internal.py` (импорт стр.8, вызов стр.18)
  - (uzum/octo internals лицензию не дёргают)
- FastAPI `internal.py`/`routes.py`: убрать `_license_error_response`, импорты license-исключений
  и `except (MissingLicenseKeyError, InvalidLicenseKeyError)`-блоки.

### A3. Base-агностичная модель транзакции (async)
Их колонки оставляем как «правильные»: `gateway, transaction_id, account_id, amount, state,
reason, extra_data, created_at, updated_at, performed_at, cancelled_at` + state-константы
(`CREATED=0, INITIATING=1, SUCCESSFULLY=2, CANCELLED=-2, CANCELLED_DURING_INIT=-1`).

`payuz/integrations/fastapi/models.py` переписать:
- **`PaymentTransactionMixin`** — declarative-mixin: только `Column`-определения + state-константы
  + методы `create_transaction`/`mark_as_paid`/`mark_as_cancelled` как **async** (принимают
  `AsyncSession`, делают `await db.execute(select(...))`, `await db.flush()`/`commit()`).
  НЕ привязан к какому-либо `Base` — потребитель примешивает его к своему `Base`.
- **`PaymentTransaction`** — готовая конкретная модель на ВНУТРЕННЕМ `Base = declarative_base()`
  для standalone-использования (мелкие проекты без своего Base). На неё опираются дефолтные
  хэндлеры/`routes.py` если потребитель не передал свою модель.
- Хэндлеры должны принимать `transaction_model` (класс модели) параметром — чтобы Cupid
  передал свою модель на своём Base, и library не «крутилась вокруг своего Base».
- `run_migrations(engine)` → async-вариант (`async with engine.begin(): await conn.run_sync(
  Base.metadata.create_all)`), но это только для standalone; проекты со своим Alembic не зовут.

Миграции: библиотека НЕ навязывает свои миграции. Потребитель (Cupid) создаёт таблицу `payments`
своей Alembic-миграцией по схеме mixin'а. Документировать DDL в README.

### A4. Async вебхуки payme + click
`payuz/integrations/fastapi/internal.py` — переписать на `AsyncSession`:
- `db: AsyncSession`; все `db.query(...).filter(...).first()` → `await db.execute(select(...))`
  + `.scalar_one_or_none()/.scalars().all()`; `db.commit()/refresh()` → `await db.commit()` и т.п.
- Event-хуки сделать `async def` (потребитель в них дергает свою async-бизнес-логику):
  `transaction_created`, `successfully_payment`, `cancelled_payment`, `check_transaction`,
  `before_check_perform_transaction`, `transaction_already_exists`, `get_statement`.
- **Новый async-хук `before_cancel_transaction(params, transaction, account=None)`** в payme-
  хэндлере: вызывается ПЕРЕД мутацией в `_cancel_transaction`; потребитель может бросить
  исключение, чтобы отказать в отмене (Cupid → код -31007 «услуга оказана»). По умолчанию no-op.
- Идемпотентность отмены уже есть (повтор Cancel по отменённой → возврат как есть) — сохранить.
- `transaction_model` вместо хардкода `PaymentTransaction`.

`payuz/integrations/fastapi/routes.py` — публичные `PaymeWebhookHandler`/`ClickWebhookHandler`
делают `async`-хуки; убрать license-обёртки; `router` оставить как пример (опционально).

### A5. Перенос uzum/paynet/octo во FastAPI (async)
По образцу Django `internal_webhooks/{uzum,paynet,octo}.py` реализовать async-хэндлеры во
FastAPI с тем же контрактом (хуки + `transaction_model` + `AsyncSession`):
- **uzum**: check/create/perform/cancel/get_statement (по их протоколу).
- **paynet**: PerformTransaction/CheckTransaction/CancelTransaction/GetStatement.
- **octo**: callback со sha1-проверкой подписи, статусы SUCCEEDED/CANCELED/REFUNDED/…
- Схемы (`schemas.py`) и экспорты (`__init__.py`) дополнить. Только после этого удалить Django.

### A6. Self-check библиотеки
- `python -c "import payuz; from payuz.integrations.fastapi import PaymeWebhookHandler, ClickWebhookHandler"`.
- Никаких top-level импортов django.
- (Опц.) лёгкие unit-тесты на подпись click / роутинг payme-методов с фейковой AsyncSession.

---

## Часть B. Интеграция в Cupid (замена самописной payme/click)

### B1. Установка
- В dev: editable-инсталл локального пути (`pip install -e ../payuz-fastapi`) для итераций;
  в `requirements.txt` финально — `payuz-fastapi @ git+ssh://git@github.com/HoPHNiDev/payuz-fastapi.git`.

### B2. Модель платежей
- Завести в Cupid модель `PaymentTransaction(Base, PaymentTransactionMixin)` на `common.models.base.Base`
  (имя таблицы `payments`). Удалить старую `common/models/payment.py` (`Payment`) и её enum-зависимости,
  где они больше не нужны.
- Alembic-миграция: drop старой таблицы `payments` (она ещё без боевых данных — pre-launch) и
  создать новую по схеме mixin'а (gateway/transaction_id/account_id/amount/state/reason/extra_data/
  created_at/updated_at/performed_at/cancelled_at, нужные индексы). Учесть downgrade.
- Репозитории/UoW: заменить `uow.payments` на работу с новой моделью (или адаптер).

### B3. Вебхуки
- `app/payments/routers.py`: endpoint'ы `/payments/payme` и `/payments/click` теперь делегируют
  в наши подклассы `PaymeWebhookHandler`/`ClickWebhookHandler` из payuz (передаём `AsyncSession`
  из UoW, `account_model=Order`, `account_field='public_id'`, `amount_field='price_amount'`,
  ключи из настроек).
- В подклассах переопределяем async-хуки:
  - `successfully_payment` → `OrderService.on_paid(order_id)`.
  - `cancelled_payment` → перевод заказа в CANCELLED/REFUNDED (через FSM).
  - `before_cancel_transaction` → если заказ уже GENERATING/READY/DELIVERED → бросаем
    исключение → отказ -31007 (сохраняем поведение прошлой задачи).
- Checkout-ссылки: можно использовать gateway-клиенты payme/click из payuz (`generate_pay_link`/
  `create_payment`) или оставить текущую сборку URL. Привести account-параметр к `public_id`.

### B4. Чистка нативной логики
- Удалить `app/payments/providers/payme.py`, `app/payments/providers/click.py`,
  `app/payments/services.py` (PaymentService), `app/payments/exceptions.py` (payme/click-специфика)
  — заменить тонкими подклассами payuz + хуками к OrderService. Сохранить reconcile-beat
  (переписав на новую модель) и dev-confirm endpoint.
- `app/core/settings.py`: payme/click ключи остаются (их принимает payuz-хэндлер).

### B5. Тесты
- Снести `tests/payments/test_payme.py`, `test_cancel_logic.py`, `test_click_sign.py` (они
  тестировали самописную логику) и заменить тестами на наши payuz-подклассы (хуки → OrderService,
  -31007, идемпотентность). `test_payments_reconcile.py` — обновить под новую модель.
- Прогнать весь `pytest` зелёным.

---

## Порядок исполнения
A0 → A2 → A3 → A4 → A5 → A1(удаление django) → A6 → B2 → B3 → B4 → B5.
(Сначала чистка/перенос внутри библиотеки, удаление Django — последним из A; затем Cupid.)
