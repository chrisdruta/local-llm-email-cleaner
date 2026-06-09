"""OAuth credential handling: refresh, fallback to interactive flow, caching."""

from __future__ import annotations

import dataclasses
import types

import pytest
from google.auth.exceptions import RefreshError

from local_llm_email_cleaner.config import DEFAULTS
from local_llm_email_cleaner.gmail import auth


class FakeCreds:
    def __init__(
        self,
        *,
        valid,
        expired=False,
        refresh_token=None,
        refresh_raises=False,
        json="{}",
    ):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self._refresh_raises = refresh_raises
        self._json = json
        self.refreshed = False

    def refresh(self, _request):
        if self._refresh_raises:
            raise RefreshError("token has been expired or revoked")
        self.valid = True
        self.refreshed = True

    def to_json(self):
        return self._json


def _cfg(tmp_path):
    cfg = dataclasses.replace(
        DEFAULTS,
        token_path=tmp_path / "token.json",
        credentials_path=tmp_path / "creds.json",
    )
    return cfg


def test_valid_token_short_circuits(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.token_path.write_text("{}")
    valid = FakeCreds(valid=True)
    monkeypatch.setattr(
        auth.Credentials, "from_authorized_user_file", lambda *a, **k: valid
    )
    assert auth.get_credentials(cfg) is valid


def test_expired_token_is_refreshed_and_recached(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.token_path.write_text("{}")
    creds = FakeCreds(
        valid=False, expired=True, refresh_token="r", json='{"refreshed":1}'
    )
    monkeypatch.setattr(
        auth.Credentials, "from_authorized_user_file", lambda *a, **k: creds
    )
    # If the flow is touched, fail loudly — a refresh must not re-authorize.
    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *a, **k: pytest.fail("must not run interactive flow"),
    )
    result = auth.get_credentials(cfg)
    assert result is creds and creds.refreshed
    assert cfg.token_path.read_text() == '{"refreshed":1}'


def test_refresh_failure_discards_token_and_reauthorizes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.token_path.write_text('{"stale":true}')
    cfg.credentials_path.write_text('{"installed":{}}')

    stale = FakeCreds(valid=False, expired=True, refresh_token="r", refresh_raises=True)
    fresh = FakeCreds(valid=True, json='{"fresh":true}')
    monkeypatch.setattr(
        auth.Credentials, "from_authorized_user_file", lambda *a, **k: stale
    )
    flow = types.SimpleNamespace(run_local_server=lambda **k: fresh)
    monkeypatch.setattr(
        auth.InstalledAppFlow, "from_client_secrets_file", lambda *a, **k: flow
    )

    result = auth.get_credentials(cfg)
    assert result is fresh  # fell through to the interactive flow
    assert cfg.token_path.read_text() == '{"fresh":true}'  # stale token replaced


def test_refresh_failure_without_client_file_raises_helpful_error(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)
    cfg.token_path.write_text('{"stale":true}')  # but no credentials.json
    stale = FakeCreds(valid=False, expired=True, refresh_token="r", refresh_raises=True)
    monkeypatch.setattr(
        auth.Credentials, "from_authorized_user_file", lambda *a, **k: stale
    )
    with pytest.raises(auth.MissingCredentialsError):
        auth.get_credentials(cfg)
    assert not cfg.token_path.exists()  # the unusable token was discarded
