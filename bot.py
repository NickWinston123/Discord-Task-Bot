import os
import sys
import json
import asyncio
import subprocess
import platform
import hashlib
import httplib2

from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# ----------------- CONFIG -----------------

load_dotenv()
load_dotenv("secrets.env", override=True)

UPCOMING_SOON_MINUTES = int(os.getenv("UPCOMING_SOON_MINUTES", "20"))  # 🟡 if starts within N minutes
ACTIVE_URGENT_PERCENT = float(os.getenv("ACTIVE_URGENT_PERCENT", "0")) # 0 disables; set 0.15 for last 15%


WIFI_DEVICE_IP = os.getenv("WIFI_DEVICE_IP", "192.168.1.167:5555")
WIFI_IP_ONLY = WIFI_DEVICE_IP.split(":")[0]

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")
TASK_PREFIX = os.getenv("TASK_PREFIX", "TASK:")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

CONFIG_DIR = os.getenv("CONFIG_DIR", "/app/config")
LOG_FILE = os.path.join(CONFIG_DIR, "task_log.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token.json")
CRED_PATH = os.path.join(CONFIG_DIR, "credentials.json")

GOAL_PREFIX = os.getenv("GOAL_PREFIX", "GOAL:")
GOAL_LOOKAHEAD_DAYS = int(os.getenv("GOAL_LOOKAHEAD_DAYS", "30"))


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

NUM_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
STATUS_EMOJI = {
    "pending": "⚪",
    "completed": "✅",
    "skipped": "❌",
}
REFRESH_EMOJI = "🔄"  # refresh button

# In-memory reminder state (resets when bot restarts)
reminder_state = {
    "start_sent": set(),         # task_ids we've already reminded at start
    "end_sent": set(),           # task_ids we've reminded at end
    "nag_last": {},              # task_id -> datetime of last nag
}

# In-memory phone online state (per-day)
phone_online_state = {
    "today": None,
    "seen_online": False,
    "first_online_ts": None,  # <-- first successful ping timestamp (wake proxy)
    "last_notify": None,
}


refresh_lock = asyncio.Lock()

def _task_fingerprint(tasks) -> str:
    normalized = []
    for t in tasks:
        normalized.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "start": t.get("scheduled_start"),
            "end": t.get("scheduled_end"),
            "all_day": bool(t.get("all_day", False)),
        })
    normalized.sort(key=lambda x: (x["start"] or "", x["id"] or ""))
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ----------------- RUNTIME STATE (PERSISTENT) -----------------

RUNTIME_STATE_PATH = os.path.join(CONFIG_DIR, "runtime_state.json")

def _json_safe(obj):
    """
    Convert objects to JSON-safe equivalents (datetimes -> isoformat).
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return list(obj)
    return obj


def now_local() -> datetime:
    """Return current time in configured TIMEZONE (or naive now if missing)."""
    if TIMEZONE:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            pass
    return datetime.now()


class SimpleUser:
    """Minimal stand-in user object for auto-checkin."""
    def __init__(self, user_id: int):
        self.id = user_id
        self.name = f"AutoCheckin-{user_id}"




def save_runtime_state():
    """
    Persist minimal bot runtime state so a restart/reconnect doesn't lose the last board.
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)

        payload = {
            "saved_at": now_local().isoformat(),
            "last_checkin_message_id": getattr(bot, "last_checkin_message_id", None),
            "last_task_message": getattr(bot, "last_task_message", None),
        }

        # Optional: persist today's first_online_ts if you care
        ts = phone_online_state.get("first_online_ts")
        if isinstance(ts, datetime):
            payload["phone_first_online_ts"] = ts.isoformat()
            payload["phone_first_online_date"] = ts.date().isoformat()
        else:
            payload["phone_first_online_ts"] = None
            payload["phone_first_online_date"] = None

        with open(RUNTIME_STATE_PATH, "w") as f:
            json.dump(payload, f, indent=2, default=_json_safe)

    except Exception as e:
        print(f"⚠️ save_runtime_state failed: {e}")

def load_runtime_state():
    """
    Load persisted runtime state into bot.* attributes.
    """
    if not os.path.exists(RUNTIME_STATE_PATH):
        return

    try:
        with open(RUNTIME_STATE_PATH, "r") as f:
            payload = json.load(f)

        # Restore last_checkin_message_id
        lcmid = payload.get("last_checkin_message_id")
        if isinstance(lcmid, int):
            bot.last_checkin_message_id = lcmid
        else:
            bot.last_checkin_message_id = None

        # Restore last_task_message
        info = payload.get("last_task_message")
        if isinstance(info, dict):
            # keep only expected keys and types
            cleaned = {
                "message_id": info.get("message_id"),
                "channel_id": info.get("channel_id"),
                "tasks": info.get("tasks") or [],
                "user_id": info.get("user_id"),
                "statuses": info.get("statuses") or [],
                "fingerprint": info.get("fingerprint"),
                "for_date": info.get("for_date"),
                "render_hash": info.get("render_hash"),
            }
            bot.last_task_message = cleaned
        else:
            bot.last_task_message = None

        # Optional: restore today's phone_first_online_ts
        ts_str = payload.get("phone_first_online_ts")
        d_str = payload.get("phone_first_online_date")
        if ts_str and d_str == now_local().date().isoformat():
            try:
                phone_online_state["first_online_ts"] = datetime.fromisoformat(ts_str)
            except Exception:
                pass

        print("✅ Runtime state restored from disk.")

    except Exception as e:
        print(f"⚠️ load_runtime_state failed: {e}")

async def restore_last_task_board_if_needed():
    """
    After startup, try to re-associate with the last posted task board message.
    If Discord deleted it or we can't fetch, we just drop state and you can !checkin.
    """
    info = getattr(bot, "last_task_message", None)
    if not info:
        return

    try:
        channel_id = info.get("channel_id")
        message_id = info.get("message_id")
        if not channel_id or not message_id:
            return

        channel = bot.get_channel(int(channel_id))
        if not channel:
            try:
                channel = await bot.fetch_channel(int(channel_id))
            except Exception:
                return

        try:
            await channel.fetch_message(int(message_id))
        except Exception:
            # Message no longer exists or not accessible; drop state
            bot.last_task_message = None
            save_runtime_state()
            print("⚠️ Saved task board message not found; cleared runtime state.")
            return

        # If it's not today's date, don't treat it as live
        if info.get("for_date") != now_local().date().isoformat():
            # Keep it persisted (history), but it's not "live" for reactions/ticks
            print("ℹ️ Restored task board is not for today; will not treat as live.")
            return

        print("✅ Last task board message exists and is linked.")

    except Exception as e:
        print(f"⚠️ restore_last_task_board_if_needed failed: {e}")

def _content_hash(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


async def maybe_tick_task_message(channel):
    info = getattr(bot, "last_task_message", None)
    if not info or info.get("channel_id") != channel.id or refresh_lock.locked():
        return
    if info.get("for_date") != now_local().date().isoformat():
        return

    try:
        msg = await channel.fetch_message(info["message_id"])
        tasks = info.get("tasks", [])
        statuses = info.get("statuses", [])
        
        # Pull goals in executor to avoid blocking the loop
        loop = asyncio.get_running_loop()
        goals = await loop.run_in_executor(None, get_goals_lookahead, GOAL_LOOKAHEAD_DAYS)
        
        new_content = build_task_message(tasks, statuses, goals=goals)
        new_hash = _content_hash(new_content)

        if info.get("render_hash") != new_hash:
            await msg.edit(content=new_content)
            info["render_hash"] = new_hash
            save_runtime_state()
    except Exception as e:
        print(f"⚠️ Task tick edit failed: {e}")

# ----------------- PING HELPER -----------------


def ping_device(ip, timeout=1):
    if platform.system().lower() == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout * 1000), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout), ip]

    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


# ----------------- GOOGLE CALENDAR AUTH / ENV FLAGS -----------------

def _get_bool_env(name, default=True):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


REMIND_AT_START = _get_bool_env("REMIND_AT_START", True)
REMIND_AT_END = _get_bool_env("REMIND_AT_END", True)
KEEP_REMINDING = _get_bool_env("KEEP_REMINDING", True)
RED_WARNINGS = _get_bool_env("RED_WARNINGS", True)
END_OF_DAY_SUMMARY = _get_bool_env("END_OF_DAY_SUMMARY", True)

REMIND_INTERVAL_MINUTES = int(os.getenv("REMIND_INTERVAL_MINUTES", "30"))

END_OF_DAY_HOUR = int(os.getenv("END_OF_DAY_HOUR", "22"))      # default 10 PM
START_OF_DAY_HOUR = int(os.getenv("START_OF_DAY_HOUR", "4"))   # default 4 AM

PRIMARY_USER_ID = os.getenv("PRIMARY_USER_ID")  # string of your Discord ID
if PRIMARY_USER_ID is not None:
    try:
        PRIMARY_USER_ID = int(PRIMARY_USER_ID)
    except ValueError:
        PRIMARY_USER_ID = None

# Mention target for pinging you in messages
MENTION_TARGET = f"<@{PRIMARY_USER_ID}>" if PRIMARY_USER_ID else ""

# Phone ping config
PHONE_PING_ENABLED = _get_bool_env("PHONE_PING_ENABLED", False) and ping_device is not None and WIFI_IP_ONLY is not None
PHONE_PING_INTERVAL_MINUTES = max(1, int(os.getenv("PHONE_PING_INTERVAL_MINUTES", "5")))


def get_gcal_service():
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(
            f"token.json not found at {TOKEN_PATH}. "
            "Run the init_gcal.py helper on your local machine, then copy token.json into /app/config."
        )

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as token:
                token.write(creds.to_json())
        else:
            raise RuntimeError("Google credentials invalid and no refresh token available.")

    # ✅ google-auth way (no creds.authorize)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return service


def log_drift_summary(for_date: date, avg_minutes: float, samples: int):
    data = load_log()
    data.setdefault("drift", [])
    data["drift"].append(
        {
            "date": for_date.isoformat(),
            "avg_minutes_late_vs_end": round(avg_minutes, 2),
            "samples": samples,
            "timestamp": now_local().isoformat(),
        }
    )
    save_log(data)



# ----------------- TASK FETCHING -----------------

def get_tasks_for_date(target_date: date):
    service = get_gcal_service()
    tz = ZoneInfo(TIMEZONE) if TIMEZONE else timezone.utc
    
    start_day_local = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz)
    end_day_local = start_day_local + timedelta(days=1)

    time_min = start_day_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    time_max = end_day_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    events_result = service.events().list(
        calendarId=CALENDAR_ID, timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime"
    ).execute()

    events = events_result.get("items", [])
    tasks = []
    for ev in events:
        title = ev.get("summary", "")
        if not title.upper().startswith(TASK_PREFIX.upper()): continue

        start_info = ev.get("start", {})
        end_info = ev.get("end", {})
        
        if "dateTime" in start_info:
            all_day = False
            s_dt = _to_local(datetime.fromisoformat(start_info["dateTime"].replace('Z', '+00:00')))
            e_dt = _to_local(datetime.fromisoformat(end_info["dateTime"].replace('Z', '+00:00')))
            
            if (e_dt - s_dt).total_seconds() > 86400:
                s_dt = datetime.combine(target_date, s_dt.time()).replace(tzinfo=tz)
                e_dt = datetime.combine(target_date, e_dt.time()).replace(tzinfo=tz)
                if e_dt <= s_dt:
                    e_dt += timedelta(days=1)
            
            sched_start = s_dt.isoformat()
            sched_end = e_dt.isoformat()
        else:
            all_day = True
            sched_start = start_info.get("date")
            sched_end = end_info.get("date")

        tasks.append({
            "id": ev["id"],
            "title": title[len(TASK_PREFIX):].strip(),
            "scheduled_start": sched_start,
            "scheduled_end": sched_end,
            "all_day": all_day
        })

    # Explicitly sort tasks chronologically by start time
    def sort_key(t):
        # Timed tasks use their actual start time
        if not t["all_day"]:
            return _parse_dt(t["scheduled_start"])
        # All-day tasks sort to the beginning of the day (match check-in logic)
        return datetime.combine(target_date, datetime.min.time()).replace(tzinfo=tz)

    tasks.sort(key=sort_key)
    return tasks
def get_todays_tasks():
    """Backward-compatible wrapper."""
    return get_tasks_for_date(now_local().date())

# ----------------- LOGGING -----------------

def load_log():
    if not os.path.exists(LOG_FILE):
        return {"checkins": [], "tasks": [], "phone_pings": []}
    with open(LOG_FILE, "r") as f:
        return json.load(f)


def save_log(data):
    with open(LOG_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def get_today_first_checkin_time(user_id: int | None) -> datetime | None:
    """Earliest check-in timestamp for today (local TZ)."""
    if user_id is None:
        return None
    data = load_log()
    today_str = now_local().date().isoformat()
    first = None
    for entry in data.get("checkins", []):
        if entry.get("user_id") != user_id:
            continue
        if entry.get("date") != today_str:
            continue
        ts_str = entry.get("timestamp")
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        dt = _to_local(dt)
        if dt is None:
            continue
        if first is None or dt < first:
            first = dt
    return first


def log_phone_first_online(user_id: int | None, ts: datetime | None = None):
    """Persist the first successful phone ping time for today."""
    if user_id is None:
        return
    if ts is None:
        ts = now_local()

    data = load_log()
    today_str = ts.date().isoformat()
    data.setdefault("phone_pings", [])

    # if already logged for today, do nothing
    for entry in data["phone_pings"]:
        if entry.get("user_id") == user_id and entry.get("date") == today_str:
            return

    data["phone_pings"].append(
        {
            "user_id": user_id,
            "timestamp": ts.isoformat(),
            "date": today_str,
        }
    )
    save_log(data)


def get_today_first_phone_online_time(user_id: int | None) -> datetime | None:
    """Read the persisted first successful phone ping time for today."""
    if user_id is None:
        return None
    data = load_log()
    today_str = now_local().date().isoformat()
    for entry in data.get("phone_pings", []):
        if entry.get("user_id") == user_id and entry.get("date") == today_str:
            ts_str = entry.get("timestamp")
            if not ts_str:
                return None
            try:
                dt = datetime.fromisoformat(ts_str)
            except Exception:
                return None
            return _to_local(dt)
    return None


def log_checkin(user_id, ts: datetime | None = None):
    if ts is None:
        ts = now_local()
    data = load_log()
    data["checkins"].append(
        {
            "user_id": user_id,
            "timestamp": ts.isoformat(),
            "date": ts.date().isoformat(),
        }
    )
    save_log(data)


def log_task_completion(user_id, task_id, status, ts: datetime | None = None):
    if ts is None:
        ts = now_local()
    data = load_log()
    data["tasks"].append(
        {
            "user_id": user_id,
            "task_id": task_id,
            "status": status,  # "completed" / "skipped"
            "timestamp": ts.isoformat(),
            "date": ts.date().isoformat(),
        }
    )
    save_log(data)


def get_today_status_map(user_id: int | None):
    """Return latest status per task_id for this user (or all) for *today* in local TIMEZONE."""
    data = load_log()
    today_str = now_local().date().isoformat()
    status_map = {}
    for entry in data.get("tasks", []):
        if entry.get("date") != today_str:
            continue
        if user_id is not None and entry.get("user_id") != user_id:
            continue
        # last one wins
        status_map[entry["task_id"]] = entry["status"]
    return status_map


# ----------------- MESSAGE BUILDING -----------------

def _to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    try:
        return dt.astimezone(ZoneInfo(TIMEZONE)) if TIMEZONE else dt
    except Exception:
        return dt

def _pending_time_symbol(now: datetime, start_local: datetime | None, end_local: datetime | None) -> str:
    """
    Time-state color for *pending* tasks only:
      🟡 upcoming soon (starts within UPCOMING_SOON_MINUTES)
      🟠 active window
      🔴 active but in last ACTIVE_URGENT_PERCENT of window (if enabled)
      ⚪ default
    """
    if start_local is None or end_local is None:
        return "⚪"

    if now < start_local:
        mins = (start_local - now).total_seconds() / 60.0
        if mins <= UPCOMING_SOON_MINUTES:
            return "🟡"
        return "⚪"

    if start_local <= now <= end_local:
        # active
        if ACTIVE_URGENT_PERCENT and ACTIVE_URGENT_PERCENT > 0:
            total = (end_local - start_local).total_seconds()
            if total > 0:
                progress = (now - start_local).total_seconds() / total
                if progress >= (1.0 - ACTIVE_URGENT_PERCENT):
                    return "🔴"
        return "🟠"

    # past end -> leave overdue handling to build_task_message (it appends 🔴 OVERDUE)
    return "⚪"

def _clear_reminder_state_for_task(task_id: str):
    reminder_state["start_sent"].discard(task_id)
    reminder_state["end_sent"].discard(task_id)
    reminder_state["nag_last"].pop(task_id, None)


def _parse_dt(dt_str: str):
    """Parse an RFC3339 / ISO string into a datetime, or None."""
    if not dt_str:
        return None
    try:
        # Google may use "Z" for UTC; Python wants "+00:00"
        if dt_str.endswith("Z"):
            dt_str = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def _format_time_local(dt: datetime):
    """Convert to TIMEZONE (if set) and return '7:00 AM' style string."""
    if dt is None:
        return None
    try:
        if TIMEZONE:
            dt = dt.astimezone(ZoneInfo(TIMEZONE))
    except Exception:
        # If TIMEZONE is bogus, just leave it as is
        pass
    # 12-hour without leading zero
    return dt.strftime("%I:%M %p").lstrip("0")


def build_task_overlook_message(for_date: date, tasks):
    # Example: "Saturday, Dec 13"
    header = for_date.strftime("%A, %b %d").replace(" 0", " ")

    lines = [f"🗓️ **Task overlook — {header}:**"]

    if not tasks:
        lines.append("✅ No TASK: events scheduled.")
        return "\n".join(lines)

    for idx, task in enumerate(tasks[:len(NUM_EMOJIS)]):
        emoji = NUM_EMOJIS[idx]
        title = task["title"]

        if task.get("all_day", False):
            lines.append(f"{emoji} **{title}**")
            lines.append("    🕒 All day")
            continue

        start_dt = _parse_dt(task.get("scheduled_start"))
        end_dt = _parse_dt(task.get("scheduled_end"))
        start_str = _format_time_local(start_dt)
        end_str = _format_time_local(end_dt)

        if start_str and end_str:
            time_line = f"    🕒 {start_str} → {end_str}"
        elif start_str:
            time_line = f"    🕒 {start_str}"
        else:
            time_line = None

        lines.append(f"{emoji} **{title}**")
        if time_line:
            lines.append(time_line)

    if len(tasks) > len(NUM_EMOJIS):
        lines.append(f"…and {len(tasks) - len(NUM_EMOJIS)} more.")

    return "\n".join(lines)


def get_events_in_local_window(start_local: datetime, end_local: datetime):
    """
    Pull events in a local-time window; convert to UTC for Google Calendar.
    """
    service = get_gcal_service()

    tz = start_local.tzinfo
    if tz:
        start_utc = start_local.astimezone(ZoneInfo("UTC"))
        end_utc = end_local.astimezone(ZoneInfo("UTC"))
    else:
        start_utc = start_local
        end_utc = end_local

    time_min = start_utc.isoformat().replace("+00:00", "Z")
    time_max = end_utc.isoformat().replace("+00:00", "Z")

    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return events_result.get("items", [])

def get_goals_lookahead(days: int):
    """
    Fetch GOAL: events in the next `days` days (local TZ window).
    """
    now = now_local()
    try:
        tz = ZoneInfo(TIMEZONE) if TIMEZONE else None
    except Exception:
        tz = None

    if tz:
        start_local = datetime(now.year, now.month, now.day, tzinfo=tz)
    else:
        start_local = datetime(now.year, now.month, now.day)

    end_local = start_local + timedelta(days=days)

    events = get_events_in_local_window(start_local, end_local)

    goals = []
    for ev in events:
        title = ev.get("summary", "") or ""
        if not title.upper().startswith(GOAL_PREFIX.upper()):
            continue

        start_info = ev.get("start", {})
        end_info = ev.get("end", {})

        if "dateTime" in start_info:
            all_day = False
            sched_start = start_info.get("dateTime")
            sched_end = end_info.get("dateTime")
        else:
            all_day = True
            sched_start = start_info.get("date")
            sched_end = end_info.get("date")

        goals.append(
            {
                "id": ev["id"],
                "title": title[len(GOAL_PREFIX):].strip(),
                "raw_title": title,
                "scheduled_start": sched_start,
                "scheduled_end": sched_end,
                "all_day": all_day,
                "description": ev.get("description", "") or "",
            }
        )

    return goals

def build_goals_section(goals, lookahead_days: int, max_items: int = 5):
    if not goals:
        return "🎯 **Long-term focus:** (none scheduled)"

    today = now_local().date()

    lines = [f"🎯 **Long-term focus (next {lookahead_days} days):**"]
    for g in goals[:max_items]:
        d = g.get("scheduled_start")
        date_str = d[:10] if d else ""

        if date_str:
            try:
                goal_date = date.fromisoformat(date_str)  # YYYY-MM-DD
                delta_days = (goal_date - today).days

                if delta_days > 0:
                    suffix = f"({delta_days} days)"
                elif delta_days == 0:
                    suffix = "(today)"
                else:
                    suffix = f"({abs(delta_days)} days ago)"
            except Exception:
                suffix = ""

            lines.append(f"• {date_str} {suffix} — {g['title']}".rstrip())
        else:
            lines.append(f"• {g['title']}")

    if len(goals) > max_items:
        lines.append(f"…and {len(goals) - max_items} more.")
    return "\n".join(lines)

def _get_effective_window(task, user_id=None):
    """
    Returns (start_local, end_local) for a task.
    If All-Day, starts at first check-in (or START_OF_DAY_HOUR) and ends at END_OF_DAY_HOUR.
    """
    if not task.get("all_day"):
        return _to_local(_parse_dt(task.get("scheduled_start"))), _to_local(_parse_dt(task.get("scheduled_end")))

    now = now_local()
    # Start: Today's check-in or fallback to start hour
    checkin = get_today_first_checkin_time(user_id or PRIMARY_USER_ID)
    if checkin:
        start_dt = checkin
    else:
        start_dt = now.replace(hour=START_OF_DAY_HOUR, minute=0, second=0, microsecond=0)
    
    # End: Today's EOD hour
    end_dt = now.replace(hour=END_OF_DAY_HOUR, minute=0, second=0, microsecond=0)
    
    return start_dt, end_dt


def build_task_message(tasks, statuses, goals=None):
    now = now_local()
    lines = ["📋 **Today's tasks:**"]

    for idx, (task, status) in enumerate(zip(tasks, statuses)):
        if idx >= len(NUM_EMOJIS): break
        emoji = NUM_EMOJIS[idx]
        title = task["title"]
        start_local, end_local = _get_effective_window(task)
        
        overdue = False
        if RED_WARNINGS and status == "pending" and end_local and now > end_local:
            overdue = True

        if status == "pending" and not overdue:
            status_symbol = _pending_time_symbol(now, start_local, end_local)
        else:
            status_symbol = STATUS_EMOJI.get(status, "⚪")

        if overdue:
            lines.append(f"{emoji} 🔴 **{title}** 🔴 OVERDUE")
        else:
            lines.append(f"{emoji} {status_symbol} **{title}**")

        if task.get("all_day"):
            lines.append("    🕒 All day")
        elif start_local and end_local:
            lines.append(f"    🕒 {start_local.strftime('%I:%M %p').lstrip('0')} → {end_local.strftime('%I:%M %p').lstrip('0')}")

    # Goals section - NO API CALLS HERE
    if goals:
        lines.append("\n" + build_goals_section(goals, GOAL_LOOKAHEAD_DAYS, max_items=5))

    lines.append("\nReact with the **number emoji** to complete.\nReact with ❌ to skip rest.\nReact with 🔄 to refresh.")
    return "\n".join(lines)

# ----------------- DISCORD BOT -----------------

async def maybe_run_missed_checkin():
    """
    If the bot starts AFTER the daily check-in time,
    auto-run the check-in so we have a task message to react to.
    """
    now = now_local()

    checkin_time = now.replace(
        hour=START_OF_DAY_HOUR, minute=0, second=0, microsecond=0
    )

    # If bot started after start-of-day, generate today's list once
    if now > checkin_time:
        channel = bot.get_channel(CHANNEL_ID)
        if not channel:
            try:
                channel = await bot.fetch_channel(CHANNEL_ID)
            except Exception:
                return


        await channel.send(
            f"{MENTION_TARGET} ⚠️ **Bot started after the daily check-in time. "
            "Automatically generating today's task list...**"
        )

        if PRIMARY_USER_ID is None:
            await channel.send(
                "⚠️ Could not auto-checkin: PRIMARY_USER_ID is not set in .env."
            )
            return

        fake_user = SimpleUser(PRIMARY_USER_ID)
        await handle_checkin(channel, fake_user)

@bot.check
async def globally_restrict_to_channel(ctx):
    # If CHANNEL_ID is 0 or not set, allow all (safety), 
    # otherwise only allow the specific channel.
    if CHANNEL_ID == 0:
        return True
    return ctx.channel.id == CHANNEL_ID

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    load_runtime_state()
    await restore_last_task_board_if_needed()
    save_runtime_state()

    if not CHANNEL_ID:
        print("CHANNEL_ID not set; scheduled tasks will not run.")
        return

    # Start daily checkin
    if not daily_checkin_prompt.is_running():
        daily_checkin_prompt.start()

    # Auto-checkin if bot started after configured start-of-day time
    await maybe_run_missed_checkin()

    # Reminder loop
    if (REMIND_AT_START or REMIND_AT_END or KEEP_REMINDING) and not reminder_loop.is_running():
        reminder_loop.start()

    # End of day summary
    if END_OF_DAY_SUMMARY and not end_of_day_summary_loop.is_running():
        end_of_day_summary_loop.start()

    # Phone pings
    if PHONE_PING_ENABLED and not phone_ping_loop.is_running():
        phone_ping_loop.start()
        print(f"📶 Phone ping loop enabled; interval = {PHONE_PING_INTERVAL_MINUTES} min")
    else:
        print("📵 Phone ping loop disabled (env or import missing).")


@tasks.loop(minutes=1)
async def reminder_loop():
    """Periodic check for upcoming / overdue tasks + auto-refresh task list if calendar changes."""
    if not (REMIND_AT_START or REMIND_AT_END or KEEP_REMINDING):
        return

    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    # 1. Update the UI colors/emojis on the active message
    await maybe_tick_task_message(channel)

    now = now_local()

    # 2. AUTO-REFRESH: compare calendar snapshot vs posted task list snapshot
    info = getattr(bot, "last_task_message", None)
    if info and info.get("message_id") and info.get("channel_id") == channel.id:
        if info.get("for_date") == now.date().isoformat():
            try:
                loop = asyncio.get_running_loop()
                latest_tasks = await loop.run_in_executor(None, get_todays_tasks)
                latest_fp = _task_fingerprint(latest_tasks)
                old_fp = info.get("fingerprint")

                if old_fp is not None and latest_fp != old_fp:
                    if not refresh_lock.locked():
                        try:
                            live_msg = await channel.fetch_message(info["message_id"])
                            fake_user = SimpleUser(info["user_id"])
                            await refresh_task_message(live_msg, fake_user, reason="auto")
                        except Exception as e:
                            print(f"⚠️ Auto-refresh failed: {e}")
            except Exception as e:
                print(f"⚠️ Auto-refresh compare error: {e}")

    # 3. NOTIFICATIONS: Logic for pings
    loop = asyncio.get_running_loop()
    status_map = await loop.run_in_executor(None, get_today_status_map, PRIMARY_USER_ID)
    tasks_list = await loop.run_in_executor(None, get_todays_tasks)

    if not tasks_list:
        return

    for task in tasks_list:
        task_id = task["id"]
        status = status_map.get(task_id, "pending")
        if status != "pending":
            continue

        # Use normalized window: All-day tasks start at check-in and end at EOD_HOUR
        start_local, end_local = _get_effective_window(task)
        if start_local is None or end_local is None:
            continue

        title = task["title"]
        is_all_day = task.get("all_day", False)

        # A) Remind at start
        if (
            REMIND_AT_START
            and now >= start_local
            and task_id not in reminder_state["start_sent"]
        ):
            if is_all_day:
                msg = f"{MENTION_TARGET} ⏰ **All-day task active:** `{title}`"
            else:
                msg = f"{MENTION_TARGET} ⏰ **Reminder:** `{title}` is scheduled for now ({start_local.strftime('%I:%M %p').lstrip('0')})."
            
            await channel.send(msg)
            reminder_state["start_sent"].add(task_id)

        # B) Remind at end (if still pending)
        if (
            REMIND_AT_END
            and now >= end_local
            and task_id not in reminder_state["end_sent"]
        ):
            if is_all_day:
                msg = f"{MENTION_TARGET} ⚠️ **EOD Overdue:** `{title}` is still pending at the end of the day."
            else:
                msg = f"{MENTION_TARGET} ⚠️ **Overdue:** `{title}` was scheduled to end at {end_local.strftime('%I:%M %p').lstrip('0')}."
            
            await channel.send(msg)
            reminder_state["end_sent"].add(task_id)

        # C) Keep reminding (EXCLUDED for all-day tasks to minimize noise)
        if KEEP_REMINDING and not is_all_day and now >= start_local:
            last_nag = reminder_state["nag_last"].get(task_id)
            if (
                last_nag is None
                or (now - last_nag).total_seconds() >= REMIND_INTERVAL_MINUTES * 60
            ):
                await channel.send(
                    f"{MENTION_TARGET} 🔁 **Still pending:** `{title}`.\n"
                    f"React with the number in today's task list when you've completed it."
                )
                reminder_state["nag_last"][task_id] = now
@tasks.loop(hours=24)
async def end_of_day_summary_loop():
    if not END_OF_DAY_SUMMARY:
        return

    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    loop = asyncio.get_running_loop()

    # --- today's summary ---
    tasks_today = await loop.run_in_executor(None, get_todays_tasks)

    if not tasks_today:
        summary_text = (
            f"{MENTION_TARGET} 🌙 **End of day summary:** No TASK: events were scheduled today."
        )
    else:
        # FIX: Offload blocking disk read
        status_map = await loop.run_in_executor(None, get_today_status_map, PRIMARY_USER_ID)
        total = len(tasks_today)
        completed = 0
        skipped = 0
        pending = 0

        for t in tasks_today:
            s = status_map.get(t["id"], "pending")
            if s == "completed":
                completed += 1
            elif s == "skipped":
                skipped += 1
            else:
                pending += 1

        summary_text = (
            f"{MENTION_TARGET} 🌙 **End of day summary:**\n"
            f"✅ Completed: {completed}\n"
            f"❌ Skipped: {skipped}\n"
            f"⚪ Pending: {pending}\n"
            f"📊 Total tasks: {total}"
        )

    # FIX: Offload blocking disk reads
    checkin_dt = await loop.run_in_executor(None, get_today_first_checkin_time, PRIMARY_USER_ID)
    checkin_str = _format_time_local(checkin_dt) if checkin_dt else None

    wake_dt = None
    wake_str = None
    if PHONE_PING_ENABLED:
        # FIX: Offload blocking disk read
        wake_dt = await loop.run_in_executor(None, get_today_first_phone_online_time, PRIMARY_USER_ID)
        if not wake_dt:
            wake_dt = phone_online_state.get("first_online_ts")
        wake_str = _format_time_local(wake_dt) if wake_dt else None

    extra_lines = []
    extra_lines.append(f"🕒 Checked in: {checkin_str if checkin_str else '—'}")
    if PHONE_PING_ENABLED:
        extra_lines.append(f"📱 Wake ping: {wake_str if wake_str else '—'}")

    summary_text = summary_text + "\n" + "\n".join(extra_lines)

    # --- tomorrow overlook (append) ---
    tomorrow = now_local().date() + timedelta(days=1)
    tasks_tomorrow = await loop.run_in_executor(None, get_tasks_for_date, tomorrow)

    overlook_text = build_task_overlook_message(tomorrow, tasks_tomorrow)

    # --- drift logging (completed timed tasks only) ---
    try:
        # FIX: Offload blocking disk read
        data = await loop.run_in_executor(None, load_log)
        today_str = now_local().date().isoformat()

        completed_ts = {}
        for entry in data.get("tasks", []):
            if entry.get("date") != today_str:
                continue
            if PRIMARY_USER_ID is not None and entry.get("user_id") != PRIMARY_USER_ID:
                continue
            if entry.get("status") == "completed":
                completed_ts[entry["task_id"]] = entry.get("timestamp")

        drifts = []
        for t in tasks_today:
            if t.get("all_day"):
                continue
            end_dt = _parse_dt(t.get("scheduled_end"))
            end_local = _to_local(end_dt)
            if end_local is None:
                continue

            ts_str = completed_ts.get(t["id"])
            if not ts_str:
                continue

            try:
                done_dt = datetime.fromisoformat(ts_str)
            except Exception:
                continue

            done_local = _to_local(done_dt)
            if done_local is None:
                continue

            diff_min = (done_local - end_local).total_seconds() / 60.0
            drifts.append(diff_min)

        if drifts:
            avg = sum(drifts) / len(drifts)
            # FIX: Wrap internal drift logging IO
            await loop.run_in_executor(None, log_drift_summary, now_local().date(), avg, len(drifts))
    except Exception as e:
        print(f"⚠️ Drift logging error: {e}")

    await channel.send(summary_text + "\n\n" + overlook_text)


@end_of_day_summary_loop.before_loop
async def before_end_of_day_summary():
    await bot.wait_until_ready()
    if not END_OF_DAY_SUMMARY:
        return
    now = now_local()
    target = now.replace(hour=END_OF_DAY_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())


@tasks.loop(hours=24)
async def daily_checkin_prompt():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print("Channel not found for daily_checkin_prompt")
        return

    msg = await channel.send(
        f"{MENTION_TARGET} 🌅 **Daily check-in ready when you are.**\n"
        "React with ✅ when you're ready to pull today’s tasks."
    )
    await msg.add_reaction("✅")

    # track this message so ✅ reactions on it trigger handle_checkin
    bot.last_checkin_message_id = msg.id
    save_runtime_state()



@daily_checkin_prompt.before_loop
async def before_daily_checkin():
    await bot.wait_until_ready()
    now = now_local()
    target = now.replace(
        hour=START_OF_DAY_HOUR, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())


# ----------------- PHONE PING LOOP -----------------

def has_today_checkin(user_id: int | None) -> bool:
    """Return True if this user has at least one checkin entry for *today* (local TZ)."""
    if user_id is None:
        return False
    data = load_log()
    today_str = now_local().date().isoformat()
    for entry in data.get("checkins", []):
        if entry.get("user_id") == user_id and entry.get("date") == today_str:
            return True
    return False


@tasks.loop(minutes=1)
async def phone_ping_loop():
    """
    New behavior:
    - Only start caring after the daily 🌅 check-in message has been posted.
    - If the phone is online AND you have NOT checked in today:
        -> Ping you in Discord.
    - Keep doing that on PHONE_PING_INTERVAL_MINUTES until you check in.
    - After you've checked in, stop doing anything for the rest of the day.
    """
    if not PHONE_PING_ENABLED:
        return

    await bot.wait_until_ready()

    now = now_local()

    # Don't start until the daily check-in message has been posted
    if getattr(bot, "last_checkin_message_id", None) is None:
        return

    # Respect start-of-day; don't bother pinging before that
    if now.hour < START_OF_DAY_HOUR:
        return

    today = now.date()
    if phone_online_state["today"] != today:
        phone_online_state["today"] = today
        phone_online_state["seen_online"] = False
        phone_online_state["first_online_ts"] = None   # <-- add
        phone_online_state["last_notify"] = None


    # If you've already checked in today, we don't care about phone status anymore.
    if has_today_checkin(PRIMARY_USER_ID):
        return

    if WIFI_IP_ONLY is None or ping_device is None:
        return

    # Only ping on our configured interval
    minutes_since_midnight = now.hour * 60 + now.minute
    if minutes_since_midnight % PHONE_PING_INTERVAL_MINUTES != 0:
        return

    loop = asyncio.get_running_loop()
    try:
        reachable = await loop.run_in_executor(None, ping_device, WIFI_IP_ONLY)
    except Exception as e:
        print(f"⚠️ Phone ping error: {e}")
        return

    # Debug (optional): uncomment if you want to see it's doing work
    # print(f"[phone_ping_loop] reachable={reachable}, has_checkin={has_today_checkin(PRIMARY_USER_ID)} at {now}")

    if not reachable:
        return

    # Record first successful ping of the day (wake proxy)
    if phone_online_state.get("first_online_ts") is None:
        phone_online_state["first_online_ts"] = now
        log_phone_first_online(PRIMARY_USER_ID, now)



    phone_online_state["seen_online"] = True

    # Throttle notifications so you don't get spammed every interval
    last_notify = phone_online_state.get("last_notify")
    if last_notify is not None:
        diff = (now - last_notify).total_seconds()
        if diff < PHONE_PING_INTERVAL_MINUTES * 60:
            return  # too soon to send another reminder

    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await channel.send(
            f"{MENTION_TARGET} 📱 **Phone appears to be online, but you haven't checked in yet.**\n"
            "React with ✅ on the daily check-in message to pull today's tasks."
        )
        phone_online_state["last_notify"] = now

# ----------------- TASK MESSAGE (CHECKIN + REFRESH) -----------------

@bot.command(name="checkin")
async def manual_checkin(ctx):
    await handle_checkin(ctx.channel, ctx.author)


async def handle_checkin(channel, user):
    ts = now_local()
    log_checkin(user.id, ts)

    await channel.send("📡 Pulling tasks from your calendar...")
    loop = asyncio.get_running_loop()
    
    # Offload blocking IO to executors
    tasks = await loop.run_in_executor(None, get_todays_tasks)
    goals = await loop.run_in_executor(None, get_goals_lookahead, GOAL_LOOKAHEAD_DAYS)

    if not tasks:
        await channel.send("✅ No TASK: events found for today. You’re clear!")
        bot.last_task_message = {
            "message_id": None,
            "channel_id": channel.id,
            "tasks": [],
            "user_id": user.id,
            "statuses": [],
            "fingerprint": _task_fingerprint([]),
            "for_date": now_local().date().isoformat(),
        }
        save_runtime_state()
        return

    # Initialize statuses from today's log (persistence)
    status_map = get_today_status_map(user.id)
    statuses = [status_map.get(t["id"], "pending") for t in tasks]

    # Use refactored builder that accepts goals as a parameter
    content = build_task_message(tasks, statuses, goals=goals)
    msg = await channel.send(content)
    render_hash = _content_hash(content)

    # Add reactions for pending tasks
    for idx, status in enumerate(statuses):
        if idx >= len(NUM_EMOJIS):
            break
        if status == "pending":
            await msg.add_reaction(NUM_EMOJIS[idx])

    await msg.add_reaction("❌")
    await msg.add_reaction(REFRESH_EMOJI)

    bot.last_task_message = {
        "message_id": msg.id,
        "channel_id": channel.id,
        "tasks": tasks,
        "user_id": user.id,
        "statuses": statuses,
        "fingerprint": _task_fingerprint(tasks),
        "for_date": now_local().date().isoformat(),
        "render_hash": render_hash,
    }
    save_runtime_state()


async def refresh_task_message(message, user, reason: str = "manual"):
    async with refresh_lock:
        loop = asyncio.get_running_loop()
        channel = message.channel

        # Pull everything via executors
        tasks = await loop.run_in_executor(None, get_todays_tasks)
        goals = await loop.run_in_executor(None, get_goals_lookahead, GOAL_LOOKAHEAD_DAYS)
        status_map = await loop.run_in_executor(None, get_today_status_map, user.id)

        if reason == "auto":
            try: await channel.send("🔁 **Task list changed. Refreshing...**")
            except: pass

        new_fp = _task_fingerprint(tasks)
        today_iso = now_local().date().isoformat()

        # Update old message state
        try:
            await message.edit(content=message.content + "\n\n*(This task list is outdated.)*")
            await message.clear_reactions()
        except: pass

        if not tasks:
            content = "📋 **Today's tasks (after refresh):**\n(no TASK: events found.)"
            new_msg = await channel.send(content)
            bot.last_task_message = {
                "message_id": new_msg.id, "channel_id": channel.id, "tasks": [],
                "user_id": user.id, "statuses": [], "fingerprint": new_fp,
                "for_date": today_iso, "render_hash": _content_hash(content),
            }
        else:
            statuses = [status_map.get(t["id"], "pending") for t in tasks]
            content = build_task_message(tasks, statuses, goals=goals)
            new_msg = await channel.send(content)

            for idx, status in enumerate(statuses):
                if idx < len(NUM_EMOJIS) and status == "pending":
                    await new_msg.add_reaction(NUM_EMOJIS[idx])
            await new_msg.add_reaction("❌")
            await new_msg.add_reaction(REFRESH_EMOJI)

            bot.last_task_message = {
                "message_id": new_msg.id, "channel_id": channel.id, "tasks": tasks,
                "user_id": user.id, "statuses": statuses, "fingerprint": new_fp,
                "for_date": today_iso, "render_hash": _content_hash(content),
            }
        save_runtime_state()


# ----------------- REACTION HANDLING -----------------

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    message = reaction.message
    emoji = str(reaction.emoji)
    loop = asyncio.get_running_loop()

    # 1) Daily check-in message
    if (
        message.author.id == bot.user.id
        and emoji == "✅"
        and getattr(bot, "last_checkin_message_id", None) == message.id
    ):
        await handle_checkin(message.channel, user)
        return

    # 2) Task message reactions
    info = getattr(bot, "last_task_message", None)
    if not info or message.id != info.get("message_id") or user.id != info.get("user_id"):
        return

    tasks = info["tasks"]
    statuses = info["statuses"]
    now = now_local()

    # Number emoji: complete a task
    if emoji in NUM_EMOJIS:
        idx = NUM_EMOJIS.index(emoji)
        if idx < len(tasks) and statuses[idx] == "pending":
            task_id = tasks[idx]["id"]

            statuses[idx] = "completed"
            # FIX: Offload disk write
            await loop.run_in_executor(None, log_task_completion, user.id, task_id, "completed", now)

            _clear_reminder_state_for_task(task_id)

            # Fetch goals so they don't disappear from the render
            goals = await loop.run_in_executor(None, get_goals_lookahead, GOAL_LOOKAHEAD_DAYS)
            new_content = build_task_message(tasks, statuses, goals=goals)
            
            await message.edit(content=new_content)
            bot.last_task_message["statuses"] = statuses
            save_runtime_state()

    # ❌: mark all remaining as skipped
    elif emoji == "❌":
        changed = False
        for i, status in enumerate(statuses):
            if status == "pending":
                task_id = tasks[i]["id"]
                statuses[i] = "skipped"
                # FIX: Offload disk write
                await loop.run_in_executor(None, log_task_completion, user.id, task_id, "skipped", now)

                _clear_reminder_state_for_task(task_id)
                changed = True

        if changed:
            goals = await loop.run_in_executor(None, get_goals_lookahead, GOAL_LOOKAHEAD_DAYS)
            new_content = build_task_message(tasks, statuses, goals=goals)
            await message.edit(content=new_content)
            bot.last_task_message["statuses"] = statuses
            save_runtime_state()

    # 🔄: refresh from Google Calendar
    elif emoji == REFRESH_EMOJI:
        await refresh_task_message(message, user, reason="manual")
        

# ----------------- MAIN -----------------

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set in environment or .env")
    load_runtime_state()
    bot.run(DISCORD_TOKEN)
