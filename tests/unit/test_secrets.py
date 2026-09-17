from __future__ import annotations

from ragmonk.security.secrets import is_secret_filename


def test_env_file_is_secret() -> None:
    assert is_secret_filename(".env") is True


def test_pem_key_is_secret() -> None:
    assert is_secret_filename("server.pem") is True
    assert is_secret_filename("id_rsa") is True


def test_ordinary_source_file_is_not_secret() -> None:
    assert is_secret_filename("main.py") is False
