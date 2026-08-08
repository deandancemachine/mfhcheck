# sample call https://www.sevenrooms.com/api-yoa/availability/ng/widget/range?venue=mamasfishhouserestaurantinn&party_size=2&halo_size_interval=100&start_date=2026-11-01&num_days=1&channel=SEVENROOMS_WIDGET&exclude_pdr=true&intent=user_search
import json
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

TIME_WINDOWS = {
    "breakfast": (time(6, 0), time(10, 59)),
    "lunch": (time(11, 0), time(15, 59)),
    "dinner": (time(16, 0), time(22, 59)),
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.6422.140 Safari/537.36"
)

SEVENROOMS_API_BASE_URL = "https://www.sevenrooms.com/api-yoa/availability/ng/widget/range"
DEFAULT_VENUE = "mamasfishhouserestaurantinn"
DEFAULT_RESTAURANT_URL = "https://mamasfishhouse.sevenrooms.com"


@dataclass
class Slot:
    date: date
    time: time
    description: str


def parse_date(value: Optional[str]) -> Optional[date]:
    if not isinstance(value, str):
        return None
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"]:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_time(value: Optional[str]) -> Optional[time]:
    if not isinstance(value, str):
        return None
    for fmt in ["%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %I:%M:%S"]:
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    value = value.strip().lower().replace(".", "")
    if value in {"noon", "12pm"}:
        return time(12, 0)
    if value == "midnight":
        return time(0, 0)
    return None


def date_range(start_date: date, end_date: date) -> List[date]:
    days: List[date] = []
    current = start_date
    while current <= end_date:
        days.append(current)
        current += timedelta(days=1)
    return days


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is not None:
        return value.strip()
    return default


def get_target_config() -> Tuple[str, str, str, date, date, List[str], int]:
    api_base_url = get_env("SEVENROOMS_API_BASE_URL", SEVENROOMS_API_BASE_URL)
    venue = get_env("SEVENROOMS_VENUE", DEFAULT_VENUE)
    restaurant_url = get_env("SEVENROOMS_RESTAURANT_URL", DEFAULT_RESTAURANT_URL)
    start_date = parse_date(get_env("START_DATE", "2026-11-27"))
    end_date = parse_date(get_env("END_DATE", "2026-11-30"))
    if not start_date or not end_date:
        raise ValueError("START_DATE and END_DATE must be set in YYYY-MM-DD format.")
    if end_date < start_date:
        raise ValueError("END_DATE must not be before START_DATE.")
    windows = get_env("TIME_WINDOWS", "dinner")
    time_windows = [window.strip().lower() for window in windows.split(",") if window.strip()]
    if not time_windows:
        time_windows = ["dinner"]
    party_size = int(get_env("PARTY_SIZE", "2"))
    return api_base_url, venue, restaurant_url, start_date, end_date, time_windows, party_size


def fetch_availability_json(api_base_url: str, venue: str, start_date: date, num_days: int, party_size: int) -> Dict[str, Any]:
    params = {
        "venue": venue,
        "party_size": str(party_size),
        "halo_size_interval": "100",
        "start_date": start_date.isoformat(),
        "num_days": str(num_days),
        "channel": "SEVENROOMS_WIDGET",
        "exclude_pdr": "true",
        "intent": "user_search",
    }
    response = requests.get(api_base_url,  params=params, timeout=30)
    # headers={"User-Agent": USER_AGENT}
    response.raise_for_status()
    return response.json()


def parse_availability_response(response_json: Dict[str, Any]) -> List[Slot]:
    slots: List[Slot] = []
    availability = response_json.get("data", {}).get("availability", {})
    if not isinstance(availability, dict):
        return []

    for date_key, day_items in availability.items():
        day_date = parse_date(date_key)
        if day_date is None or not isinstance(day_items, list):
            continue
        for item in day_items:
            if not isinstance(item, dict):
                continue
            for slot_item in item.get("times", []):
                if not isinstance(slot_item, dict):
                    continue
                if slot_item.get("type") != "book":
                    continue
                time_string = (
                    slot_item.get("time")
                    or slot_item.get("time_iso")
                    or slot_item.get("real_datetime_of_slot")
                    or slot_item.get("utc_datetime")
                )
                time_value = parse_time(time_string)
                if time_value is None:
                    continue
                description_value = (
                    slot_item.get("time")
                    or slot_item.get("time_iso")
                    or slot_item.get("real_datetime_of_slot")
                    or slot_item.get("utc_datetime")
                    or "available"
                )
                description = str(description_value)
                slots.append(Slot(date=day_date, time=time_value, description=description))

    return sorted(slots, key=lambda s: (s.date, s.time, s.description))


def filter_slots(slots: List[Slot], windows: List[str], start_date: date, end_date: date) -> List[Slot]:
    target_windows = [window for window in windows if window in TIME_WINDOWS]
    filtered: List[Slot] = []
    for slot in slots:
        if slot.date < start_date or slot.date > end_date:
            continue
        for window in target_windows:
            start, end = TIME_WINDOWS[window]
            if start <= slot.time <= end:
                filtered.append(slot)
                break
    return filtered


def send_slack_message(webhook_url: str, message: str) -> None:
    payload = {"text": message}
    response = requests.post(webhook_url, json=payload, headers={"User-Agent": USER_AGENT}, timeout=15)
    response.raise_for_status()


def send_discord_message(webhook_url: str, message: str) -> None:
    payload = {"content": message}
    response = requests.post(webhook_url, json=payload, headers={"User-Agent": USER_AGENT}, timeout=15)
    response.raise_for_status()


def send_email(smtp_host: str, smtp_port: int, smtp_user: str, smtp_password: str, sender: str, recipient: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(message)


def build_notification_message(slots: List[Slot], restaurant_url: str) -> str:
    lines = [
        f"SevenRooms reservation availability found for {restaurant_url}",
        "",
    ]
    for slot in slots:
        lines.append(f"- {slot.date.isoformat()} {slot.time.strftime('%I:%M %p')} — {slot.description}")
    lines.append("")
    lines.append("Visit the restaurant page to book:")
    lines.append(restaurant_url)
    return "\n".join(lines)


def save_search_results(slots: List[Slot], result_dir: Path) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    result_path = result_dir / f"search_result_{timestamp}.json"
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "slot_count": len(slots),
        "slots": [
            {
                "date": slot.date.isoformat(),
                "time": slot.time.strftime("%H:%M"),
                "description": slot.description,
            }
            for slot in slots
        ],
    }
    with result_path.open("w", encoding="utf-8") as result_file:
        json.dump(payload, result_file, indent=2)
    return result_path


def save_raw_api_response(response_json: Dict[str, Any], raw_dir: Path, current_date: date) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    filename = f"raw_response_{current_date.isoformat()}.json"
    result_path = raw_dir / filename
    with result_path.open("w", encoding="utf-8") as result_file:
        json.dump(response_json, result_file, indent=2)
    return result_path


def main() -> int:
    api_base_url, venue, restaurant_url, start_date, end_date, time_windows, party_size = get_target_config()
    today = date.today()
    if today > end_date:
        print(f"Current date {today.isoformat()} is after END_DATE {end_date.isoformat()}. Exiting.")
        return 0

    print(f"Checking SevenRooms availability for {restaurant_url}")
    print(f"API base URL: {api_base_url}")
    print(f"Venue: {venue}")
    print(f"Dates: {start_date.isoformat()} -> {end_date.isoformat()}")
    print(f"Time windows: {', '.join(time_windows)}")
    print(f"Party size: {party_size}")

    slots: List[Slot] = []
    raw_dir = Path(get_env("RAW_RESULT_DIR", "search_result/raw"))
    for current_date in date_range(start_date, end_date):
        print(f"Requesting availability for {current_date.isoformat()}")
        try:
            response_json = fetch_availability_json(api_base_url, venue, current_date, 1, party_size)
            saved_raw_path = save_raw_api_response(response_json, raw_dir, current_date)
            print(f"Saved raw API response to {saved_raw_path}")
        except Exception as exc:
            print(f"Failed to fetch availability from SevenRooms API for {current_date.isoformat()}: {exc}")
            continue

        day_slots = parse_availability_response(response_json)
        if not day_slots:
            print(f"No availability slots were found for {current_date.isoformat()}.")
            status = response_json.get("status")
            if status is not None:
                print(f"API status: {status}")
            continue
        slots.extend(day_slots)

    if not slots:
        print("No availability slots were found in the requested date range.")
        return 0

    filtered_slots = filter_slots(slots, time_windows, start_date, end_date)
    if not filtered_slots:
        print("No matching breakfast/lunch/dinner slots were found in the target date range.")
        return 0

    print("Found candidate reservation slots:")
    for slot in filtered_slots:
        print(f"- {slot.date.isoformat()} {slot.time.strftime('%I:%M %p')} — {slot.description}")

    result_dir = Path(get_env("SEARCH_RESULT_DIR", "search_result"))
    result_path = save_search_results(filtered_slots, result_dir)
    print(f"Saved search results to {result_path}")

    notification_message = build_notification_message(filtered_slots, restaurant_url)

    sent_notifications = 0
    slack_url = get_env("SLACK_WEBHOOK_URL")
    if slack_url:
        try:
            send_slack_message(slack_url, notification_message)
            print("Slack notification sent.")
            sent_notifications += 1
        except Exception as exc:
            print(f"Failed to send Slack notification: {exc}")

    discord_url = get_env("DISCORD_WEBHOOK_URL")
    if discord_url:
        try:
            send_discord_message(discord_url, notification_message)
            print("Discord notification sent.")
            sent_notifications += 1
        except Exception as exc:
            print(f"Failed to send Discord notification: {exc}")

    smtp_host = get_env("SMTP_HOST")
    smtp_port = int(get_env("SMTP_PORT", "465"))
    smtp_user = get_env("SMTP_USER")
    smtp_password = get_env("SMTP_PASSWORD")
    email_to = get_env("EMAIL_TO")
    email_from = get_env("EMAIL_FROM")
    if smtp_host and smtp_user and smtp_password and email_to and email_from:
        try:
            send_email(
                smtp_host,
                smtp_port,
                smtp_user,
                smtp_password,
                email_from,
                email_to,
                f"SevenRooms availability for {restaurant_url}",
                notification_message,
            )
            print("Email notification sent.")
            sent_notifications += 1
        except Exception as exc:
            print(f"Failed to send email notification: {exc}")

    if sent_notifications == 0:
        print("No external notification method was configured. Set SLACK_WEBHOOK_URL, DISCORD_WEBHOOK_URL, or SMTP_* and EMAIL_* secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
