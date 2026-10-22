"""Google Health API CLI.

Setup: put an OAuth "Desktop app" client in client_secret.json, then:
    uv run main.py login
    uv run main.py list steps --filter 'steps.interval.civil_start_time >= "2026-10-01"'
    uv run main.py rollup steps --start 2026-10-14 --end 2026-10-21
Dashboard: uv run dashboard.py
"""

import argparse
import json
import subprocess
import sys
from datetime import date

import health_api


def push_secrets() -> None:
    """Send client id/secret and refresh token to GitHub via stdin, so they never appear in argv or logs."""
    installed = json.loads(health_api.CLIENT_SECRET.read_text())["installed"]
    token = json.loads(health_api.TOKEN_FILE.read_text())
    for name, value in [("GOOGLE_CLIENT_ID", installed["client_id"]),
                        ("GOOGLE_CLIENT_SECRET", installed["client_secret"]),
                        ("GOOGLE_REFRESH_TOKEN", token["refresh_token"])]:
        subprocess.run(["gh", "secret", "set", name], input=value, text=True, check=True)
    print("Secrets updated. Trigger a rebuild with: gh workflow run publish.yml")


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Health API client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="Run the OAuth consent flow and store token.json")
    sub.add_parser("push-secrets", help="Upload Google credentials to this repo's GitHub Actions secrets (gh CLI)")

    p_list = sub.add_parser("list", help="List raw data points for a data type")
    p_list.add_argument("data_type", help="e.g. steps, heart-rate, sleep, exercise, weight")
    p_list.add_argument("--filter", help='AIP-160 filter, e.g. \'steps.interval.civil_start_time >= "2026-10-01"\'')
    p_list.add_argument("--page-size", type=int)
    p_list.add_argument("--all", action="store_true", help="Follow nextPageToken until exhausted")

    p_roll = sub.add_parser("rollup", help="Daily aggregates for a data type")
    p_roll.add_argument("data_type")
    p_roll.add_argument("--start", type=date.fromisoformat, required=True)
    p_roll.add_argument("--end", type=date.fromisoformat, default=date.today())
    p_roll.add_argument("--window-days", type=int, default=1)

    args = parser.parse_args()

    try:
        if args.cmd == "login":
            health_api.login()
            print(f"Logged in; token saved to {health_api.TOKEN_FILE}")
            return
        if args.cmd == "push-secrets":
            push_secrets()
            return
        session = health_api.load_session()
        if args.cmd == "list":
            result = health_api.list_data_points(session, args.data_type, args.filter, args.page_size, args.all)
        else:
            result = health_api.daily_rollup(session, args.data_type, args.start, args.end, args.window_days)
    except health_api.HealthApiError as e:
        sys.exit(str(e))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
