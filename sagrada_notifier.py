#!/usr/bin/env python3
"""
Watch Sagrada Familia ticket calendar dates and notify when sold-out days reopen.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


AUTH_URL = "https://services.clorian.com/user/api/oauth/token"
CATALOG_URL = "https://services.clorian.com/catalog"
TICKET_URL = "https://tickets.sagradafamilia.org/en/1-individual/4375-sagrada-familia"
ORIGIN = "https://tickets.sagradafamilia.org"

POS_ID = "649"
SALES_GROUP_ID = "1"
PRODUCT_ID = "4375"
API_KEY = "thesagradafamiliafrontendoftomorrow"

DEFAULT_STATE = Path("sagrada_state.json")
DEFAULT_CONFIG = Path("sagrada_config.json")


class NotifierError(RuntimeError):
    pass


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM-DD, got {value!r}") from exc


def month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    current = start.replace(day=1)
    months = []
    while current <= end:
        months.append(current)
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def request_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    transport: str = "auto",
) -> Any:
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en",
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "Referer": TICKET_URL,
        "User-Agent": "sagrada-ticket-notifier/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["pos"] = POS_ID

    if transport in ("auto", "urllib"):
        try:
            request = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if transport == "urllib":
                raise NotifierError(f"Request failed for {url}: {exc}") from exc

    command = ["curl", "-L", "-sS", "-X", method]
    for key, value in headers.items():
        command.extend(["-H", f"{key}: {value}"])
    command.append(url)
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=45)
    if completed.returncode != 0:
        raise NotifierError(completed.stderr.strip() or f"curl exited {completed.returncode}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise NotifierError(f"Could not parse JSON from {url}: {completed.stdout[:300]}") from exc


def get_token(transport: str) -> str:
    query = urllib.parse.urlencode({"secretKey": API_KEY})
    payload = request_json(f"{AUTH_URL}?{query}", method="POST", transport=transport)
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token:
        raise NotifierError(f"Login did not return an access token: {payload}")
    return token


def fetch_product(token: str, transport: str) -> dict[str, Any]:
    url = f"{CATALOG_URL}/salesGroups/{SALES_GROUP_ID}/product/{PRODUCT_ID}/views/loyalty"
    product = request_json(url, token=token, transport=transport)
    if not isinstance(product, dict) or product.get("productId") != int(PRODUCT_ID):
        raise NotifierError(f"Unexpected product response: {product}")
    return product


def fetch_availability_month(
    token: str,
    month: dt.date,
    *,
    venue_id: int,
    min_tickets: int,
    transport: str,
) -> dict[str, str]:
    params = urllib.parse.urlencode(
        {
            "month": month.month,
            "year": month.year,
            "venueId": venue_id,
            "minTickets": min_tickets,
        }
    )
    url = f"{CATALOG_URL}/salesGroups/{SALES_GROUP_ID}/product/{PRODUCT_ID}/availability?{params}"
    payload = request_json(url, token=token, transport=transport)
    if not isinstance(payload, dict):
        raise NotifierError(f"Unexpected availability response for {month:%Y-%m}: {payload}")
    return {str(date): str(status) for date, status in payload.items()}


def post_pushover(
    title: str,
    message: str,
    pushover: dict[str, Any],
    *,
    transport: str,
) -> None:
    app_token = pushover.get("app_token") or os.environ.get("PUSHOVER_APP_TOKEN")
    user_key = pushover.get("user_key") or os.environ.get("PUSHOVER_USER_KEY")
    if not app_token or not user_key:
        return

    data = {
        "token": app_token,
        "user": user_key,
        "title": title,
        "message": message,
        "url": TICKET_URL,
        "url_title": "Open Sagrada Familia tickets",
    }
    for optional_key in ("priority", "sound", "device"):
        if pushover.get(optional_key) is not None:
            data[optional_key] = str(pushover[optional_key])

    encoded = urllib.parse.urlencode(data).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "sagrada-ticket-notifier/1.0",
    }
    url = "https://api.pushover.net/1/messages.json"

    if transport in ("auto", "urllib"):
        try:
            request = urllib.request.Request(url, data=encoded, method="POST", headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("status") != 1:
                    print(f"Pushover warning: {payload}", file=sys.stderr)
                return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if transport == "urllib":
                print(f"Pushover failed: {exc}", file=sys.stderr)
                return

    command = ["curl", "-L", "-sS", "-X", "POST"]
    for key, value in headers.items():
        command.extend(["-H", f"{key}: {value}"])
    command.extend(["--data", encoded.decode("utf-8"), url])
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=45)
    if completed.returncode != 0:
        print(f"Pushover failed: {completed.stderr.strip()}", file=sys.stderr)


def notify(
    title: str,
    message: str,
    *,
    no_desktop: bool,
    pushover: dict[str, Any],
    transport: str,
) -> None:
    print(f"\n{title}\n{message}\a", flush=True)
    post_pushover(title, message, pushover, transport=transport)
    if no_desktop or sys.platform != "darwin":
        return
    script = (
        'display notification '
        f'{json.dumps(message)} '
        'with title '
        f'{json.dumps(title)} '
        'sound name "Glass"'
    )
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True, text=True)


def choose_end_date(product: dict[str, Any], configured: str | None) -> dt.date:
    if configured:
        return parse_date(configured)
    calendar_end = str(product.get("calendarEnd", "")).split(" ")[0]
    if calendar_end:
        return parse_date(calendar_end)
    today = dt.date.today()
    return today + dt.timedelta(days=90)


def status_label(status: str | None) -> str:
    if status == "availability":
        return "available"
    if status == "no-availability":
        return "sold out"
    return status or "missing"


def selected_watch_dates(args: argparse.Namespace, config: dict[str, Any]) -> set[str]:
    dates: list[str] = []
    dates.extend(config.get("watch_dates", []))
    for item in args.dates or []:
        dates.extend(part.strip() for part in item.split(",") if part.strip())
    dates.extend(date.isoformat() for date in args.date or [])
    return {parse_date(date).isoformat() for date in dates}


def check_once(args: argparse.Namespace, config: dict[str, Any]) -> bool:
    token = get_token(args.transport)
    product = fetch_product(token, args.transport)
    venue_id = int(config.get("venue_id") or product["productVenueSet"][0]["venueId"])
    watched = selected_watch_dates(args, config)
    watched_dates = sorted(parse_date(date) for date in watched)
    start_date = args.start_date or (
        watched_dates[0] if watched_dates else parse_date(config.get("start_date") or dt.date.today().isoformat())
    )
    end_date = args.end_date or (
        watched_dates[-1] if watched_dates else choose_end_date(product, config.get("end_date"))
    )
    min_tickets = int(args.min_tickets or config.get("min_tickets") or 1)

    if end_date < start_date:
        raise NotifierError(f"End date {end_date} is before start date {start_date}")

    current: dict[str, str] = {}
    for month in month_starts(start_date, end_date):
        current.update(
            fetch_availability_month(
                token,
                month,
                venue_id=venue_id,
                min_tickets=min_tickets,
                transport=args.transport,
            )
        )

    current = {
        date: status
        for date, status in current.items()
        if start_date <= dt.date.fromisoformat(date) <= end_date and (not watched or date in watched)
    }

    state = load_json(args.state, {})
    previous = state.get("statuses", {})
    first_run = not previous

    reopened = sorted(
        date
        for date, status in current.items()
        if status == "availability" and previous.get(date) == "no-availability"
    )
    newly_sold_out = sorted(
        date
        for date, status in current.items()
        if status == "no-availability" and previous.get(date) == "availability"
    )

    save_json(
        args.state,
        {
            "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "min_tickets": min_tickets,
            "statuses": current,
        },
    )

    available_count = sum(1 for status in current.values() if status == "availability")
    sold_out_count = sum(1 for status in current.values() if status == "no-availability")
    print(
        f"Checked {len(current)} dates ({available_count} available, {sold_out_count} sold out) "
        f"from {start_date} to {end_date}."
    )

    if first_run:
        sold_out = sorted(date for date, status in current.items() if status == "no-availability")
        print(f"Initial baseline saved. Sold-out dates being watched: {', '.join(sold_out) or 'none'}.")
        if args.notify_current:
            available = sorted(date for date, status in current.items() if status == "availability")
            if available:
                notify(
                    "Sagrada Familia tickets available",
                    ", ".join(available),
                    no_desktop=args.no_desktop,
                    pushover=config.get("pushover", {}),
                    transport=args.transport,
                )
                return True
        return False

    if reopened:
        lines = [f"{date} is now {status_label(current.get(date))}" for date in reopened]
        notify(
            "Sagrada Familia tickets reopened",
            "\n".join(lines),
            no_desktop=args.no_desktop,
            pushover=config.get("pushover", {}),
            transport=args.transport,
        )
        return True

    if newly_sold_out:
        print("Newly sold out:", ", ".join(newly_sold_out))
    print("No watched sold-out dates reopened.")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Notify when sold-out Sagrada Familia dates reopen.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="JSON config file")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="State file")
    parser.add_argument("--once", action="store_true", help="Check once and exit")
    parser.add_argument("--interval", type=int, default=None, help="Seconds between checks")
    parser.add_argument("--from", dest="start_date", type=parse_date, help="Start date, YYYY-MM-DD")
    parser.add_argument("--to", dest="end_date", type=parse_date, help="End date, YYYY-MM-DD")
    parser.add_argument("--date", action="append", type=parse_date, help="Watch one date. Can be repeated.")
    parser.add_argument("--dates", action="append", help="Comma-separated dates to watch, YYYY-MM-DD")
    parser.add_argument("--min-tickets", type=int, default=None, help="Minimum tickets required")
    parser.add_argument("--notify-current", action="store_true", help="Notify available dates on first run")
    parser.add_argument("--no-desktop", action="store_true", help="Skip macOS desktop notification")
    parser.add_argument(
        "--transport",
        choices=("auto", "urllib", "curl"),
        default="auto",
        help="HTTP transport. auto tries Python first, then curl.",
    )
    args = parser.parse_args()

    config = load_json(args.config, {}) if args.config.exists() else {}
    interval = int(args.interval or config.get("interval_seconds") or 300)

    while True:
        try:
            check_once(args, config)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
        if args.once:
            break
        print(f"Sleeping {interval} seconds. Press Ctrl-C to stop.")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
