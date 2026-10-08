from app.core import Ledger, settle
from app.util import normalize


def test_settle():
    assert settle(Ledger(), 1) == normalize(1)


def test_post():
    Ledger().post(5)
