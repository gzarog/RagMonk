# Settlement Guide

Provider settlements are retried automatically by the retry worker whenever a payment attempt times out. The SettlementService coordinates the retry logic.

## Retry Policy

PaymentConsumer invokes SettlementService.process whenever a new settlement request arrives. RetryWorker invokes SettlementService.retry_settlement whenever a provider payment attempt times out. Each provider is allowed a maximum number of retry attempts before the settlement is marked failed.

## Retry Limits by Provider

| Provider | Max Retries | Timeout Seconds |
| --- | --- | --- |
| Stripe | 5 | 30 |
| Adyen | 3 | 45 |
| PayPal | 4 | 20 |
