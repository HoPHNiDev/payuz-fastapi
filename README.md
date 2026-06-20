# payuz-fastapi

Async, **FastAPI-only** payment-gateway integration for Uzbekistan: **Payme, Click, Uzum,
Paynet, Octo**. A stripped-down, async-native fork of `paytechuz` — no Django, no license layer,
and a **Base-agnostic** transaction model so it plugs into your own SQLAlchemy `Base` + Alembic.

- Import name: `payuz` · Distribution: `payuz-fastapi`
- DB layer: `sqlalchemy[asyncio]` (`AsyncSession`)
- Webhook handlers expose `async` event hooks you override to drive your own order logic.

## Install

```bash
pip install "payuz-fastapi @ git+ssh://git@github.com/HoPHNiDev/payuz-fastapi.git"
```

## Transaction model

Bind the mixin to **your** declarative `Base` and manage the schema with **your** migrations:

```python
from yourapp.db import Base
from payuz.integrations.fastapi import PaymentTransactionMixin

class PaymentTransaction(Base, PaymentTransactionMixin):
    __tablename__ = "payments"
```

Columns: `id, gateway, transaction_id, account_id, amount, state, reason, extra_data,
created_at, updated_at, performed_at, cancelled_at`.
States: `CREATED=0, INITIATING=1, SUCCESSFULLY=2, CANCELLED=-2, CANCELLED_DURING_INIT=-1`.

Small projects without their own Base can use the ready-made standalone model and helper:

```python
from payuz.integrations.fastapi import PaymentTransaction, run_migrations  # standalone
await run_migrations(async_engine)  # creates the `payments` table
```

## Webhook handler

```python
from payuz.integrations.fastapi import PaymeWebhookHandler

class MyPaymeHandler(PaymeWebhookHandler):
    async def successfully_payment(self, params, transaction):
        await mark_order_paid(transaction.account_id)

    async def cancelled_payment(self, params, transaction):
        await cancel_order(transaction.account_id)

    async def before_cancel_transaction(self, params, transaction):
        # raise TransactionCompleted(...) to refuse cancelling a delivered order (Payme -31007)
        ...

@app.post("/payments/payme")
async def payme_webhook(request: Request, db: AsyncSession = Depends(get_session)):
    handler = MyPaymeHandler(
        db=db, payme_id=PAYME_ID, payme_key=PAYME_KEY,
        account_model=Order, account_field="id", amount_field="amount",
        transaction_model=PaymentTransaction,
    )
    return await handler.handle_webhook(request)
```

Handlers: `PaymeWebhookHandler`, `ClickWebhookHandler`, `UzumWebhookHandler`,
`PaynetWebhookHandler`, `OctoWebhookHandler` — all in `payuz.integrations.fastapi`.

Gateway clients (build pay links / call provider APIs) live in `payuz`:
`PaymeGateway, ClickGateway, UzumGateway, PaynetGateway, OctoGateway, create_gateway`.
