import sys, io, base64, webbrowser, os, threading, time, ctypes, json
from datetime import datetime
from pystray import Icon, Menu, MenuItem
from PIL import Image, ImageTk  # ImageTk for Tk icon

# -------------------------------------------------------------------
# TWILIO SETUP (reads credentials from environment variables)
#   Set these in PowerShell (per-user):
#     setx TWILIO_ACCOUNT_SID "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
#     setx TWILIO_AUTH_TOKEN  "your_auth_token_here"
#     setx TWILIO_FROM_E164   "+1xxxxxxxxxx"   # SMS-capable Twilio number
#   Then restart the app/terminal.
# -------------------------------------------------------------------

# --------------------------
# CONSTANTS & PATHS
# --------------------------
BASE_URL = "https://app.healthboxhr.com/"
APPDATA_DIR = os.path.join(os.environ["APPDATA"], "MoffettClocker")
STATE_FILE = os.path.join(APPDATA_DIR, "status.txt")
LAST_OFF_PROMPT_FILE = os.path.join(APPDATA_DIR, "last_off_prompt.txt")
LAST_ON_PROMPT_FILE  = os.path.join(APPDATA_DIR, "last_on_prompt.txt")
COUNTS_FILE          = os.path.join(APPDATA_DIR, "prompt_counts.json")  # per-day counts

# Monthly SMS budget (calendar month)
MONTH_COUNTS_FILE = os.path.join(APPDATA_DIR, "prompt_counts_month.json")

SETTINGS_FILE        = os.path.join(APPDATA_DIR, "settings.json")

# Fixed poll interval
POLL_INTERVAL_SEC = 10

# --------------------------
# DEFAULT SETTINGS
# --------------------------
DEFAULT_SETTINGS = {
    # OFF state: user active but not clocked in -> "Forgot to clock in?"
    "enable_clock_in_reminder": True,
    "clock_in_cutoff_hour": 15,            # 1–24, only show before this hour (24 = all day)
    "active_threshold_off_min": 30,        # minutes of continuous activity before prompting
    "off_prompt_cooldown_min": 210,        # min gap between prompts (minutes) ~3.5h
    "max_clock_in_per_day": 3,             # daily max

    # ON state: user idle while clocked in -> "Still working?"
    "enable_clock_out_idle_reminder": True,
    "on_idle_threshold_min": 45,           # minutes of idle before prompting
    "on_idle_prompt_cooldown_min": 210,    # min gap between prompts (minutes)
    "on_idle_after_hour": 0,               # only show this after (0–23). 0 = always allowed
    "max_clock_out_per_day": 3,            # daily max

    # SMS clock-out reminder
    "enable_sms_clock_out_reminder": False,
    "sms_phone_e164": "",                  # normalized +E.164 (e.g., +358401234567)
    "sms_only_after_hour": 0,              # (kept for backward compat; unused by new window check)
    "sms_idle_threshold_min": 60,          # minutes
    "sms_max_per_day": 1,                  # per-day cap
    "sms_window_start_hour": 12,           # 0–23 inclusive
    "sms_window_end_hour": 22,             # 1–24 exclusive; 24 = midnight
    "sms_max_per_month": 10,               # hard monthly cap

    # Notification days (applies to all reminders)
    "notify_days": {                       # Monday..Sunday
        "mon": True, "tue": True, "wed": True, "thu": True,
        "fri": True, "sat": False, "sun": False
    },

    # General (not user-exposed)
    "active_idle_cutoff_sec": 300          # idle >= this resets "active" streak (seconds)
}

_settings_lock = threading.Lock()
_settings_cache = None
_config_window_open = False

# --------------------------
# SETTINGS IO
# --------------------------
def ensure_settings_file():
    """If settings.json doesn't exist, create it with defaults once."""
    os.makedirs(APPDATA_DIR, exist_ok=True)
    if not os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_SETTINGS, f, indent=2)
        except Exception:
            pass

def load_settings():
    """Load from disk (or defaults if missing/invalid) and cache."""
    global _settings_cache
    os.makedirs(APPDATA_DIR, exist_ok=True)
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        else:
            data = dict(DEFAULT_SETTINGS)
    except Exception:
        data = dict(DEFAULT_SETTINGS)

    # Only accept known keys; unknowns are ignored
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: data.get(k, v) for k, v in DEFAULT_SETTINGS.items()})

    with _settings_lock:
        _settings_cache = merged
    return merged

def save_settings(new_values: dict):
    """Write to disk and update cache. Keep it dead simple."""
    os.makedirs(APPDATA_DIR, exist_ok=True)
    # Only write known keys; fall back to defaults if missing
    clean = {k: new_values.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
    finally:
        with _settings_lock:
            _settings_cache = dict(clean)

def get_settings():
    with _settings_lock:
        if _settings_cache is None:
            return load_settings()
        return dict(_settings_cache)

# --------------------------
# PER-DAY COUNTS
# --------------------------
def _today():
    return datetime.now().strftime("%Y-%m-%d")

def _load_counts():
    os.makedirs(APPDATA_DIR, exist_ok=True)
    today = _today()
    data = {"date": today, "on": 0, "off": 0, "sms": 0}
    try:
        if os.path.exists(COUNTS_FILE):
            with open(COUNTS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f) or {}
            if loaded.get("date") == today:
                # keep backward compatible if 'sms' missing
                data = {
                    **data,
                    **{k: int(loaded.get(k, 0)) for k in ("on", "off", "sms")}
                }
    except Exception:
        pass
    if data.get("date") != today:
        data = {"date": today, "on": 0, "off": 0, "sms": 0}
    return data

def _save_counts(data):
    try:
        with open(COUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

# --------------------------
# MONTHLY COUNTS (SMS budget)
# --------------------------
def _this_month():
    return datetime.now().strftime("%Y-%m")

def _load_month_counts():
    os.makedirs(APPDATA_DIR, exist_ok=True)
    current = _this_month()
    data = {"month": current, "sms": 0}
    try:
        if os.path.exists(MONTH_COUNTS_FILE):
            with open(MONTH_COUNTS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f) or {}
            if loaded.get("month") == current:
                data = {"month": current, "sms": int(loaded.get("sms", 0))}
    except Exception:
        pass
    if data.get("month") != current:
        data = {"month": current, "sms": 0}
    return data

def _save_month_counts(data):
    try:
        with open(MONTH_COUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def get_month_sms_count() -> int:
    return int(_load_month_counts().get("sms", 0))

def inc_month_sms_count():
    data = _load_month_counts()
    data["sms"] = int(data.get("sms", 0)) + 1
    _save_month_counts(data)


def get_count(kind: str) -> int:
    data = _load_counts()
    return int(data.get(kind, 0))

def inc_count(kind: str):
    data = _load_counts()
    data[kind] = int(data.get(kind, 0)) + 1
    _save_counts(data)

# --------------------------
# EMBEDDED ICONS
# --------------------------
m_on_b64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAACXBIWXMAAC4jAAAuIwF4pT92AAAGhWlUWHRYTUw6Y29tLmFkb2JlLnhtcAAAAAAAPD94cGFja2V0IGJlZ2luPSLvu78iIGlkPSJXNU0wTXBDZWhpSHpyZVN6TlRjemtjOWQiPz4gPHg6eG1wbWV0YSB4bWxuczp4PSJhZG9iZTpuczptZXRhLyIgeDp4bXB0az0iQWRvYmUgWE1QIENvcmUgOS4xLWMwMDMgNzkuOTY5MGE4NywgMjAyNS8wMy8wNi0xOToxMjowMyAgICAgICAgIj4gPHJkZjpSREYgeG1sbnM6cmRmPSJodHRwOi8vd3d3LnczLm9yZy8xOTk5LzAyLzIyLXJkZi1zeW50YXgtbnMjIj4gPHJkZjpEZXNjcmlwdGlvbiByZGY6YWJvdXQ9IiIgeG1sbnM6eG1wPSJodHRwOi8vbnMuYWRvYmUuY29tL3hhcC8xLjAvIiB4bWxuczpkYz0iaHR0cDovL3B1cmwub3JnL2RjL2VsZW1lbnRzLzEuMS8iIHhtbG5zOnBob3Rvc2hvcD0iaHR0cDovL25zLmFkb2JlLmNvbS9waG90b3Nob3AvMS4wLyIgeG1sbnM6eG1wTU09Imh0dHA6Ly9ucy5hZG9iZS5jb20veGFwLzEuMC9tbS8iIHhtbG5zOnN0RXZ0PSJodHRwOi8vbnMuYWRvYmUuY29tL3hhcC8xLjAvc1R5cGUvUmVzb3VyY2VFdmVudCMiIHhtcDpDcmVhdG9yVG9vbD0iQWRvYmUgUGhvdG9zaG9wIDI2LjExICgyMDI1MDkwNy5tLjMyMTEgYjgzMjczNykgIChXaW5kb3dzKSIgeG1wOkNyZWF0ZURhdGU9IjIwMjUtMDktMTBUMTc6MDc6MDArMDI6MDAiIHhtcDpNb2RpZnlEYXRlPSIyMDI1LTA5LTEwVDE3OjE5OjIzKzAyOjAwIiB4bXA6TWV0YWRhdGFEYXRlPSIyMDI1LTA5LTEwVDE3OjE5OjIzKzAyOjAwIiBkYzpmb3JtYXQ9ImltYWdlL3BuZyIgcGhvdG9zaG9wOkNvbG9yTW9kZT0iMyIgeG1wTU06SW5zdGFuY2VJRD0ieG1wLmlpZDoxYjNiNjI3Yy01ZTFjLTVhNGQtOGUxNy00Mzk5ZGFmNWU1ZDkiIHhtcE1NOkRvY3VtZW50SUQ9ImFkb2JlOmRvY2lkOnBob3Rvc2hvcDpmNDAxMmQyZC05NzhmLWU2NDktODQxOS04MjQwNGFmZTczZTAiIHhtcE1NOk9yaWdpbmFsRG9jdW1lbnRJRD0ieG1wLmRpZDozZDRjMWJiNC0zOWM2LTkzNDUtYTczMS0wZWEzMjI5NGEzZGYiPiA8eG1wTU06SGlzdG9yeT4gPHJkZjpTZXE+IDxyZGY6bGkgc3RFdnQ6YWN0aW9uPSJjcmVhdGVkIiBzdEV2dDppbnN0YW5jZUlEPSJ4bXAuaWlkOjNkNGMxYmI0LTM5YzYtOTM0NS1hNzMxLTBlYTMyMjk0YTNkZiIgc3RFdnQ6d2hlbj0iMjAyNS0wOS0xMFQxNzowNzowMCswMjowMCIgc3RFdnQ6c29mdHdhcmVBZ2VudD0iQWRvYmUgUGhvdG9zaG9wIDI2LjExICgyMDI1MDkwNy5tLjMyMTEgYjgzMjczNykgIChXaW5kb3dzKSIvPiA8cmRmOmxpIHN0RXZ0OmFjdGlvbj0iY29udmVydGVkIiBzdEV2dDpwYXJhbWV0ZXJzPSJmcm9tIGFwcGxpY2F0aW9uL3ZuZC5hZG9iZS5waG90b3Nob3AgdG8gaW1hZ2UvcG5nIi8+IDxyZGY6bGkgc3RFdnQ6YWN0aW9uPSJzYXZlZCIgc3RFdnQ6aW5zdGFuY2VJRD0ieG1wLmlpZDoxYjNiNjI3Yy01ZTFjLTVhNGQtOGUxNy00Mzk5ZGFmNWU1ZDkiIHN0RXZ0OndoZW49IjIwMjUtMDktMTBUMTc6MTk6MjMrMDI6MDAiIHN0RXZ0OnNvZnR3YXJlQWdlbnQ9IkFkb2JlIFBob3Rvc2hvcCAyNi4xMSAoMjAyNTA5MDcubS4zMjExIGI4MzI3MzcpICAoV2luZG93cykiIHN0RXZ0OmNoYW5nZWQ9Ii8iLz4gPC9yZGY6U2VxPiA8L3htcE1NOkhpc3Rvcnk+IDwvcmRmOkRlc2NyaXB0aW9uPiA8L3JkZjpSREY+IDwveDp4bXBtZXRhPiA8P3hwYWNrZXQgZW5kPSJyIj8+wWkmkAAAAxxJREFUeNrtmk1IVGEUhicrMGIGiiBBCCmM6GcTCiEVgauihQaRYPRDBOUihCgqWhjhzyrJwAKjn0VECUmbKERbRS6sRfSzqI2EkSiWFpSF3c6FO3DRb86x834XcuYs3o2MD+c+880733fvpIIgSBVyUibABJgAE2ACTIAJMAEmwASYADYf0xsXULZRrlJeUiYpwX+aEcpzSielllIMCSDA2ggYzNN8oTRTMv8sgP6pgvJ1Hl98PMOUrXMWQC9eTvmUJxefzS/KrrkKuJxnF5/NN0o5K4BesIzyPU8FhOmTBBzJ44vPppIT0F0AAi5xAj4XgIDBXBdfWgAXH2Yyl4A9AHSa0kWpo9TMSLgrm1Jy70aMg5QLlKc+JOQS0AxAzws7Si231sGroowlIeCxEvgn3DwxAuqBYUtzMA8kIWBUCXwjbKw6tFtYhpn2KoD+uAoA3hAEDCi5DwTuhLcSBAvwGDPkYqAAzwgCfiu5Lb4LcDMzZCXArWa4JQB3t88C/EFZxAzaABRrhuFWAwJW+izAZ8Iyva3kvhW4J5XcoVmHIbAA24VB3ym5twTuPSW32yUAKcA6ZshMtJQ13AZBwJCSe9olACnANQl9TisSKsAdLgHaAhwT3qWzSu7P8OuT4dYA55W0S4C2AB8JAnqU3AGB24ruWOOwMmA5NQmDDiu5HQK3X8m96RKwFxCwkxkSubdQz3AXRjc3NdzjLgFtwKArmEGRb5ZyhrvJR7HGgb1K2AdhmWrFjoeP4xjuUSV3Kl6sceC49k6NIKBPyX0icK/7KNYsbDWwnBqZIYuAh6gXBQGvldwrLgFIAVYxQ65HTmrCTZBpJXe/S4D2cxqew5cwgx5CTmoJ7SzXuQRoC/CFsEw7kZMawz2n5E7MLFa0AK8Jgw4qufcF7kMlt3fW7wPAAjzMDFkM3Ko6JQgY0d4CcwlACnADM+QWgLud4Zb5fLaAFGC4DS1iBj0BnNSWMtx9Pp8tIAXYLyzTO0ruK4Hb7vPZAlKAbcKg75XcLoGr/bFWj/1O0ASYABNgAkyACTABJsAEmAATYAIiAX8B4GS/l8f2X4AAAAAASUVORK5CYII="
m_off_b64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAACXBIWXMAAC4jAAAuIwF4pT92AAAGhWlUWHRYTUw6Y29tLmFkb2JlLnhtcAAAAAAAPD94cGFja2V0IGJlZ2luPSLvu78iIGlkPSJXNU0wTXBDZWhpSHpyZVN6TlRjemtjOWQiPz4gPHg6eG1wbWV0YSB4bWxuczp4PSJhZG9iZTpuczptZXRhLyIgeDp4bXB0az0iQWRvYmUgWE1QIENvcmUgOS4xLWMwMDMgNzkuOTY5MGE4NywgMjAyNS8wMy8wNi0xOToxMjowMyAgICAgICAgIj4gPHJkZjpSREYgeG1sbnM6cmRmPSJodHRwOi8vd3d3LnczLm9yZy8xOTk5LzAyLzIyLXJkZi1zeW50YXgtbnMjIj4gPHJkZjpEZXNjcmlwdGlvbiByZGY6YWJvdXQ9IiIgeG1sbnM6eG1wPSJodHRwOi8vbnMuYWRvYmUuY29tL3hhcC8xLjAvIiB4bWxuczpkYz0iaHR0cDovL3B1cmwub3JnL2RjL2VsZW1lbnRzLzEuMS8iIHhtbG5zOnBob3Rvc2hvcD0iaHR0cDovL25zLmFkb2JlLmNvbS9waG90b3Nob3AvMS4wLyIgeG1sbnM6eG1wTU09Imh0dHA6Ly9ucy5hZG9iZS5jb20veGFwLzEuMC9tbS8iIHhtbG5zOnN0RXZ0PSJodHRwOi8vbnMuYWRvYmUuY29tL3hhcC8xLjAvc1R5cGUvUmVzb3VyY2VFdmVudCMiIHhtcDpDcmVhdG9yVG9vbD0iQWRvYmUgUGhvdG9zaG9wIDI2LjExICgyMDI1MDkwNy5tLjMyMTEgYjgzMjczNykgIChXaW5kb3dzKSIgeG1wOkNyZWF0ZURhdGU9IjIwMjUtMDktMTBUMTc6MDc6MDArMDI6MDAiIHhtcDpNb2RpZnlEYXRlPSIyMDI1LTA5LTEwVDE3OjE5OjQ0KzAyOjAwIiB4bXA6TWV0YWRhdGFEYXRlPSIyMDI1LTA5LTEwVDE3OjE5OjQ0KzAyOjAwIiBkYzpmb3JtYXQ9ImltYWdlL3BuZyIgcGhvdG9zaG9wOkNvbG9yTW9kZT0iMyIgeG1wTU06SW5zdGFuY2VJRD0ieG1wLmlpZDpiOGNlNTc4ZC1hZDM4LWUwNDctODRlYi1hMTNmNGVhNTE0NzgiIHhtcE1NOkRvY3VtZW50SUQ9ImFkb2JlOmRvY2lkOnBob3Rvc2hvcDoyM2UwZmRmMy01YThjLTFiNGItYTBkYy02NzNjNjM0OTQzYmMiIHhtcE1NOk9yaWdpbmFsRG9jdW1lbnRJRD0ieG1wLmRpZDo2ZDRlMTUyMS0yZGMwLTA3NGItYTFkNS01NWVjZmQwODE3ZmQiPiA8eG1wTU06SGlzdG9yeT4gPHJkZjpTZXE+IDxyZGY6bGkgc3RFdnQ6YWN0aW9uPSJjcmVhdGVkIiBzdEV2dDppbnN0YW5jZUlEPSJ4bXAuaWlkOjZkNGUxNTIxLTJkYzAtMDc0Yi1hMWQ1LTU1ZWNmZDA4MTdmZCIgc3RFdnQ6d2hlbj0iMjAyNS0wOS0xMFQxNzowNzowMCswMjowMCIgc3RFdnQ6c29mdHdhcmVBZ2VudD0iQWRvYmUgUGhvdG9zaG9wIDI2LjExICgyMDI1MDkwNy5tLjMyMTEgYjgzMjczNykgIChXaW5kb3dzKSIvPiA8cmRmOmxpIHN0RXZ0OmFjdGlvbj0iY29udmVydGVkIiBzdEV2dDpwYXJhbWV0ZXJzPSJmcm9tIGFwcGxpY2F0aW9uL3ZuZC5hZG9iZS5waG90b3Nob3AgdG8gaW1hZ2UvcG5nIi8+IDxyZGY6bGkgc3RFdnQ6YWN0aW9uPSJzYXZlZCIgc3RFdnQ6aW5zdGFuY2VJRD0ieG1wLmlpZDpiOGNlNTc4ZC1hZDM4LWUwNDctODRlYi1hMTNmNGVhNTE0NzgiIHN0RXZ0OndoZW49IjIwMjUtMDktMTBUMTc6MTk6NDQrMDI6MDAiIHN0RXZ0OnNvZnR3YXJlQWdlbnQ9IkFkb2JlIFBob3Rvc2hvcCAyNi4xMSAoMjAyNTA5MDcubS4zMjExIGI4MzI3MzcpICAoV2luZG93cykiIHN0RXZ0OmNoYW5nZWQ9Ii8iLz4gPC9yZGY6U2VxPiA8L3htcE1NOkhpc3Rvcnk+IDwvcmRmOkRlc2NyaXB0aW9uPiA8L3JkZjpSREY+IDwveDp4bXBtZXRhPiA8P3hwYWNrZXQgZW5kPSJyIj8+U0ghxQAAAv1JREFUeNrtmk1IVFEYhkdLMEIhCQqEkMKIok0ohFQErooWGkSC0Q8RlIsIoniKFkVYrpIMLDD6WUSUkLSJQrRV5MJaRD+L2rgwkqQygzKx22aEjJnz2fd9AzlzBs72mfc8c+577zl3UkmSpAp5pKKAKCAKiAKigCggCogCooAooEAFzPYDFAEbgcvAc+ArkPynYwR4CnQCjUBpyvIBVqaByRwdn4FWoFwz+Rrgyxye/J9jGNjwL5OvAN7nyeSnx09g62wFXMyzyU+PcaBamvwi4FueCkiAPknA/jye/PSoDQnoLgABF0ICPhSAgMFsk68sgMknwNdsArYboFNAF9AENPw1GoEJJfd2mrEHOAM89pCQTUCrAXpKeKLUchsz8OqA0VwIeKgE/gIqAgKaDWErszB350LARyXwlXBr7dA+wgaYZa4CgGUG4DVBwICSe0/gjrmVoLEADwZClhgKEEHApJJ7zrsA1wVC1hq49QHuUgN3m2cBfgfmB4K2GIq1PMCtNwhY4lmAT4RlelPJfS1wjyq5Q94F2C4EfaPk3hC4d5Tcbu8CbAqELE8vZQ23RRAwpOQe9y7AFTm6TmtyVICbPQtwVPiVTii5P4CSALfBsF8p8yzAB4KAHiV3QOCed3tiBaoMy+m0EHRYye0QuP1K7vVMsB0GAVsCIS1nC80B7rz04aaGeygTsM0QdHEgqOXOUh3grnUtVqBXCXsnLFOt2E9AUYB7QMmdyFis6S9UndQIAvqU3EcC96pbsQLLDcvpSCBkseEl6llBwEsl95J3AdYFQq523anNPASZUnJ3eV6nk8CCQNC9rjs1nyfLVZ4F+ExYpp1uO7WZ3JNK7ljGYjUU4BUh6KCSe1fg3ldye70LcF8gZKnhqOqYIGDE8wjMUoBrAiHXG7ibAtwqz3cLlgIcB4oDQQ8bdmoLA9ydru8WDAXYLyzTW0ruC4Hb7vpuwVCAbULQt0pul8DV/lmrJyMw/lEyCogCooAoIAqIAqKAKCAKiAKigEIU8Bstm0n2N0s2awAAAABJRU5ErkJggg="

def load_icon(b64):
    b64 = b64.strip()
    missing_padding = len(b64) % 4
    if missing_padding:
        b64 += "=" * (4 - missing_padding)
    return Image.open(io.BytesIO(base64.b64decode(b64)))

icons = {
    "On": load_icon(m_on_b64),
    "Off": load_icon(m_off_b64),
}

from PIL import ImageDraw  # already imported PIL.Image, so extend here

def make_gleam_frames(base_icon: Image.Image, steps=20) -> list[Image.Image]:
    """Return a sequence of icons with a pure white diagonal gleam, masked by the icon's alpha."""
    frames = []
    w, h = base_icon.size

    # Extract alpha channel from the base icon (shape of the "M")
    mask = base_icon.convert("RGBA").split()[3]

    for i in range(steps):
        # Start with transparent canvas
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay, "RGBA")

        offset = int((i / (steps - 1)) * (w + h))

        # Draw the pure white diagonal band
        draw.line(
            [(offset - h, 0), (offset, h)],
            fill=(255, 255, 255, 255),  # pure white, fully opaque
            width=14
        )

        # Apply mask so the gleam only shows where the icon is opaque
        overlay = Image.composite(overlay, Image.new("RGBA", (w, h), (0, 0, 0, 0)), mask)

        # Merge overlay onto the base icon
        frame = Image.alpha_composite(base_icon.convert("RGBA"), overlay)

        frames.append(frame)

    return frames

# --------------------------
# STATE PERSISTENCE
# --------------------------
def save_state(status):
    os.makedirs(APPDATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(status)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return f.read().strip()
    return "Off"

# --------------------------
# WINDOWS IDLE TIME
# --------------------------
def get_idle_seconds_windows():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    try:
        last_input_info = LASTINPUTINFO()
        last_input_info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last_input_info)):
            millis = ctypes.windll.kernel32.GetTickCount() - last_input_info.dwTime
            return millis / 1000.0
    except Exception:
        pass
    return 0.0

# --------------------------
# PROMPT COOLDOWNS
# --------------------------
def _load_epoch_file(path: str) -> float:
    try:
        with open(path, "r") as f:
            return float(f.read().strip())
    except Exception:
        return 0.0

def _save_epoch_file(path: str, epoch: float):
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(str(float(epoch)))
    except Exception:
        pass

def load_last_off_prompt_epoch(): return _load_epoch_file(LAST_OFF_PROMPT_FILE)
def save_last_off_prompt_epoch(epoch): _save_epoch_file(LAST_OFF_PROMPT_FILE, epoch)
def load_last_on_prompt_epoch():  return _load_epoch_file(LAST_ON_PROMPT_FILE)
def save_last_on_prompt_epoch(epoch): _save_epoch_file(LAST_ON_PROMPT_FILE, epoch)

# --------------------------
# DAY/HOUR GATING HELPERS
# --------------------------
def _weekday_key(dt=None) -> str:
    # Monday=0 .. Sunday=6 -> "mon".."sun"
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    w = (dt or datetime.now()).weekday()
    return names[w]

def is_notification_day_enabled(cfg, dt=None) -> bool:
    days = cfg.get("notify_days", {})
    return bool(days.get(_weekday_key(dt), True))

def is_in_hour_window(start_hour: int, end_hour: int, now_hour: int) -> bool:
    """
    Window is [start, end) in 24h. end=24 means until midnight.
    Supports wrap-around, e.g., 22..6  => 22,23,0..5 are allowed.
    """
    s = max(0, min(23, int(start_hour)))
    e = max(1, min(24, int(end_hour)))
    h = max(0, min(23, int(now_hour)))

    if s == e:
        # Treat equal as full-day allowed to avoid accidental total block
        return True

    if s < e:
        return s <= h < e
    else:
        # wrap across midnight
        return h >= s or h < e

def is_before_cutoff_local(cutoff_hour):
    """
    "Only before 1–24" semantics.
    24 = always allowed (entire day).
    """
    try:
        cutoff = int(cutoff_hour)
    except Exception:
        cutoff = 24
    if cutoff >= 24:
        return True
    if cutoff < 1:
        cutoff = 1
    return datetime.now().hour < cutoff

def is_after_cutoff_local(after_hour):
    """
    "Only after 0–23" semantics.
    0 = always allowed.
    """
    try:
        a = int(after_hour)
    except Exception:
        a = 0
    a = max(0, min(23, a))
    return datetime.now().hour >= a


# --------------------------
# PHONE NORMALIZATION + SMS SENDER
# --------------------------
def _normalize_phone_e164(raw: str) -> str:
    """Return +E.164 string or empty if invalid."""
    try:
        import phonenumbers
        num = phonenumbers.parse(str(raw), None)  # requires +country code
        if phonenumbers.is_valid_number(num):
            return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        pass
    try:
        import re
        s = re.sub(r"[^\d+]", "", str(raw))
        if not s.startswith("+"):
            return ""
        digits = re.sub(r"\D", "", s[1:])
        if 8 <= len(digits) <= 15:
            return "+" + digits
    except Exception:
        pass
    return ""

def _send_sms_twilio(e164: str, body: str) -> bool:
    """Send SMS using Twilio API. Requires env vars:
       TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_E164"""
    try:
        import os
        from twilio.rest import Client

        account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
        auth_token  = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        from_number = os.environ.get("TWILIO_FROM_E164", "").strip()

        if not (account_sid and auth_token and from_number):
            print("Twilio not configured: set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_E164")
            return False

        client = Client(account_sid, auth_token)
        msg = client.messages.create(
            body=body,
            from_=from_number,
            to=e164
        )
        return bool(getattr(msg, "sid", None))
    except Exception as e:
        print("Twilio send failed:", e)
        return False

# --------------------------
# BACKGROUND MONITOR
# --------------------------
_current_status = None
_active_streak_seconds = 0.0

def monitor_loop(icon: Icon):
    global _current_status, _active_streak_seconds

    while True:
        try:
            cfg = get_settings()
            status = _current_status
            idle_s = get_idle_seconds_windows()
            now = time.time()
            now_hour = datetime.now().hour

            # Global day gating: if today's unchecked, skip all notifications
            if not is_notification_day_enabled(cfg):
                time.sleep(POLL_INTERVAL_SEC)
                continue

            # ON: idle reminder (clock-out nudge)
            if status == "On" and cfg.get("enable_clock_out_idle_reminder", True):
                on_idle_threshold_sec = int(cfg.get("on_idle_threshold_min", 45)) * 60
                on_cooldown_sec = int(cfg.get("on_idle_prompt_cooldown_min", 210)) * 60
                on_after_hour = int(cfg.get("on_idle_after_hour", 0))
                max_per_day_on = max(0, int(cfg.get("max_clock_out_per_day", 3)))

                if idle_s >= on_idle_threshold_sec and is_after_cutoff_local(on_after_hour):
                    if get_count("on") < max_per_day_on:
                        last_on = load_last_on_prompt_epoch()
                        if now - last_on >= on_cooldown_sec:
                            icon.notify("Still working?", "Moffett Clocker Helper")
                            save_last_on_prompt_epoch(now)
                            inc_count("on")

            # OFF: active reminder (clock-in nudge)
            elif status == "Off" and cfg.get("enable_clock_in_reminder", True):
                active_idle_cutoff_sec = int(cfg.get("active_idle_cutoff_sec", 300))
                if idle_s < active_idle_cutoff_sec:
                    _active_streak_seconds += POLL_INTERVAL_SEC
                else:
                    _active_streak_seconds = 0.0

                active_threshold_off_sec = int(cfg.get("active_threshold_off_min", 30)) * 60
                off_cooldown_sec = int(cfg.get("off_prompt_cooldown_min", 210)) * 60
                cutoff_hour = int(cfg.get("clock_in_cutoff_hour", 15))
                max_per_day_off = max(0, int(cfg.get("max_clock_in_per_day", 3)))

                if (_active_streak_seconds >= active_threshold_off_sec) and is_before_cutoff_local(cutoff_hour):
                    if get_count("off") < max_per_day_off:
                        last_off = load_last_off_prompt_epoch()
                        if now - last_off >= off_cooldown_sec:
                            icon.notify("Forgot to clock in?", "Moffett Clocker Helper")
                            save_last_off_prompt_epoch(now)
                            inc_count("off")

            # SMS: ON state independent of desktop reminder
            if status == "On" and cfg.get("enable_sms_clock_out_reminder", False):
                sms_idle_threshold_sec = int(cfg.get("sms_idle_threshold_min", 60)) * 60
                sms_daily_limit = max(0, int(cfg.get("sms_max_per_day", 1)))
                sms_monthly_limit = max(0, int(cfg.get("sms_max_per_month", 10)))
                phone = str(cfg.get("sms_phone_e164", "")).strip()

                # Hour window check
                s = int(cfg.get("sms_window_start_hour", 12))
                e = int(cfg.get("sms_window_end_hour", 24))
                in_window = is_in_hour_window(s, e, now_hour)

                if phone and in_window and (idle_s >= sms_idle_threshold_sec):
                    if get_count("sms") < sms_daily_limit and get_month_sms_count() < sms_monthly_limit:
                        msg = "Still working? You are idle while clocked in. Open HealthBox to clock out: " + BASE_URL
                        if _send_sms_twilio(phone, msg):
                            inc_count("sms")
                            inc_month_sms_count()
                            try:
                                icon.notify("SMS clock-out reminder sent", "Moffett Clocker Helper")
                            except Exception:
                                pass

        except Exception:
            # keep tray app alive on any unexpected error
            pass

        time.sleep(POLL_INTERVAL_SEC)

def gleam_loop(icon: Icon):
    base_off = icons["Off"]
    frames = make_gleam_frames(base_off, steps=20)
    while True:
        try:
            if _current_status == "Off":
                for f in frames:
                    icon.icon = f
                    time.sleep(0.04)
                icon.icon = base_off
                time.sleep(60)       # wait 1 min before next gleam
            else:
                time.sleep(10)
        except Exception:
            pass

# --------------------------
# ACTIONS
# --------------------------
def set_status(icon, status, open_browser=False):
    global _current_status, _active_streak_seconds
    icon.icon = icons[status]
    icon.title = "Moffett Clocker Helper"
    save_state(status)
    _current_status = status
    _active_streak_seconds = 0.0
    if open_browser:
        webbrowser.open(BASE_URL)

def turn_on(icon, item): set_status(icon, "On", open_browser=True)
def turn_off(icon, item): set_status(icon, "Off", open_browser=True)

def quit_app(icon, item):
    try:
        icon.visible = False
    except Exception:
        pass
    icon.stop()

def show_info(icon, item):
    webbrowser.open("http://russell-digital.be/moffett/clockhelper.html")

# --------------------------
# CONFIG WINDOW (Tk) — ultra simple
# --------------------------
def open_config(icon, item):
    # Only one window at a time
    global _config_window_open
    if _config_window_open:
        return

    def _run():
        import tkinter as tk
        from tkinter import ttk, messagebox

        global _config_window_open
        _config_window_open = True

        cfg = load_settings()

        root = tk.Tk()
        root.title("Moffett Clocker — Settings")
        try:
            pil_icon = icons["On"].copy().resize((32, 32), Image.LANCZOS)
            tk_icon = ImageTk.PhotoImage(pil_icon)
            root.iconphoto(True, tk_icon)
        except Exception:
            tk_icon = None
        root.resizable(False, False)

        frm = ttk.Frame(root, padding=14)
        frm.grid(sticky="nsew")
        frm.columnconfigure(0, weight=1, minsize=360)
        frm.columnconfigure(1, weight=0)

        # Helpers
        def add_entry_row(row, label_text, initial_text, width=8):
            ttk.Label(frm, text=label_text).grid(column=0, row=row, sticky="w", padx=(0,10), pady=2)
            e = ttk.Entry(frm, width=width, justify="center")
            e.insert(0, str(initial_text))
            e.grid(column=1, row=row, sticky="e")
            return e

        def add_check_row(row, label_text, initial_bool):
            var = tk.BooleanVar(value=bool(initial_bool))
            ttk.Checkbutton(frm, text=label_text, variable=var).grid(column=0, row=row, columnspan=2, sticky="w")
            return var

        def _to_int(val, default):
            try:
                return int(str(val).strip())
            except Exception:
                return default

        def _clamp(v, lo, hi):
            try:
                v = int(v)
            except Exception:
                return lo
            return max(lo, min(hi, v))

        r = 0

        # ---------------- Notification days ----------------
        ttk.Label(frm, text="Notification days", font=("Segoe UI", 10, "bold")).grid(column=0, row=r, columnspan=2, sticky="w"); r += 1
        days_cfg = cfg.get("notify_days", {})
        defaults = {"mon": True,"tue": True,"wed": True,"thu": True,"fri": True,"sat": False,"sun": False}
        days_cfg = {**defaults, **(days_cfg or {})}

        day_vars = {}
        day_labels = [("Mon","mon"),("Tue","tue"),("Wed","wed"),("Thu","thu"),("Fri","fri"),("Sat","sat"),("Sun","sun")]
        day_frame = ttk.Frame(frm); day_frame.grid(column=0, row=r, columnspan=2, sticky="w", pady=(0,6)); r += 1
        for i,(label,key) in enumerate(day_labels):
            var = tk.BooleanVar(value=bool(days_cfg.get(key, True)))
            ttk.Checkbutton(day_frame, text=label, variable=var).grid(column=i, row=0, padx=(0,8), sticky="w")
            day_vars[key] = var

        ttk.Separator(frm).grid(column=0, row=r, columnspan=2, sticky="ew", pady=8); r += 1

        # ---------------- Clock-in reminder ----------------
        ttk.Label(frm, text="Clock-in reminder", font=("Segoe UI", 10, "bold")).grid(column=0, row=r, columnspan=2, sticky="w", pady=(0,2)); r+=1
        v_enable_ci   = add_check_row(r, "Remind me to clock in", cfg.get("enable_clock_in_reminder", True)); r+=1
        e_cutoff      = add_entry_row(r, "Only show this before (1–24):", cfg.get("clock_in_cutoff_hour", 15)); r+=1
        e_active_min  = add_entry_row(r, "Active before reminder (minutes):", cfg.get("active_threshold_off_min", 30)); r+=1
        e_ci_cool     = add_entry_row(r, "Min gap between reminders (minutes):", cfg.get("off_prompt_cooldown_min", 210)); r+=1
        e_ci_max_day  = add_entry_row(r, "Max reminders per day:", cfg.get("max_clock_in_per_day", 3)); r+=1

        # ---------------- Clock-out reminder ----------------
        ttk.Label(frm, text="Clock-out reminder", font=("Segoe UI", 10, "bold")).grid(column=0, row=r, columnspan=2, sticky="w", pady=(8,2)); r+=1
        v_enable_co   = add_check_row(r, "Remind me if I go idle", cfg.get("enable_clock_out_idle_reminder", True)); r+=1
        e_after_hour  = add_entry_row(r, "Only show this after (0–23):", cfg.get("on_idle_after_hour", 0)); r+=1
        e_idle_min    = add_entry_row(r, "Idle before reminder (minutes):", cfg.get("on_idle_threshold_min", 45)); r+=1
        e_co_cool     = add_entry_row(r, "Min gap between reminders (minutes):", cfg.get("on_idle_prompt_cooldown_min", 210)); r+=1
        e_co_max_day  = add_entry_row(r, "Max reminders per day:", cfg.get("max_clock_out_per_day", 3)); r+=1

        # ---------------- SMS clock-out ----------------
        ttk.Label(frm, text="SMS clock-out reminder", font=("Segoe UI", 10, "bold")).grid(column=0, row=r, columnspan=2, sticky="w", pady=(8,2)); r+=1
        v_enable_sms  = add_check_row(r, "Send SMS if I go idle", cfg.get("enable_sms_clock_out_reminder", False)); r+=1

        ttk.Label(frm, text="Phone number (+country code):").grid(column=0, row=r, sticky="w", padx=(0,10), pady=2)
        e_sms_phone = ttk.Entry(frm, width=20, justify="center")
        e_sms_phone.insert(0, str(cfg.get("sms_phone_e164", "")))
        e_sms_phone.grid(column=1, row=r, sticky="e"); r+=1

        e_sms_start  = add_entry_row(r, "Only send between start hour (0–23):", cfg.get("sms_window_start_hour", 12)); r+=1
        e_sms_end    = add_entry_row(r, "Only send between end hour (1–24):",   cfg.get("sms_window_end_hour", 22)); r+=1
        e_sms_idle   = add_entry_row(r, "Idle before SMS reminder (minutes):",  cfg.get("sms_idle_threshold_min", 60)); r+=1

        # Budget / caps row
        month_used = get_month_sms_count()
        month_cap  = int(cfg.get("sms_max_per_month", 10))
        budget_text = f"Monthly SMS budget: {month_used} / {month_cap}"
        budget_label = ttk.Label(frm, text=budget_text)
        budget_label.grid(column=0, row=r, columnspan=2, sticky="w", pady=(4,0)); r += 1

        ttk.Label(
            frm,
            text="Max 1/day & 10/month to cap costs, and remember to buy Shane a pint."
        ).grid(column=0, row=r, columnspan=2, sticky="w", pady=(0,8)); r += 1

        ttk.Separator(frm).grid(column=0, row=r, columnspan=2, sticky="ew", pady=8); r+=1

        # Test buttons
        leftbtns = ttk.Frame(frm); leftbtns.grid(column=0, row=r, sticky="w")
        ttk.Button(leftbtns, text="Test clock-in notification",  command=lambda: icon.notify("Forgot to clock in? (test)", "Moffett Clocker Helper")).grid(column=0, row=0, padx=(0,8))
        ttk.Button(leftbtns, text="Test clock-out notification", command=lambda: icon.notify("Still working? (test)", "Moffett Clocker Helper")).grid(column=1, row=0)

        btns = ttk.Frame(frm); btns.grid(column=1, row=r, sticky="e")

        def on_save_and_close():
            # Normalize phone if enabled
            enable_sms = bool(v_enable_sms.get())
            raw_phone = e_sms_phone.get().strip()
            phone_norm = _normalize_phone_e164(raw_phone) if raw_phone else ""

            if enable_sms and not phone_norm:
                messagebox.showerror("Invalid phone", "Enter a valid phone number with +country code. Example: +358401234567")
                return

            # Hours validation
            ci_cutoff = _clamp(e_cutoff.get(), 1, 24)     # 24 = all day
            co_after  = _clamp(e_after_hour.get(), 0, 23) # 0 = always
            win_start = _clamp(e_sms_start.get(), 0, 23)
            win_end   = _clamp(e_sms_end.get(), 1, 24)

            new_cfg = {
                "enable_clock_in_reminder": bool(v_enable_ci.get()),
                "clock_in_cutoff_hour": ci_cutoff,
                "active_threshold_off_min": _to_int(e_active_min.get(),   DEFAULT_SETTINGS["active_threshold_off_min"]),
                "off_prompt_cooldown_min":  _to_int(e_ci_cool.get(),      DEFAULT_SETTINGS["off_prompt_cooldown_min"]),
                "max_clock_in_per_day":     _to_int(e_ci_max_day.get(),   DEFAULT_SETTINGS["max_clock_in_per_day"]),

                "enable_clock_out_idle_reminder": bool(v_enable_co.get()),
                "on_idle_threshold_min":    _to_int(e_idle_min.get(),     DEFAULT_SETTINGS["on_idle_threshold_min"]),
                "on_idle_prompt_cooldown_min": _to_int(e_co_cool.get(),   DEFAULT_SETTINGS["on_idle_prompt_cooldown_min"]),
                "on_idle_after_hour":       co_after,
                "max_clock_out_per_day":    _to_int(e_co_max_day.get(),   DEFAULT_SETTINGS["max_clock_out_per_day"]),

                # SMS settings
                "enable_sms_clock_out_reminder": enable_sms,
                "sms_phone_e164": phone_norm,
                "sms_only_after_hour":  DEFAULT_SETTINGS["sms_only_after_hour"],  # legacy, keep stable
                "sms_idle_threshold_min": _to_int(e_sms_idle.get(), DEFAULT_SETTINGS["sms_idle_threshold_min"]),
                "sms_max_per_day":       DEFAULT_SETTINGS["sms_max_per_day"],
                "sms_window_start_hour": win_start,
                "sms_window_end_hour":   win_end,
                "sms_max_per_month":     DEFAULT_SETTINGS["sms_max_per_month"],

                # Notification days
                "notify_days": {k: bool(v.get()) for k, v in day_vars.items()},

                # keep internal cutoff stable
                "active_idle_cutoff_sec":   DEFAULT_SETTINGS["active_idle_cutoff_sec"],
            }

            save_settings(new_cfg)
            on_close()

        ttk.Button(btns, text="Save & Close", command=on_save_and_close).grid(column=0, row=0)

        # Bottom row: Test SMS notification (uses Twilio)
        def _test_sms():
            raw = e_sms_phone.get().strip()
            e164 = _normalize_phone_e164(raw)
            if not e164:
                messagebox.showerror(
                    "Invalid phone",
                    "Enter a valid phone number with +country code. Example: +353861234567"
                )
                return

            # Use latest saved caps (not unsaved edits)
            caps = get_settings()
            daily_cap = int(caps.get("sms_max_per_day", 1))
            monthly_cap = int(caps.get("sms_max_per_month", 10))

            # Hard-gate before sending
            if get_count("sms") >= daily_cap:
                messagebox.showwarning("SMS", "Daily SMS cap reached; test blocked.")
                return
            if get_month_sms_count() >= monthly_cap:
                messagebox.showwarning("SMS", "Monthly SMS cap reached; test blocked.")
                return

            ok = _send_sms_twilio(
                e164,
                "Moffett Clocker Helper test. Still working? This is a test SMS."
            )
            if ok:
                inc_count("sms")          # counts toward daily cap
                inc_month_sms_count()     # counts toward 10/month
                messagebox.showinfo("SMS", "Test SMS sent (counted toward daily & monthly caps).")
                try:
                    month_used_now = get_month_sms_count()
                    budget_label.config(text=f"Monthly SMS budget: {month_used_now} / {monthly_cap}")
                except Exception:
                    pass
            else:
                messagebox.showwarning("SMS", "SMS failed. Check Twilio creds, trial limits, or network.")

        r += 1
        ttk.Button(frm, text="Test SMS notification (use sparingly)", command=_test_sms)\
            .grid(column=0, row=r, sticky="w")

        def on_close():
            global _config_window_open
            _config_window_open = False
            try:
                root.destroy()
            except Exception:
                pass

        root.protocol("WM_DELETE_WINDOW", on_close)
        root.mainloop()

    threading.Thread(target=_run, daemon=True).start()

# --------------------------
# RIGHT-CLICK MENU
# --------------------------
right_menu = Menu(
    MenuItem("Clock in", turn_on),
    MenuItem("Clock out", turn_off),
    Menu.SEPARATOR,
    MenuItem("Configure…", open_config),
    MenuItem("Info", show_info),
    Menu.SEPARATOR,
    MenuItem("Quit", quit_app),
)

# --------------------------
# MAIN
# --------------------------
def run_tray():
    ensure_settings_file()  # create file once if missing
    load_settings()         # prime cache
    last_status = load_state()
    global _current_status
    _current_status = last_status

    icon = Icon("Moffett", icons.get(last_status, icons["Off"]), menu=right_menu)
    icon.title = "Moffett Clocker Helper"

    threading.Thread(target=monitor_loop, args=(icon,), daemon=True).start()
    threading.Thread(target=gleam_loop, args=(icon,), daemon=True).start()
    icon.run()

if __name__ == "__main__":
    run_tray()
