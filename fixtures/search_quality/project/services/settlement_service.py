class SettlementService:
    def process(self):
        return self.retry_settlement()

    def retry_settlement(self):
        return 'retried'
