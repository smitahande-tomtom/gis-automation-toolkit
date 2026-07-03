"""
Dumbo Hourly Status Agent
=========================
Polls the Dumbo production dashboard every hour and reports the run
statuses for a given user (default: hande).

Usage
-----
    python dumbo_status_agent.py [--username hande] [--password <pwd>]
                                 [--interval 60] [--log-file dumbo_status.log]
                                 [--once]

Options
-------
--username   Dumbo username to filter runs for  (default: hande)
--password   Dashboard password; if omitted you will be prompted at startup
--interval   Polling interval in minutes        (default: 60)
--log-file   Path to a log file                 (default: dumbo_status.log)
--once       Run a single check and exit (no scheduling loop)

Dependencies
------------
    pip install requests beautifulsoup4 schedule
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import time
from datetime import datetime
from typing import Any

try:
    import requests
    from requests import Session
except ImportError:
    sys.exit("Missing dependency: run  pip install requests")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: run  pip install beautifulsoup4")

try:
    import schedule
except ImportError:
    sys.exit("Missing dependency: run  pip install schedule")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DASHBOARD_BASE_URL = "https://dumbo-prod.maps-contentops.amiefarm.com"
DASHBOARD_URL = f"{DASHBOARD_BASE_URL}/dashboard"
LOGIN_URL = f"{DASHBOARD_BASE_URL}/login"

# HTML element hints — adjust these selectors to match the actual page markup.
# The agent will still log a clear warning if the selectors produce no results.
RUNS_TABLE_SELECTOR = "table"          # CSS selector for the runs table
RUN_ROW_SELECTOR = "tr"               # rows inside that table
USERNAME_COL_IDX = 0                  # column index that holds the username
STATUS_COL_IDX = 1                    # column index that holds the run status
RUN_ID_COL_IDX = 2                    # column index that holds the run ID / name
TIMESTAMP_COL_IDX = 3                 # column index for timestamp (if present)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("dumbo_agent")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def login(session: Session, username: str, password: str, logger: logging.Logger) -> bool:
    """
    Attempt to authenticate against the Dumbo dashboard.

    The function first fetches the login page to capture any CSRF token,
    then POSTs the credentials.  Adjust the field names
    ('username', 'password') to match the actual form.
    """
    logger.info("Authenticating as '%s' …", username)
    try:
        login_page = session.get(LOGIN_URL, timeout=30)
        login_page.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Could not reach login page: %s", exc)
        return False

    soup = BeautifulSoup(login_page.text, "html.parser")

    # Collect any hidden form fields (CSRF tokens, etc.)
    payload: dict[str, str] = {}
    form = soup.find("form")
    if form:
        for hidden in form.find_all("input", type="hidden"):
            if hidden.get("name"):
                payload[hidden["name"]] = hidden.get("value", "")

    # Add credentials — update field names to match the actual form
    payload["username"] = username
    payload["password"] = password

    # Determine the form action URL
    action = LOGIN_URL
    if form and form.get("action"):
        raw_action: str = form["action"]
        if raw_action.startswith("http"):
            action = raw_action
        else:
            action = DASHBOARD_BASE_URL.rstrip("/") + "/" + raw_action.lstrip("/")

    try:
        resp = session.post(action, data=payload, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Login POST failed: %s", exc)
        return False

    # Heuristic: if we're still on the login page the credentials were wrong
    if "/login" in resp.url:
        logger.error(
            "Authentication failed — still on login page (%s). "
            "Check your username/password.",
            resp.url,
        )
        return False

    logger.info("Authenticated successfully.")
    return True


# ---------------------------------------------------------------------------
# Dashboard scraping
# ---------------------------------------------------------------------------

def fetch_dashboard(session: Session, logger: logging.Logger) -> str | None:
    """Return the raw HTML of the dashboard page, or None on error."""
    try:
        resp = session.get(DASHBOARD_URL, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.error("Failed to fetch dashboard: %s", exc)
        return None


def parse_runs(html: str, username: str, logger: logging.Logger) -> list[dict[str, Any]]:
    """
    Parse the dashboard HTML and return a list of run records that belong to
    *username*.  Each record is a plain dict with at minimum:
        {run_id, username, status, timestamp}

    Adjust the selectors / column indices at the top of this file to match the
    actual page structure.  If the dashboard exposes a JSON API endpoint,
    replace this function with a direct API call instead.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(RUNS_TABLE_SELECTOR)

    if not table:
        logger.warning(
            "Could not find a runs table using selector '%s'. "
            "The dashboard markup may have changed — please inspect the page "
            "and update RUNS_TABLE_SELECTOR.",
            RUNS_TABLE_SELECTOR,
        )
        return []

    rows = table.select(RUN_ROW_SELECTOR)
    runs: list[dict[str, Any]] = []

    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue  # header or empty row

        def cell_text(idx: int) -> str:
            if idx < len(cells):
                return cells[idx].get_text(strip=True)
            return ""

        row_user = cell_text(USERNAME_COL_IDX).lower()
        if row_user != username.lower():
            continue

        runs.append(
            {
                "run_id": cell_text(RUN_ID_COL_IDX) or "—",
                "username": cell_text(USERNAME_COL_IDX),
                "status": cell_text(STATUS_COL_IDX) or "unknown",
                "timestamp": cell_text(TIMESTAMP_COL_IDX) or "—",
                "raw_cells": [c.get_text(strip=True) for c in cells],
            }
        )

    return runs


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

STATUS_EMOJI: dict[str, str] = {
    "running":   "🟡",
    "success":   "✅",
    "succeeded": "✅",
    "failed":    "❌",
    "failure":   "❌",
    "error":     "❌",
    "cancelled": "⛔",
    "pending":   "⏳",
    "queued":    "⏳",
    "unknown":   "❓",
}


def emoji_for(status: str) -> str:
    return STATUS_EMOJI.get(status.lower(), "❓")


def report_runs(runs: list[dict[str, Any]], username: str, logger: logging.Logger) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    separator = "─" * 70

    if not runs:
        logger.info("%s", separator)
        logger.info("Hourly Dumbo Status Report  (%s)", ts)
        logger.info("User     : %s", username)
        logger.info("Runs     : No runs found for this user on the dashboard.")
        logger.info("%s", separator)
        return

    logger.info("%s", separator)
    logger.info("Hourly Dumbo Status Report  (%s)", ts)
    logger.info("User     : %s", username)
    logger.info("Total    : %d run(s)", len(runs))
    logger.info("%s", separator)
    logger.info("  %-30s  %-15s  %s", "Run ID", "Status", "Timestamp")
    logger.info("  %-30s  %-15s  %s", "-" * 30, "-" * 15, "-" * 20)

    summary: dict[str, int] = {}
    for run in runs:
        status = run["status"]
        icon = emoji_for(status)
        logger.info(
            "  %-30s  %s %-13s  %s",
            run["run_id"],
            icon,
            status,
            run["timestamp"],
        )
        summary[status] = summary.get(status, 0) + 1

    logger.info("%s", separator)
    logger.info(
        "Summary: %s",
        "  |  ".join(f"{emoji_for(s)} {s}: {n}" for s, n in sorted(summary.items())),
    )
    logger.info("%s", separator)


# ---------------------------------------------------------------------------
# Core check function
# ---------------------------------------------------------------------------

def check_once(
    session: Session,
    username: str,
    logger: logging.Logger,
    reauth_callback,
) -> None:
    """Fetch the dashboard and log the status for *username*."""
    html = fetch_dashboard(session, logger)

    if html is None:
        logger.warning("Dashboard fetch failed — will retry next cycle.")
        return

    # Detect session expiry (redirect back to login page)
    if "/login" in html[:2000].lower() or "log in" in html[:2000].lower():
        logger.warning("Session expired — re-authenticating …")
        if not reauth_callback():
            logger.error("Re-authentication failed. Skipping this cycle.")
            return
        html = fetch_dashboard(session, logger)
        if html is None:
            return

    runs = parse_runs(html, username, logger)
    report_runs(runs, username, logger)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hourly Dumbo run status agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--username",
        default="hande",
        help="Dumbo username whose runs to monitor (default: hande)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Dashboard password (prompted at startup if omitted)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Polling interval in minutes (default: 60)",
    )
    parser.add_argument(
        "--log-file",
        default="dumbo_status.log",
        help="Path to the log file (default: dumbo_status.log)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single status check and exit",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    logger = setup_logging(args.log_file)

    password = args.password
    if not password:
        password = getpass.getpass(f"Password for Dumbo user '{args.username}': ")

    session = requests.Session()
    session.headers.update({"User-Agent": "DumboStatusAgent/1.0"})

    def do_login() -> bool:
        return login(session, args.username, password, logger)

    if not do_login():
        sys.exit(1)

    if args.once:
        check_once(session, args.username, logger, do_login)
        return

    logger.info(
        "Dumbo status agent started — checking every %d minute(s) for user '%s'.",
        args.interval,
        args.username,
    )

    # Run immediately, then schedule recurring checks
    check_once(session, args.username, logger, do_login)

    schedule.every(args.interval).minutes.do(
        check_once,
        session=session,
        username=args.username,
        logger=logger,
        reauth_callback=do_login,
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")


if __name__ == "__main__":
    main()
