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
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any


AUTH_URL = "https://services.clorian.com/user/api/oauth/token"
CATALOG_URL = "https://services.clorian.com/catalog"
TICKET_URL = "https://tickets.sagradafamilia.org/en/1-individual/4375-sagrada-familia"
ORIGIN = "https://tickets.sagradafamilia.org"

POS_ID = "649"
SALES_GROUP_ID = "1"
API_KEY = "thesagradafamiliafrontendoftomorrow"
TIME_ZONE = "Europe/Madrid"
DETECTION_TIME_ZONE = "America/New_York"

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
    referer: str = TICKET_URL,
) -> Any:
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en",
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "Referer": referer,
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


def fetch_product(token: str, product_id: str, ticket_url: str, transport: str) -> dict[str, Any]:
    url = f"{CATALOG_URL}/salesGroups/{SALES_GROUP_ID}/product/{product_id}/views/loyalty"
    product = request_json(url, token=token, transport=transport, referer=ticket_url)
    if not isinstance(product, dict) or product.get("productId") != int(product_id):
        raise NotifierError(f"Unexpected product response: {product}")
    return product


def fetch_availability_month(
    token: str,
    month: dt.date,
    *,
    venue_id: int,
    min_tickets: int,
    product_id: str,
    ticket_url: str,
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
    url = f"{CATALOG_URL}/salesGroups/{SALES_GROUP_ID}/product/{product_id}/availability?{params}"
    payload = request_json(url, token=token, transport=transport, referer=ticket_url)
    if not isinstance(payload, dict):
        raise NotifierError(f"Unexpected availability response for {month:%Y-%m}: {payload}")
    return {str(date): str(status) for date, status in payload.items()}


def fetch_event_times(
    token: str,
    date: str,
    *,
    product_id: str,
    venue_id: int,
    ticket_url: str,
    transport: str,
) -> list[str]:
    params = urllib.parse.urlencode(
        {
            "salesGroupId": SALES_GROUP_ID,
            "productId": product_id,
            "venueId": venue_id,
            "startDateFrom": f"{date} 00:00",
            "startDateTo": f"{date} 23:59",
            "timeZone": TIME_ZONE,
        }
    )
    url = f"{CATALOG_URL}/events/available?{params}"
    try:
        payload = request_json(url, token=token, transport=transport, referer=ticket_url)
    except NotifierError as exc:
        print(f"Could not fetch event times for {product_id} on {date}: {exc}", file=sys.stderr)
        return []
    if not isinstance(payload, list):
        message = payload.get("message") if isinstance(payload, dict) else payload
        print(f"Event times unavailable for {product_id} on {date}: {message}", file=sys.stderr)
        return []

    times = []
    for event in payload:
        start = event.get("startDatetime") if isinstance(event, dict) else None
        if not start:
            continue
        try:
            parsed = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo(TIME_ZONE))
            times.append(parsed.strftime("%H:%M"))
        except ValueError:
            if len(str(start)) >= 16:
                times.append(str(start)[11:16])
    return sorted(set(times))


def post_pushover(
    title: str,
    message: str,
    pushover: dict[str, Any],
    *,
    transport: str,
    ticket_url: str,
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
        "url": ticket_url,
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
    ticket_url: str,
) -> None:
    print(f"\n{title}\n{message}\a", flush=True)
    post_pushover(title, message, pushover, transport=transport, ticket_url=ticket_url)
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


def get_products(config: dict[str, Any]) -> list[dict[str, Any]]:
    products = config.get("products")
    if products:
        return products
    return [
        {
            "key": "standard",
            "label": "Sagrada Familia",
            "product_id": str(config.get("product_id") or "4375"),
            "ticket_url": config.get("ticket_url") or TICKET_URL,
            "venue_id": config.get("venue_id"),
            "watch_dates": config.get("watch_dates", []),
            "message_prefix": config.get("message_prefix") or "New Sagrada tickets available",
        }
    ]


def format_detection_time(checked_at: dt.datetime) -> str:
    return checked_at.astimezone(ZoneInfo(DETECTION_TIME_ZONE)).strftime("%Y-%m-%d %H:%M %Z")


def build_reopened_message(
    prefix: str,
    date: str,
    *,
    detected_at: str,
    event_times: list[str],
) -> str:
    line = f"{prefix} on {date} at {detected_at}"
    if event_times:
        line += f"\nTicket times: {', '.join(event_times)} {TIME_ZONE}"
    return line


def check_product(
    args: argparse.Namespace,
    config: dict[str, Any],
    product_config: dict[str, Any],
    token: str,
    checked_at: dt.datetime,
    state: dict[str, Any],
) -> tuple[str, dict[str, Any], bool]:
    product_id = str(product_config["product_id"])
    key = str(product_config.get("key") or product_id)
    ticket_url = product_config.get("ticket_url") or TICKET_URL
    product = fetch_product(token, product_id, ticket_url, args.transport)
    venue_id = int(product_config.get("venue_id") or product["productVenueSet"][0]["venueId"])
    watched = selected_watch_dates(args, {**config, **product_config})
    watched_dates = sorted(parse_date(date) for date in watched)
    start_date = args.start_date or (
        watched_dates[0]
        if watched_dates
        else parse_date(product_config.get("start_date") or config.get("start_date") or dt.date.today().isoformat())
    )
    end_date = args.end_date or (
        watched_dates[-1]
        if watched_dates
        else choose_end_date(product, product_config.get("end_date") or config.get("end_date"))
    )
    min_tickets = int(args.min_tickets or product_config.get("min_tickets") or config.get("min_tickets") or 1)

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
                product_id=product_id,
                ticket_url=ticket_url,
                transport=args.transport,
            )
        )

    current = {
        date: status
        for date, status in current.items()
        if start_date <= dt.date.fromisoformat(date) <= end_date and (not watched or date in watched)
    }

    previous = state.get("products", {}).get(key, {}).get("statuses")
    if previous is None and key == "standard":
        previous = state.get("statuses", {})
    previous = previous or {}
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

    available_count = sum(1 for status in current.values() if status == "availability")
    sold_out_count = sum(1 for status in current.values() if status == "no-availability")
    print(
        f"Checked {product_config.get('label') or key}: "
        f"{len(current)} dates ({available_count} available, {sold_out_count} sold out) "
        f"from {start_date} to {end_date}."
    )
    product_state = {
        "checked_at_utc": checked_at.isoformat().replace("+00:00", "Z"),
        "product_id": product_id,
        "venue_id": venue_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "min_tickets": min_tickets,
        "statuses": current,
    }

    if first_run:
        sold_out = sorted(date for date, status in current.items() if status == "no-availability")
        print(f"Initial baseline saved for {key}. Sold-out dates being watched: {', '.join(sold_out) or 'none'}.")
        if args.notify_current:
            available = sorted(date for date, status in current.items() if status == "availability")
            if available:
                detected_at = format_detection_time(checked_at)
                message = "\n\n".join(
                    build_reopened_message(
                        product_config.get("message_prefix") or "New Sagrada tickets available",
                        date,
                        detected_at=detected_at,
                        event_times=fetch_event_times(
                            token,
                            date,
                            product_id=product_id,
                            venue_id=venue_id,
                            ticket_url=ticket_url,
                            transport=args.transport,
                        ),
                    )
                    for date in available
                )
                notify(
                    product_config.get("notification_title") or "Sagrada tickets available",
                    message,
                    no_desktop=args.no_desktop,
                    pushover=config.get("pushover", {}),
                    transport=args.transport,
                    ticket_url=ticket_url,
                )
                return key, product_state, True
        return key, product_state, False

    if reopened:
        detected_at = format_detection_time(checked_at)
        lines = [
            build_reopened_message(
                product_config.get("message_prefix") or "New Sagrada tickets available",
                date,
                detected_at=detected_at,
                event_times=fetch_event_times(
                    token,
                    date,
                    product_id=product_id,
                    venue_id=venue_id,
                    ticket_url=ticket_url,
                    transport=args.transport,
                ),
            )
            for date in reopened
        ]
        notify(
            product_config.get("notification_title") or "Sagrada tickets available",
            "\n".join(lines),
            no_desktop=args.no_desktop,
            pushover=config.get("pushover", {}),
            transport=args.transport,
            ticket_url=ticket_url,
        )
        return key, product_state, True

    if newly_sold_out:
        print(f"Newly sold out for {key}:", ", ".join(newly_sold_out))
    print(f"No watched sold-out dates reopened for {key}.")
    return key, product_state, False


def check_once(args: argparse.Namespace, config: dict[str, Any]) -> bool:
    checked_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    token = get_token(args.transport)
    state = load_json(args.state, {})
    next_state = {
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "checked_at_utc": checked_at.isoformat().replace("+00:00", "Z"),
        "products": dict(state.get("products", {})),
    }
    notified = False
    for product_config in get_products(config):
        key, product_state, product_notified = check_product(args, config, product_config, token, checked_at, state)
        next_state["products"][key] = product_state
        notified = notified or product_notified
    save_json(args.state, next_state)
    return notified


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
