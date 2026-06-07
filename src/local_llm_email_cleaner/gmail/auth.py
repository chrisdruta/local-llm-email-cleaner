"""OAuth flow + token caching for the Gmail API."""

from __future__ import annotations

import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from ..config import Config

logger = logging.getLogger(__name__)

# gmail.modify covers messages.trash and messages.modify but NOT
# messages.delete (which needs the full mail.google.com scope).
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class MissingCredentialsError(RuntimeError):
    pass


def get_credentials(cfg: Config) -> Credentials:
    creds: Credentials | None = None
    if cfg.token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(cfg.token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired Gmail token")
        creds.refresh(Request())
    else:
        if not cfg.credentials_path.is_file():
            raise MissingCredentialsError(
                f"No OAuth client file at {cfg.credentials_path}. Create a Google Cloud "
                "project, enable the Gmail API, create a 'Desktop app' OAuth client, and "
                "download its JSON there (see README)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(cfg.credentials_path), SCOPES
        )
        # open_browser=False: inside the devcontainer there is no browser; the
        # flow prints a localhost URL which VS Code port-forwards to the host.
        creds = flow.run_local_server(port=cfg.oauth_port, open_browser=False)

    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Token cached at %s", cfg.token_path)
    return creds


def get_service(cfg: Config):
    """Build the Gmail API service client."""
    return build("gmail", "v1", credentials=get_credentials(cfg), cache_discovery=False)
