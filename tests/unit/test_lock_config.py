import pytest
from pydantic import ValidationError

from ragmonk.core.config import IndexingConfig


def test_default_lock_timeout() -> None:
    assert IndexingConfig().lock_timeout_seconds == 30


@pytest.mark.parametrize("bad", [0, -1, 100000])
def test_invalid_lock_timeout(bad: float) -> None:
    with pytest.raises(ValidationError):
        IndexingConfig(lock_timeout_seconds=bad)
