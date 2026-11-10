"""Production handler registration.

Registered: ``clickup.create_task`` (app/integrations/clickup.py),
``slack.notify`` (app/integrations/slack.py) and ``sheets.upsert_project``
(app/integrations/sheets.py).

The worker only claims job types that have a handler; a future job type
without an adapter stays ``pending`` (untouched) rather than being marked
succeeded or dead just because code is missing.

Both handlers are always registered. If an integration is not configured, its
jobs fail with a permanent *_NOT_CONFIGURED result (dead, retryable by
an administrator later); nothing else is affected.

Every adapter must:
* keep its total run time well inside WORKER_LEASE_SECONDS (HTTP timeouts
  shorter than the lease, which the settings enforce);
* return IntegrationResult, mapping failures with retry.classify_http_status,
  and report ``unknown`` whenever the request may have reached the provider
  but no definitive answer came back;
* be idempotent per ctx.idempotency_key, and reconcile (look the resource up)
  before creating after any ``unknown`` attempt;
* never log or return credentials, webhook URLs or customer data.
"""

from app.config import Settings
from app.jobs.registry import HandlerRegistry


def build_registry(settings: Settings) -> HandlerRegistry:
    from app.integrations.clickup import ClickUpAdapter
    from app.integrations.sheets import SheetsAdapter
    from app.integrations.slack import SlackAdapter

    registry = HandlerRegistry()
    registry.register(ClickUpAdapter(settings).handler())
    registry.register(SheetsAdapter(settings).handler())
    registry.register(SlackAdapter(settings).handler())
    return registry
