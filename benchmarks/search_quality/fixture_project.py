"""The one fixed, hand-written project every golden query in
``benchmarks/search/golden_queries.yaml`` is evaluated against --
shared by ``tests/integration/test_search_quality.py`` (the always-on
quality regression test) and ``report.py`` (the baseline report
generator), so there is exactly one place that can drift out of sync
with the golden query set instead of two.

Three code files (a settlement service, its consumer, and a retry
worker) and three documents (a settlement guide with two Markdown
tables, a set of health notes with a table of its own, and a payment
provider notes doc) -- small enough to index in well under a second
through the real Tree-sitter/Docling pipeline, but with enough real
cross-references (a class name mentioned in prose, a heading, a table
cell, a shared vocabulary word across two documents) to back every
golden query category: exact title/heading/symbol/path lookups, keyword
and natural-language search, table questions, cross-document queries,
code-to-document queries, and partial/typo'd terms. See that YAML
file's own header comment for exactly which query relies on which piece
of content below -- changing a name, heading, or table value here
without updating it there will silently break golden queries.
"""

from __future__ import annotations

from pathlib import Path


def write_project(root: Path) -> None:
    services = root / "services"
    services.mkdir(parents=True)
    (services / "settlement_service.py").write_text(
        "class SettlementService:\n"
        "    def process(self):\n"
        "        return self.retry_settlement()\n"
        "\n"
        "    def retry_settlement(self):\n"
        "        return 'retried'\n"
    )

    consumers = root / "consumers"
    consumers.mkdir()
    (consumers / "payment_consumer.py").write_text(
        "from services.settlement_service import SettlementService\n\n\n"
        "class PaymentConsumer:\n"
        "    def handle(self):\n"
        "        service = SettlementService()\n"
        "        return service.process()\n"
    )

    workers = root / "workers"
    workers.mkdir()
    (workers / "retry_worker.py").write_text(
        "from services.settlement_service import SettlementService\n\n\n"
        "class RetryWorker:\n"
        "    def run(self):\n"
        "        service = SettlementService()\n"
        "        return service.retry_settlement()\n"
    )

    docs = root / "docs"
    docs.mkdir()
    (docs / "settlement_guide.md").write_text(
        "# Settlement Guide\n\n"
        "Provider settlements are retried automatically by the retry worker "
        "whenever a payment attempt times out. The SettlementService "
        "coordinates the retry logic.\n\n"
        "## Retry Policy\n\n"
        "PaymentConsumer invokes SettlementService.process whenever a new "
        "settlement request arrives. RetryWorker invokes "
        "SettlementService.retry_settlement whenever a provider payment "
        "attempt times out. Each provider is allowed a maximum number of "
        "retry attempts before the settlement is marked failed.\n\n"
        "## Retry Limits by Provider\n\n"
        "| Provider | Max Retries | Timeout Seconds |\n"
        "| --- | --- | --- |\n"
        "| Stripe | 5 | 30 |\n"
        "| Adyen | 3 | 45 |\n"
        "| PayPal | 4 | 20 |\n"
    )
    (docs / "health_notes.md").write_text(
        "# Health Notes\n\n"
        "## Cholesterol\n\n"
        "Cholesterol measurements should be taken annually. LDL cholesterol "
        "levels above 160 mg/dL indicate elevated cardiovascular risk.\n\n"
        "## Blood Pressure Ranges\n\n"
        "| Category | Systolic | Diastolic |\n"
        "| --- | --- | --- |\n"
        "| Normal | 120 | 80 |\n"
        "| Elevated | 130 | 80 |\n"
        "| High | 140 | 90 |\n"
    )
    (docs / "payment_provider_notes.md").write_text(
        "# Payment Provider Notes\n\n"
        "## Provider Timeout Behavior\n\n"
        "Stripe and Adyen each enforce their own timeout window before a "
        "payment attempt is considered failed. When a provider times out, "
        "the retry worker schedules another settlement attempt through "
        "SettlementService.\n\n"
        "## Escalation\n\n"
        "Repeated provider timeouts across settlement retries should be "
        "escalated to the on-call payments engineer.\n"
    )
