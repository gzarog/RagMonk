"""Call chains, a cycle, and calls to names defined nowhere."""

from app.util import normalize


class Ledger:
    def post(self, amount):
        return self.validate(amount)

    def validate(self, amount):
        return normalize(amount)

    def ping(self):
        return self.pong()

    def pong(self):
        return self.ping()


def settle(ledger, amount):
    ledger.post(amount)
    audit_log(amount)
    return normalize(amount)


def run():
    settle(Ledger(), 10)
    external_hook()
