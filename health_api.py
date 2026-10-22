"""Shared Google Health API (v4) client: OAuth credentials and REST calls."""

import os
import time
from datetime import date
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

BASE_URL = "https://health.googleapis.com/v4"
CLIENT_SECRET = Path(os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET", "client_secret.json"))
TOKEN_FILE = Path(os.environ.get("GOOGLE_HEALTH_TOKEN", "token.json"))

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.nutrition.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
]


class HealthApiError(Exception):
    pass


def _save_token(creds: Credentials) -> None:
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)


def login() -> Credentials:
    if not CLIENT_SECRET.exists():
        raise HealthApiError(f"Missing {CLIENT_SECRET}. Download a Desktop-app OAuth client JSON from Cloud Console.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    _save_token(creds)
    return creds


def load_session() -> AuthorizedSession:
    # CI (GitHub Actions): credentials come from environment secrets instead of token.json.
    if os.environ.get("GOOGLE_REFRESH_TOKEN"):
        creds = Credentials(
            None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            scopes=SCOPES,
        )
        try:
            creds.refresh(Request())
        except Exception as e:
            raise HealthApiError(f"Refresh token rejected ({e}). Run `uv run main.py login` then "
                                 "`uv run main.py push-secrets`.") from e
        return AuthorizedSession(creds)
    if not TOKEN_FILE.exists():
        raise HealthApiError("Not logged in. Run: uv run main.py login")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise HealthApiError("Stored token is unusable. Run: uv run main.py login")
        creds.refresh(Request())
        _save_token(creds)
    return AuthorizedSession(creds)


def call(session: AuthorizedSession, method: str, path: str, **kwargs) -> dict:
    for attempt in range(4):
        resp = session.request(method, f"{BASE_URL}/{path}", timeout=60, **kwargs)
        if resp.status_code not in (429, 500, 503) or attempt == 3:
            break
        time.sleep(2 ** attempt)  # back off on rate limits and transient errors
    if not resp.ok:
        raise HealthApiError(f"{method} {path}: {resp.status_code} {resp.reason}: {resp.text[:500]}")
    return resp.json()


def list_data_points(session, data_type: str, filter_: str | None = None, page_size: int | None = None,
                     all_pages: bool = False) -> list:
    params = {k: v for k, v in {"filter": filter_, "pageSize": page_size}.items() if v}
    points = []
    while True:
        body = call(session, "GET", f"users/me/dataTypes/{data_type}/dataPoints", params=params)
        points.extend(body.get("dataPoints", []))
        token = body.get("nextPageToken")
        if not (all_pages and token):
            return points
        params["pageToken"] = token


def daily_rollup(session, data_type: str, start: date, end: date, window_days: int = 1) -> dict:
    as_civil = lambda d: {"date": {"year": d.year, "month": d.month, "day": d.day}}
    payload = {"range": {"start": as_civil(start), "end": as_civil(end)}, "windowSizeDays": window_days}
    return call(session, "POST", f"users/me/dataTypes/{data_type}/dataPoints:dailyRollUp", json=payload)
