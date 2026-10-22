"""Build a static, password-encrypted snapshot of the dashboard for GitHub Pages.

    DASHBOARD_PASSWORD=... uv run build_site.py --out _site

All data is encrypted with AES-256-GCM using a key derived from the password
(PBKDF2-SHA256); the page decrypts it in the browser. Nothing readable is published.
"""

import argparse
import base64
import json
import os
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import dashboard
import health_api
import insights

PBKDF2_ITERATIONS = 2_000_000  # slows offline guessing; ~1 s to unlock in a browser
ROOT = Path(__file__).parent


def slice_days(data: dict, days: int) -> dict:
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    return {
        **data,
        "range": {**data["range"], "start": start, "days": days},
        "days": [d for d in data["days"] if d["date"] >= start],
        "sleep": [s for s in data["sleep"] if (s["date"] or "") >= start],
        "exercise": [e for e in data["exercise"] if (e["start"] or "")[:10] >= start],
    }


def snapshot() -> dict:
    session = health_api.load_session()
    today = date.today()
    data90 = dashboard.collect(session, 90)
    body = insights.fetch_body(session, today - timedelta(days=90))
    hr_raw = insights.fetch_hr_days(session, [today - timedelta(days=i) for i in range(7, -1, -1)])
    complete = {d: v for d, v in hr_raw.items() if d < today.isoformat()}
    live = dashboard.heart_rate_samples(session, 12)
    live["samples"] = live["samples"][::3]  # ~30 s resolution keeps the file small
    return {
        "builtAt": live["fetchedAt"],
        "dashboard": {str(n): slice_days(data90, n) for n in (7, 30, 90)},
        "insights": {str(n): insights.compute(slice_days(data90, n), body, complete) for n in (14, 30, 90)},
        "heartrate": {str(h): {**live, "hours": h,
                               "samples": [s for s in live["samples"] if s["t"] >= _hours_ago(h)]} for h in (1, 3, 12)},
        "heartart": dashboard.heart_art_from_samples(hr_raw),
    }


def _hours_ago(h: int) -> str:
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def encrypt(payload: dict, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, json.dumps(payload, separators=(",", ":")).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="_site")
    args = parser.parse_args()
    password = os.environ.get("DASHBOARD_PASSWORD")
    if not password or len(password) < 6:
        sys.exit("Set DASHBOARD_PASSWORD (6+ characters; longer is much safer).")

    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    try:
        data = snapshot()
    except health_api.HealthApiError as e:
        sys.exit(str(e))
    (out / "data.enc.json").write_text(json.dumps(encrypt(data, password)))
    html = (ROOT / "static" / "index.html").read_text()
    html = html.replace("<script>", '<script>window.FITBIT_STATIC = "data.enc.json";</script>\n<script>', 1)
    (out / "index.html").write_text(html)
    (out / ".nojekyll").write_text("")
    errors = {k: v for n in data["dashboard"].values() for k, v in n["errors"].items()}
    print(f"Built {out}/ at {data['builtAt']} "
          f"({(out / 'data.enc.json').stat().st_size / 1024:.0f} KB encrypted){'; errors: ' + ', '.join(errors) if errors else ''}")


if __name__ == "__main__":
    main()
