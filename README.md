# Sagrada Familia Ticket Notifier

Small local monitor for the Sagrada Familia ticket calendar. It watches dates that are currently sold out and sends a notification when any selected date becomes available.

## Quick Start

Run one check and create the baseline state:

```bash
python3 sagrada_notifier.py --once
```

Keep checking every five minutes:

```bash
python3 sagrada_notifier.py
```

The first run saves the current calendar into `sagrada_state.json`. After that, the script notifies only when a date changes from `no-availability` to `availability`.

## Select Dates

Copy the example config:

```bash
cp sagrada_config.example.json sagrada_config.json
```

Then edit `watch_dates` and, optionally, the number of tickets:

```json
{
  "interval_seconds": 300,
  "min_tickets": 2,
  "watch_dates": ["2026-04-26", "2026-04-27"]
}
```

You can also select dates without a config file:

```bash
python3 sagrada_notifier.py --date 2026-04-26 --date 2026-04-27
```

Leave `watch_dates` empty to watch all sold-out dates in the configured date range.

## Pushover

Create a Pushover application, then put its app token and your user key in `sagrada_config.json`:

```json
{
  "interval_seconds": 300,
  "min_tickets": 2,
  "watch_dates": ["2026-04-26", "2026-04-27"],
  "pushover": {
    "app_token": "your-app-token",
    "user_key": "your-user-key",
    "sound": "pushover"
  }
}
```

Or keep secrets out of the file:

```bash
export PUSHOVER_APP_TOKEN="your-app-token"
export PUSHOVER_USER_KEY="your-user-key"
python3 sagrada_notifier.py --date 2026-04-26
```

## GitHub Actions Cron

For hosted monitoring, edit [sagrada_config.actions.json](sagrada_config.actions.json) with the dates and ticket count you care about:

```json
{
  "interval_seconds": 300,
  "min_tickets": 2,
  "watch_dates": ["2026-05-15", "2026-05-10"],
  "pushover": {}
}
```

Add these repository secrets in GitHub:

```text
PUSHOVER_APP_TOKEN
PUSHOVER_USER_KEY
```

The workflow in `.github/workflows/sagrada-notifier.yml` runs every five minutes and stores `sagrada_state.actions.json` in the GitHub Actions cache so it only notifies when a watched date flips from sold out to available. You can also run it manually from the Actions tab.

## Useful Commands

Check a custom range:

```bash
python3 sagrada_notifier.py --once --from 2026-04-25 --to 2026-06-30
```

Notify about dates that are already available on the first run:

```bash
python3 sagrada_notifier.py --once --notify-current
```

Skip desktop notifications and only print/bell in the terminal:

```bash
python3 sagrada_notifier.py --no-desktop
```
