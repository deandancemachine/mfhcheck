import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time
from email.message import EmailMessage
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

TIME_WINDOWS = {
    "breakfast": (time(6, 0), time(10, 59)),
    "lunch": (time(11, 0), time(15, 59)),
    "dinner": (time(16, 0), time(22, 59)),
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.6422.140 Safari/537.36"
)


@dataclass
class Slot:
    date: date
    time: time
    description: str


def parse_date(value: str) -> Optional[date]:
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"]:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_time(value: str) -> Optional[time]:
    for fmt in ["%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S"]:
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


def date_range(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is not None:
        return value.strip()
    return default


def get_target_config() -> Tuple[str, date, date, List[str], int]:
    restaurant_url = get_env("SEVENROOMS_RESTAURANT_URL", "https://mamasfishhouse.sevenrooms.com")
    start_date = parse_date(get_env("START_DATE", "2026-08-27"))
    end_date = parse_date(get_env("END_DATE", "2026-08-30"))
    if not start_date or not end_date:
        raise ValueError("START_DATE and END_DATE must be set in YYYY-MM-DD format.")
    windows = get_env("TIME_WINDOWS", "dinner")
    time_windows = [window.strip().lower() for window in windows.split(",") if window.strip()]
    if not time_windows:
        time_windows = ["dinner"]
    party_size = int(get_env("PARTY_SIZE", "2"))
    return restaurant_url, start_date, end_date, time_windows, party_size


def fetch_html(url: str) -> str:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return response.text


def extract_json_strings(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    json_strings: List[str] = []

    for script in soup.find_all("script"):
        if script.string is None:
            continue
        text = script.string.strip()
        if text.startswith("{") or text.startswith("["):
            json_strings.append(text)

        match = re.search(r"window\.__INITIAL_STATE__\s*=\s*({.*?});\s*$", text, re.DOTALL)
        if match:
            json_strings.append(match.group(1))

        match = re.search(r"=\s*({\s*\".*?\s*\})\s*;", text, re.DOTALL)
        if match and len(match.group(1)) < 500000:
            candidate = match.group(1)
            if candidate.count("\"") > 4:
                json_strings.append(candidate)

    return json_strings


def parse_json_text(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def flatten_json(data: Any) -> Iterable[Any]:
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from flatten_json(value)
    elif isinstance(data, list):
        for item in data:
            yield from flatten_json(item)


def find_slot_sources(data: Any) -> List[Dict[str, Any]]:
    sources: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        if any(key in data for key in ("slots", "availability", "availabilities", "timeSlots", "reservationSlots", "time_slots")):
            sources.append(data)
        for value in data.values():
            sources.extend(find_slot_sources(value))
    elif isinstance(data, list):
        for item in data:
            sources.extend(find_slot_sources(item))
    return sources


def parse_slot_objects(source: Dict[str, Any]) -> List[Slot]:
    slots: List[Slot] = []
    for key in ("slots", "availabilities", "timeSlots", "reservationSlots", "time_slots"):
        if key not in source:
            continue
        items = source[key]
        if not isinstance(items, list):
            continue
        for item in items:
            slot = parse_slot_item(item)
            if slot:
                slots.append(slot)
    return slots


def parse_slot_item(item: Any) -> Optional[Slot]:
    if not isinstance(item, dict):
        return None
    date_value = None
    time_value = None
    description = ""

    for key in ("date", "bookingDate", "arrivalDate", "startDate", "day"):
        if key in item:
            if isinstance(item[key], str):
                date_value = parse_date(item[key])
            elif isinstance(item[key], int):
                try:
                    date_value = datetime.utcfromtimestamp(item[key]).date()
                except Exception:
                    pass

    for key in ("time", "startTime", "slot", "start"):
        if key in item and isinstance(item[key], str):
            time_value = parse_time(item[key])
        elif key in item and isinstance(item[key], int):
            try:
                time_value = datetime.utcfromtimestamp(item[key]).time()
            except Exception:
                pass

    if not date_value and isinstance(item.get("datetime"), str):
        try:
            parsed = datetime.fromisoformat(item["datetime"].rstrip("Z"))
            date_value = parsed.date()
            time_value = parsed.time()
        except ValueError:
            pass

    if not time_value and isinstance(item.get("datetime"), str):
        try:
            parsed = datetime.fromisoformat(item["datetime"].rstrip("Z"))
            time_value = parsed.time()
        except ValueError:
            pass

    if not date_value and not time_value:
        if isinstance(item.get("slot"), dict):
            inner = item["slot"]
            return parse_slot_item(inner)

    if date_value and time_value:
        description = item.get("label") or item.get("text") or item.get("displayTime") or item.get("name") or "available"
        return Slot(date=date_value, time=time_value, description=str(description))
    return None


def extract_slots_from_html(html: str) -> List[Slot]:
    slots: List[Slot] = []
    for json_text in extract_json_strings(html):
        data = parse_json_text(json_text)
        if data is None:
            continue
        for node in flatten_json(data):
            slot_sources = find_slot_sources(node)
            for source in slot_sources:
                slots.extend(parse_slot_objects(source))
    unique_slots: Dict[Tuple[date, time, str], Slot] = {}
    for slot in slots:
        unique_slots[(slot.date, slot.time, slot.description)] = slot
    return sorted(unique_slots.values(), key=lambda s: (s.date, s.time))


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


def main() -> int:
    restaurant_url, start_date, end_date, time_windows, party_size = get_target_config()
    print(f"Checking SevenRooms availability for {restaurant_url}")
    print(f"Dates: {start_date.isoformat()} -> {end_date.isoformat()}")
    print(f"Time windows: {', '.join(time_windows)}")
    print(f"Party size: {party_size}")

    try:
        html = fetch_html(restaurant_url)
    except Exception as exc:
        print(f"Failed to fetch restaurant page: {exc}")
        return 1

    slots = extract_slots_from_html(html)
    if not slots:
        print("No slots were parsed from the page HTML. Trying a conservative text search fallback.")
        if "no availability" in html.lower():
            print("Page contains a clear no-availability message.")
        else:
            print("No JSON availability payload was detected. The site may require a JavaScript-powered request or a different API path.")
        return 0

    filtered_slots = filter_slots(slots, time_windows, start_date, end_date)
    if not filtered_slots:
        print("No matching breakfast/lunch/dinner slots were found in the target date range.")
        return 0

    print("Found candidate reservation slots:")
    for slot in filtered_slots:
        print(f"- {slot.date.isoformat()} {slot.time.strftime('%I:%M %p')} — {slot.description}")

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
        print("No external notification method was configured. Set SLACK_WEBHOOK_URL or SMTP_* and EMAIL_* secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
