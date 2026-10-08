# Billing guide

The `InvoiceService` creates invoices through `create_invoice`. Fully qualified,
that is `src.billing.invoice_service.InvoiceService.create_invoice`, and in short
form `InvoiceService.void_invoice`.

## Tax

Use calculate_tax for VAT. See invoice_service.py and the ledger module, and the
helper in my-helpers.v2.py.

## Endpoints

POST /api/invoices/{id}/void voids an invoice. Payments use api/payments/charge.

| Component | Owner |
| --- | --- |
| Ledger | finance |
| PaymentGateway | platform |
