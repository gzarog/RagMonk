# Payment Provider Notes

## Provider Timeout Behavior

Stripe and Adyen each enforce their own timeout window before a payment attempt is considered failed. When a provider times out, the retry worker schedules another settlement attempt through SettlementService.

## Escalation

Repeated provider timeouts across settlement retries should be escalated to the on-call payments engineer.
