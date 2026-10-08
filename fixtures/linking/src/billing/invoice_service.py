"""Invoice service."""


class InvoiceService:
    def create_invoice(self, order):
        return order

    def void_invoice(self, invoice_id):
        return invoice_id


def calculate_tax(amount):
    return amount * 0.2


@app.post("/api/invoices/{id}/void")  # noqa: F821
def void_endpoint(id):
    return id
