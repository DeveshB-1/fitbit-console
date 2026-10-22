# Fitbit Console

A personal dashboard for Fitbit data from the [Google Health API](https://developers.google.com/health):
daily activity, sleep stages, heart rate, workouts, science-based derived metrics,
heartbeat generative art, and a fun zone (facts, sonification, a guessing game).

## Run locally

```sh
uv run main.py login        # needs client_secret.json (Google Cloud "Desktop app" OAuth client)
uv run dashboard.py         # http://127.0.0.1:8765
uv run dashboard.py --demo  # synthetic data, no credentials
```

## Hosted on GitHub Pages (encrypted)

`.github/workflows/publish.yml` runs hourly (and on demand). It fetches your data with a
refresh token stored in GitHub Actions secrets, encrypts everything with AES-256-GCM
(PBKDF2-SHA256, 600k iterations) using `DASHBOARD_PASSWORD`, and publishes only the
ciphertext plus the page. The browser decrypts it after you enter the password.

Secrets: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `DASHBOARD_PASSWORD`.
Variable: `FITBIT_SEX` (`male`/`female`, used by two formulas).

```sh
uv run main.py push-secrets   # upload Google credentials from client_secret.json + token.json
gh workflow run publish.yml   # rebuild now
```

While the Google OAuth app is in **Testing** mode its refresh tokens expire after 7 days;
re-run `uv run main.py login && uv run main.py push-secrets` when the workflow starts failing.

Derived metrics are estimates from population equations, not medical advice.
