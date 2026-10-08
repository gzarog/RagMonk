from services.settlement_service import SettlementService


class PaymentConsumer:
    def handle(self):
        service = SettlementService()
        return service.process()
