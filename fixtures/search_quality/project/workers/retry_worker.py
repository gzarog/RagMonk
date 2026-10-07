from services.settlement_service import SettlementService


class RetryWorker:
    def run(self):
        service = SettlementService()
        return service.retry_settlement()
