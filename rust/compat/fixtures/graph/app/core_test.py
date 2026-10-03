from app.util import normalize


def test_normalize():
    assert normalize(1.234) == 1.23
