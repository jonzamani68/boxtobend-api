from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
from fastapi.responses import PlainTextResponse
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path
import json
import os
import psycopg2
from urllib.parse import quote, unquote
import time
from datetime import date as dt_date
from datetime import datetime, timedelta, timezone
import secrets
import base64
import hmac
import hashlib
import requests
import smtplib
from email.message import EmailMessage
from manual_racecard_tool import import_manual_payload
from manual_results_tool import import_manual_results_payload
from rename_dog_tool import rename_or_merge_dog

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent
SPORTINGLIFE_PAYLOAD_DIR = BASE_DIR / "sportinglife_payloads"
ACCESS_POLICIES_FILE = BASE_DIR / "access_policies.json"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
VIDEOS_ROOT = Path(r"D:\greyhoundraces")


RACE_TOTALS_CACHE_TTL_SECONDS = 120
RACE_TOTALS_CACHE_MAX_ITEMS = 400
race_totals_cache = {}
COMMON_OPP_CACHE_TTL_SECONDS = 180
COMMON_OPP_CACHE_MAX_ITEMS = 500
common_opponents_cache = {}
COMMON_RACES_CACHE_TTL_SECONDS = 180
COMMON_RACES_CACHE_MAX_ITEMS = 1000
common_races_cache = {}
PERF_LOG_ALWAYS = os.getenv("PERF_LOG_ALWAYS", "0").strip().lower() in {"1", "true", "yes", "on"}
PERF_LOG_CACHE_HITS = os.getenv("PERF_LOG_CACHE_HITS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PERF_COMMON_THRESHOLD_MS = 400
PERF_COMMON_RACES_THRESHOLD_MS = 800

ACCESS_KEY_COOKIE_NAME = "box_access_key"
FULL_ACCESS_MODE = os.getenv("FULL_ACCESS_MODE", "1").strip().lower() not in {"0", "false", "no", "off"}

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
PAYPAL_API_BASE = os.getenv("PAYPAL_API_BASE", "https://api-m.paypal.com").strip().rstrip("/")

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587").strip() or "587")
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "1").strip().lower() not in {"0", "false", "no", "off"}
EMAIL_FROM = os.getenv("EMAIL_FROM", "admin@boxtobend.co.uk").strip() or "admin@boxtobend.co.uk"
SIGNUP_ALERT_BOT_TOKEN = os.getenv("SIGNUP_ALERT_BOT_TOKEN", os.getenv("MONITOR_TELEGRAM_BOT_TOKEN", "")).strip()
SIGNUP_ALERT_CHAT_ID = os.getenv("SIGNUP_ALERT_CHAT_ID", os.getenv("MONITOR_TELEGRAM_CHAT_ID", "")).strip()
FREE_TRIAL_ACCESS_KEY = os.getenv("FREE_TRIAL_ACCESS_KEY", "free_trial_towcester_2026_04_07").strip() or "free_trial_towcester_2026_04_07"
FREE_TRIAL_PLAN_CODE = os.getenv("FREE_TRIAL_PLAN_CODE", "free_trial_towcester_2026_04_07").strip() or "free_trial_towcester_2026_04_07"

_cors_origins_env = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
if _cors_origins_env:
    CORS_ALLOW_ORIGINS = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
else:
    CORS_ALLOW_ORIGINS = [
        "https://boxtobend.co.uk",
        "https://www.boxtobend.co.uk",
        "http://127.0.0.1:8010",
        "http://localhost:8010",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

BILLING_PLANS = {
    "uk_all_24h": {
        "name": "UK Full Access - 24 Hours",
        "price_pence": 500,
        "currency": "gbp",
        "duration_days": 1,
        "track_scope": "all",
        "country_scope": "GB",
    },
    "uk_1track_7d": {
        "name": "UK 1 Track - 7 Days",
        "price_pence": 1000,
        "currency": "gbp",
        "duration_days": 7,
        "track_scope": "single",
        "country_scope": "GB",
    },
    "uk_all_7d": {
        "name": "UK All Tracks - 7 Days",
        "price_pence": 1500,
        "currency": "gbp",
        "duration_days": 7,
        "track_scope": "all",
        "country_scope": "GB",
    },
    "uk_1track_28d": {
        "name": "UK 1 Track - 28 Days",
        "price_pence": 3000,
        "currency": "gbp",
        "duration_days": 28,
        "track_scope": "single",
        "country_scope": "GB",
    },
    "uk_all_28d": {
        "name": "UK All Tracks - 28 Days",
        "price_pence": 5000,
        "currency": "gbp",
        "duration_days": 28,
        "track_scope": "all",
        "country_scope": "GB",
    },
}


class BillingCheckoutRequest(BaseModel):
    provider: str
    email: str
    plan_code: str
    all_tracks: bool = True
    track: str | None = None


class FreeTrialStartRequest(BaseModel):
    email: str
    name: str | None = None


def _normalize_track_name(value) -> str:
    return str(value or "").strip().lower()


def _normalize_allowed_tracks(values):
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return None

    out = []
    seen = set()
    for value in values:
        track = str(value or "").strip()
        if not track:
            continue
        key = track.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(track)
    return out or None


def _normalize_allowed_ints(values):
    if values is None:
        return None
    if isinstance(values, (int, str)):
        values = [values]
    if not isinstance(values, list):
        return None

    out = []
    seen = set()
    for value in values:
        try:
            parsed = int(str(value).strip())
        except Exception:
            parsed = None
        if parsed is None:
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out or None


def _load_access_policies():
    raw = os.getenv("ACCESS_POLICIES_JSON", "").strip()
    if not raw and ACCESS_POLICIES_FILE.exists():
        try:
            raw = ACCESS_POLICIES_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            raw = ""

    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except Exception:
        print("Warning: ACCESS_POLICIES_JSON is not valid JSON; ignoring access policies")
        return {}

    if not isinstance(parsed, dict):
        print("Warning: ACCESS_POLICIES_JSON must be a JSON object; ignoring access policies")
        return {}

    policies = {}
    for raw_key, raw_policy in parsed.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw_policy, dict):
            continue

        allowed_tracks = _normalize_allowed_tracks(
            raw_policy.get("allowed_tracks", raw_policy.get("tracks"))
        )
        allow_dog_search = bool(raw_policy.get("allow_dog_search", raw_policy.get("dog_search", True)))
        is_admin = bool(raw_policy.get("is_admin", raw_policy.get("admin", False)))
        is_trial = bool(raw_policy.get("is_trial", False))
        allow_runner_assign = bool(raw_policy.get("allow_runner_assign", is_admin))
        allow_non_runner_edit = bool(raw_policy.get("allow_non_runner_edit", True))
        allowed_meeting_ids = _normalize_allowed_ints(
            raw_policy.get("allowed_meeting_ids", raw_policy.get("meeting_ids"))
        )

        policies[key] = {
            "allowed_tracks": allowed_tracks,
            "allowed_tracks_set": set(_normalize_track_name(t) for t in (allowed_tracks or [])),
            "allow_dog_search": allow_dog_search,
            "is_admin": is_admin,
            "is_trial": is_trial,
            "allow_runner_assign": allow_runner_assign,
            "allow_non_runner_edit": allow_non_runner_edit,
            "allowed_meeting_ids": allowed_meeting_ids,
            "allowed_meeting_ids_set": set(allowed_meeting_ids or []),
            "access_key": key,
        }

    return policies


ACCESS_POLICIES = _load_access_policies()


def _default_access_policy():
    return {
        "allowed_tracks": None,
        "allowed_tracks_set": set(),
        "allow_dog_search": True,
        "is_admin": False,
        "is_trial": False,
        "allow_runner_assign": True,
        "allow_non_runner_edit": True,
        "allowed_meeting_ids": None,
        "allowed_meeting_ids_set": set(),
        "access_key": None,
    }


def _full_access_policy(access_key: str | None = None):
    return {
        "allowed_tracks": None,
        "allowed_tracks_set": set(),
        "allow_dog_search": True,
        "is_admin": True,
        "is_trial": False,
        "allow_runner_assign": True,
        "allow_non_runner_edit": True,
        "allowed_meeting_ids": None,
        "allowed_meeting_ids_set": set(),
        "access_key": str(access_key).strip() if access_key else None,
    }


def _is_admin_policy(policy: dict) -> bool:
    return bool((policy or {}).get("is_admin", False))


def _comment_scope_key(policy: dict) -> str:
    # Admin shares one comment scope; members get private per-access-key scope.
    if _is_admin_policy(policy):
        return "__admin__"
    key = str((policy or {}).get("access_key") or "").strip()
    return key or "__admin__"


def _get_access_key_from_request(request: Request) -> str:
    if not request:
        return ""

    raw_key = request.query_params.get("access_key")
    if raw_key:
        return str(raw_key).strip()

    cookie_key = request.cookies.get(ACCESS_KEY_COOKIE_NAME)
    if cookie_key:
        return str(cookie_key).strip()

    header_key = request.headers.get("x-access-key")
    if header_key:
        return str(header_key).strip()

    return ""


def _resolve_access_policy(request: Request):
    key = _get_access_key_from_request(request)
    if key:
        paid_policy = _load_paid_access_policy(key)
        if paid_policy:
            return paid_policy

        # Reload file/env policies at request time so newly added trial keys
        # are picked up without a process restart.
        policy = _load_access_policies().get(key)
        if policy:
            return policy

        # Never elevate unknown access keys to admin/full access.
        raise HTTPException(status_code=403, detail="Invalid access key")

    if FULL_ACCESS_MODE:
        return _full_access_policy(None)

    return _default_access_policy()


def _load_paid_access_policy(access_key: str):
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COALESCE(allowed_tracks, ARRAY[]::text[]),
                COALESCE(allow_dog_search, false),
                COALESCE(allow_runner_assign, false),
                COALESCE(allow_non_runner_edit, false)
            FROM paid_access_keys
            WHERE access_key = %s
              AND status = 'active'
              AND NOW() >= starts_at
              AND NOW() < ends_at
            LIMIT 1
            """,
            (str(access_key).strip(),),
        )
        row = cur.fetchone()
        if not row:
            return None

        allowed_tracks = _normalize_allowed_tracks(row[0] or [])
        # Entitlements by plan scope:
        # - single meeting/track: tools disabled
        # - all-tracks/multi-track: tools enabled
        is_single_meeting_plan = bool(allowed_tracks and len(allowed_tracks) == 1)
        if is_single_meeting_plan:
            allow_dog_search = False
            allow_runner_assign = False
            allow_non_runner_edit = False
        else:
            allow_dog_search = True
            allow_runner_assign = True
            allow_non_runner_edit = True

        return {
            "allowed_tracks": allowed_tracks,
            "allowed_tracks_set": set(_normalize_track_name(t) for t in (allowed_tracks or [])),
            "allow_dog_search": allow_dog_search,
            "is_admin": False,
            "is_trial": False,
            "allow_runner_assign": allow_runner_assign,
            "allow_non_runner_edit": allow_non_runner_edit,
            "allowed_meeting_ids": None,
            "allowed_meeting_ids_set": set(),
            "access_key": str(access_key).strip(),
        }
    except Exception:
        return None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def _track_allowed(track: str, policy: dict) -> bool:
    allowed_tracks_set = policy.get("allowed_tracks_set") or set()
    if not allowed_tracks_set:
        return True
    return _normalize_track_name(track) in allowed_tracks_set


def _enforce_track_access(track: str, policy: dict):
    if _track_allowed(track, policy):
        return
    raise HTTPException(status_code=403, detail="Track not included in your subscription")


def _meeting_allowed(meeting_id: int, policy: dict) -> bool:
    allowed_ids = policy.get("allowed_meeting_ids_set") or set()
    if not allowed_ids:
        return True
    return int(meeting_id) in allowed_ids


def _enforce_meeting_access(meeting_id: int, policy: dict):
    if _meeting_allowed(meeting_id, policy):
        return
    raise HTTPException(status_code=403, detail="Meeting not included in your subscription")


def _race_allowed_via_entitled_dog(cur, race_id: int, policy: dict) -> bool:
    allowed_ids = list(policy.get("allowed_meeting_ids_set") or set())
    if not allowed_ids:
        return False

    cur.execute(
        """
        SELECT 1
        FROM runners ru
        WHERE ru.race_id = %s
          AND EXISTS (
              SELECT 1
              FROM runners ru2
              JOIN races r2 ON r2.id = ru2.race_id
              WHERE ru2.dog_id = ru.dog_id
                AND r2.meeting_id = ANY(%s)
          )
        LIMIT 1
        """,
        (race_id, allowed_ids),
    )
    return cur.fetchone() is not None


def _enforce_race_access(cur, race_id: int, track: str, policy: dict):
    allowed_ids = policy.get("allowed_meeting_ids_set") or set()
    if allowed_ids:
        cur.execute("SELECT meeting_id FROM races WHERE id = %s LIMIT 1", (race_id,))
        meeting_row = cur.fetchone()
        if not meeting_row or int(meeting_row[0]) not in allowed_ids:
            raise HTTPException(status_code=403, detail="Meeting not included in your subscription")

    if _track_allowed(track, policy):
        return
    if _race_allowed_via_entitled_dog(cur, race_id, policy):
        return
    raise HTTPException(status_code=403, detail="Track not included in your subscription")


def _parse_video_folder_name(folder_name: str):
    raw = str(folder_name or "").strip()
    if len(raw) < 12:
        return None, None
    meeting_date = raw[:10]
    if len(raw) == 10 or raw[10] != "_":
        return None, None
    meeting_ref = raw[11:].strip()
    if not meeting_ref:
        return None, None
    return meeting_date, meeting_ref


@app.api_route("/videos/{track_slug}/{meeting_folder}/{file_name}", methods=["GET", "HEAD"])
def serve_video_file(request: Request, track_slug: str, meeting_folder: str, file_name: str):
    policy = _resolve_access_policy(request)

    file_text = str(file_name or "").strip()
    if not file_text.lower().endswith(".mp4"):
        raise HTTPException(status_code=404, detail="Video not found")

    race_number_text = file_text[:-4]
    race_number = _to_int(race_number_text)
    if race_number is None:
        raise HTTPException(status_code=404, detail="Video not found")

    track = unquote(str(track_slug or "")).strip()
    meeting_date, meeting_ref = _parse_video_folder_name(meeting_folder)
    if not track or not meeting_date or not meeting_ref:
        raise HTTPException(status_code=404, detail="Video not found")

    video_path = (VIDEOS_ROOT / track / meeting_folder / file_text).resolve()
    try:
        video_path.relative_to(VIDEOS_ROOT.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Video not found") from exc

    conn = get_connection()
    cur = conn.cursor()
    try:
        meeting_ref_int = _to_int(meeting_ref)
        meeting_ref_country = meeting_ref.upper() if meeting_ref_int is None else None

        cur.execute(
            """
            SELECT
                r.id,
                m.track
            FROM races r
            JOIN meetings m ON m.id = r.meeting_id
            WHERE LOWER(TRIM(m.track)) = LOWER(TRIM(%s))
              AND m.meeting_date = %s
              AND r.race_number = %s
              AND (
                    (%s IS NOT NULL AND m.gbgb_meeting_id = %s)
                    OR (%s IS NOT NULL AND m.gbgb_meeting_id IS NULL AND UPPER(COALESCE(m.country, '')) = %s)
              )
            ORDER BY r.id DESC
            LIMIT 1
            """,
            (
                track,
                meeting_date,
                race_number,
                meeting_ref_int,
                meeting_ref_int,
                meeting_ref_country,
                meeting_ref_country,
            ),
        )
        race_row = cur.fetchone()

        if race_row:
            _enforce_race_access(cur, race_row[0], race_row[1], policy)
        else:
            # If the race row is not in DB, fall back to track gate only.
            _enforce_track_access(track, policy)

        if not video_path.exists() or not video_path.is_file():
            raise HTTPException(status_code=404, detail="Video not found")

        return FileResponse(path=str(video_path), media_type="video/mp4")
    finally:
        cur.close()
        conn.close()


def _apply_track_sql_filter(column_sql: str, policy: dict, params: list) -> str:
    allowed_tracks_set = policy.get("allowed_tracks_set") or set()
    if not allowed_tracks_set:
        return ""
    params.append(list(allowed_tracks_set))
    return f" AND LOWER(TRIM({column_sql})) = ANY(%s)"


def _to_int(value):
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _virtual_meeting_id(gbgb_meeting_id: int) -> int:
    return -abs(int(gbgb_meeting_id))


def _virtual_race_id(gbgb_meeting_id: int, race_number: int) -> int:
    # Keep a deterministic negative integer namespace for payload-only races.
    return -((abs(int(gbgb_meeting_id)) * 1000) + int(race_number))


def _au_rug_number(slot_no, actual_box_no):
    # Prefer official box where present, otherwise use declared slot.
    return actual_box_no if actual_box_no is not None else slot_no


def _au_rug_colour(slot_no, actual_box_no):
    rug_number = _au_rug_number(slot_no, actual_box_no)
    colour_map = {
        1: "Red",
        2: "Blue",
        3: "White",
        4: "Black",
        5: "Orange",
        6: "Green",
        7: "Black/White Stripes",
        8: "Pink",
    }
    if rug_number is None:
        return ""
    return colour_map.get(rug_number, f"Rug {rug_number}")


def _load_sportinglife_payload_index():
    meetings = {}
    races = {}

    if not SPORTINGLIFE_PAYLOAD_DIR.exists():
        return meetings, races

    for path in SPORTINGLIFE_PAYLOAD_DIR.glob("sportinglife_*_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not isinstance(payload, dict):
            continue

        gbgb_meeting_id = _to_int(payload.get("gbgb_meeting_id"))
        track = str(payload.get("track") or "").strip()
        meeting_date = str(payload.get("meeting_date") or "").strip()
        payload_races = payload.get("races") or []

        if gbgb_meeting_id is None or not track or not meeting_date or not isinstance(payload_races, list):
            continue

        meeting_id = _virtual_meeting_id(gbgb_meeting_id)
        current = meetings.get(meeting_id)
        if current is None:
            meetings[meeting_id] = {
                "id": meeting_id,
                "track": track,
                "date": meeting_date,
                "gbgb_meeting_id": gbgb_meeting_id,
                "races": [],
            }

        race_rows = []
        for race in payload_races:
            if not isinstance(race, dict):
                continue

            race_number = _to_int(race.get("race_number"))
            if race_number is None:
                continue

            race_id = _virtual_race_id(gbgb_meeting_id, race_number)
            runners = race.get("runners") or []
            if not isinstance(runners, list):
                runners = []

            runner_rows = []
            for idx, runner in enumerate(runners, start=1):
                if not isinstance(runner, dict):
                    continue
                dog_name = str(runner.get("dog") or "").strip()
                trap_num = _to_int(runner.get("trap"))
                if not dog_name:
                    continue
                if trap_num is None:
                    trap_num = idx

                sp_value = (
                    runner.get("sp")
                    or runner.get("starting_price")
                    or runner.get("startingPrice")
                    or runner.get("odds")
                    or ""
                )

                runner_rows.append(
                    {
                        "trap": trap_num,
                        "dog": dog_name,
                        "rug": str(trap_num),
                        "sp": str(sp_value or ""),
                        # Virtual dog ids keep existing UI flows working without DB writes.
                        "dog_id": -((abs(race_id) * 10) + trap_num),
                        "days": None,
                        "comment": "",
                    }
                )

            race_info = {
                "id": race_id,
                "meeting_id": meeting_id,
                "gbgb_meeting_id": gbgb_meeting_id,
                "track": track,
                "meeting_date": meeting_date,
                "number": race_number,
                "time": str(race.get("scheduled_time") or ""),
                "distance": race.get("distance"),
                "grade": str(race.get("grade") or ""),
                "going": str(race.get("going") or ""),
                "runners": sorted(runner_rows, key=lambda x: x["trap"]),
            }
            race_rows.append(race_info)
            races[race_id] = race_info

        existing_count = len(meetings[meeting_id]["races"])
        if len(race_rows) > existing_count:
            meetings[meeting_id]["races"] = sorted(race_rows, key=lambda x: (x["time"], x["number"]))

    return meetings, races


def _find_payload_race_for_id(race_id: int):
    """Resolve a race id to a Sporting Life payload race entry when possible."""
    _, payload_races = _load_sportinglife_payload_index()
    race = payload_races.get(race_id)
    if race:
        return race

    # Some UI flows use positive ids (gbgb_meeting_id*1000 + race_number).
    rid_abs = abs(int(race_id))
    gbgb_meeting_id = rid_abs // 1000
    race_number = rid_abs % 1000
    if gbgb_meeting_id <= 0 or race_number <= 0:
        return None

    virtual_id = _virtual_race_id(gbgb_meeting_id, race_number)
    return payload_races.get(virtual_id)


def _get_or_create_dog_id_by_name(cur, dog_name: str):
    name = str(dog_name or "").strip()
    if not name:
        return None

    cur.execute("SELECT id FROM dogs WHERE LOWER(name)=LOWER(%s) ORDER BY id LIMIT 1", (name,))
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute("INSERT INTO dogs (name) VALUES (%s) RETURNING id", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def _materialize_payload_race_if_missing(race_id: int) -> bool:
    """Create meeting/race/runners for a payload race id if DB race row is missing."""
    payload_race = _find_payload_race_for_id(race_id)
    if not payload_race:
        return False

    track = str(payload_race.get("track") or "").strip()
    meeting_date = str(payload_race.get("meeting_date") or "").strip()
    gbgb_meeting_id = _to_int(payload_race.get("gbgb_meeting_id"))
    race_number = _to_int(payload_race.get("number"))
    if not track or not meeting_date or gbgb_meeting_id is None or race_number is None:
        return False

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO meetings (gbgb_meeting_id, track, meeting_date, country)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (gbgb_meeting_id) DO NOTHING
            """,
            (gbgb_meeting_id, track, meeting_date, "GB"),
        )
        cur.execute("SELECT id FROM meetings WHERE gbgb_meeting_id = %s", (gbgb_meeting_id,))
        meeting_row = cur.fetchone()
        if not meeting_row:
            conn.rollback()
            return False
        meeting_id = meeting_row[0]

        cur.execute(
            """
            INSERT INTO races (id, meeting_id, race_number, race_name, distance, grade, scheduled_time, going)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
            SET
                meeting_id = EXCLUDED.meeting_id,
                race_number = EXCLUDED.race_number,
                race_name = EXCLUDED.race_name,
                distance = COALESCE(EXCLUDED.distance, races.distance),
                grade = COALESCE(EXCLUDED.grade, races.grade),
                scheduled_time = COALESCE(EXCLUDED.scheduled_time, races.scheduled_time),
                going = COALESCE(EXCLUDED.going, races.going)
            """,
            (
                race_id,
                meeting_id,
                race_number,
                f"Race {race_number}",
                _to_int(payload_race.get("distance")),
                str(payload_race.get("grade") or ""),
                str(payload_race.get("time") or "") or None,
                str(payload_race.get("going") or ""),
            ),
        )

        cur.execute("SELECT COUNT(*) FROM runners WHERE race_id = %s", (race_id,))
        existing_runner_count = cur.fetchone()[0]
        if existing_runner_count == 0:
            for runner in payload_race.get("runners", []):
                trap = _to_int((runner or {}).get("trap"))
                dog = str((runner or {}).get("dog") or "").strip()
                if trap is None or not dog:
                    continue
                dog_id = _get_or_create_dog_id_by_name(cur, dog)
                if dog_id is None:
                    continue
                cur.execute(
                    """
                    INSERT INTO runners (race_id, dog_id, trap)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (race_id, dog_id, trap),
                )

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

GRI_TRACK_CODES = {
    "Shelbourne Park": "SPK",
    "Mullingar": "MGR",
    "Limerick": "LMK",
    "Curraheen Park": "CRK",
    "Clonmel": "CML",
    "Derry": "DRY",
    "Drumbo Park": "DBP",
    "Dundalk": "DLK",
    "Enniscorthy": "ECY",
    "Galway": "GLY",
    "Kilkenny": "KKY",
    "Lifford": "LFD",
    "Newbridge": "NWB",
    "Thurles Park": "THR",
    "Tralee": "TRL",
    "Waterford": "WFD",
    "Youghal": "YGL",
}

GRI_TRACK_SLUGS = {
    "Shelbourne Park": "shelbourne-park",
    "Mullingar": "mullingar",
    "Limerick": "limerick",
    "Curraheen Park": "curraheen-park",
    "Clonmel": "clonmel",
    "Kilkenny": "kilkenny",
    "Tralee": "tralee",
    "Youghal": "youghal",
    "Galway": "galway",
    "Waterford": "waterford",
}


def build_gri_results_url(track: str, meeting_date) -> str:
    track_name = str(track or "").strip()
    track_code = GRI_TRACK_CODES.get(track_name)
    if not track_code:
        return ""

    date_text = f"{meeting_date.day}-{meeting_date.strftime('%b-%Y')}"
    return f"https://www.grireland.ie/results/view-results/?track={track_code}&date={date_text}"


def normalize_months(months) -> int:
    """Convert UI/API month filters to a safe integer.

    Supports values like 1, "1", year-to-date values such as
    "ytd" / "from_jan", and lifetime-style values such as
    "all", "lifetime", "ifetime", "0", or an empty string.
    """
    if months is None:
        return 1

    if isinstance(months, int):
        return max(months, 0)

    raw = str(months).strip().lower()
    if raw in {"ytd", "year_to_date", "year-to-date", "from_jan", "from-jan", "jan"}:
        return max(dt_date.today().month, 1)

    if raw in {"", "0", "all", "lifetime", "ifetime"}:
        return 0

    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 1


def normalize_comparison_months(months) -> int:
    raw = str(months or "").strip().lower()
    if raw in {"all", "lifetime", "full", "ytd", "year", "12"}:
        return 6

    normalized = normalize_months(months)
    if normalized <= 0:
        return 6
    return min(normalized, 6)


def _should_log_perf(total_ms: int, threshold_ms: int) -> bool:
    return PERF_LOG_ALWAYS or int(total_ms or 0) >= int(threshold_ms or 0)


# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------


def get_connection():
    return psycopg2.connect(
        host="localhost",
        port=5433,
        database="boxdb",
        user="postgres",
        password="POLand145",
    )


def get_au_connection():
    return psycopg2.connect(
        host=os.getenv("AUS_DB_HOST", "localhost"),
        port=int(os.getenv("AUS_DB_PORT", "5433")),
        database=os.getenv("AUS_DB_NAME", "aus_box_to_bend"),
        user=os.getenv("AUS_DB_USER", "postgres"),
        password=os.getenv("AUS_DB_PASSWORD", "POLand145"),
    )


def init_comments_table():
    """Create runner_comments table if it doesn't exist"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS runner_comments (
            race_id INTEGER,
            dog_id INTEGER,
            comment VARCHAR(40),
            PRIMARY KEY (race_id, dog_id)
        )
    """)
    cur.execute("ALTER TABLE runner_comments ALTER COLUMN comment TYPE VARCHAR(40)")
    conn.commit()
    cur.close()
    conn.close()


def init_dog_comments_table():
    """Create dog_comments table if it doesn't exist"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dog_comments (
            dog_id INTEGER PRIMARY KEY,
            comment VARCHAR(30)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def init_member_comments_tables():
    """Create per-member comments tables for private user notes."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS member_runner_comments (
            race_id INTEGER,
            dog_id INTEGER,
            member_key VARCHAR(120),
            comment VARCHAR(40),
            PRIMARY KEY (race_id, dog_id, member_key)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS member_dog_comments (
            dog_id INTEGER,
            member_key VARCHAR(120),
            comment VARCHAR(30),
            PRIMARY KEY (dog_id, member_key)
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def init_meeting_video_links_table():
    """Create meeting_video_links table if it doesn't exist."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_video_links (
            meeting_id INTEGER PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
            video_url TEXT
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def init_race_video_links_table():
    """Create race_video_links table if it doesn't exist."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS race_video_links (
            race_id INTEGER PRIMARY KEY REFERENCES races(id) ON DELETE CASCADE,
            video_url TEXT
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def init_billing_tables():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_orders (
            id BIGSERIAL PRIMARY KEY,
            provider VARCHAR(20) NOT NULL,
            email VARCHAR(255) NOT NULL,
            plan_code VARCHAR(64) NOT NULL,
            price_pence INTEGER NOT NULL,
            currency VARCHAR(10) NOT NULL,
            duration_days INTEGER NOT NULL,
            track_scope VARCHAR(20) NOT NULL,
            country_scope VARCHAR(8) NOT NULL,
            allowed_tracks TEXT[] NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            access_key VARCHAR(120) NOT NULL UNIQUE,
            external_order_id VARCHAR(255) NULL,
            external_customer_id VARCHAR(255) NULL,
            checkout_url TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            paid_at TIMESTAMPTZ NULL,
            starts_at TIMESTAMPTZ NULL,
            ends_at TIMESTAMPTZ NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS paid_access_keys (
            access_key VARCHAR(120) PRIMARY KEY,
            email VARCHAR(255) NOT NULL,
            provider VARCHAR(20) NOT NULL,
            plan_code VARCHAR(64) NOT NULL,
            country_scope VARCHAR(8) NOT NULL,
            allowed_tracks TEXT[] NULL,
            allow_dog_search BOOLEAN NOT NULL DEFAULT false,
            allow_runner_assign BOOLEAN NOT NULL DEFAULT false,
            allow_non_runner_edit BOOLEAN NOT NULL DEFAULT false,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            starts_at TIMESTAMPTZ NOT NULL,
            ends_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payment_ref VARCHAR(255) NULL
        )
        """
    )

    cur.execute("CREATE INDEX IF NOT EXISTS idx_paid_access_active ON paid_access_keys (status, starts_at, ends_at)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS free_trial_requests (
            id BIGSERIAL PRIMARY KEY,
            email VARCHAR(255) NOT NULL,
            name VARCHAR(255) NULL,
            access_key VARCHAR(120) NOT NULL,
            track VARCHAR(120) NOT NULL,
            meeting_date DATE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    cur.execute("CREATE INDEX IF NOT EXISTS idx_free_trial_requests_created_at ON free_trial_requests (created_at DESC)")
    conn.commit()
    cur.close()
    conn.close()


def init_schema_extensions():
    """Add new columns to runners and races tables if they don't exist"""
    conn = get_connection()
    cur = conn.cursor()
    
    # Add columns to runners table if they don't exist
    try:
        cur.execute("ALTER TABLE runners ADD COLUMN distance_beaten VARCHAR(10)")
        conn.commit()
    except:
        conn.rollback()
    
    try:
        cur.execute("ALTER TABLE runners ADD COLUMN sp VARCHAR(10)")
        conn.commit()
    except:
        conn.rollback()
    
    try:
        cur.execute("ALTER TABLE runners ADD COLUMN result_comment VARCHAR(100)")
        conn.commit()
    except:
        conn.rollback()
    
    try:
        cur.execute("ALTER TABLE runners ADD COLUMN sectional_time VARCHAR(10)")
        conn.commit()
    except:
        conn.rollback()
    
    # Add going column to races table if it doesn't exist
    try:
        cur.execute("ALTER TABLE races ADD COLUMN going VARCHAR(50)")
        conn.commit()
    except:
        conn.rollback()
    
    cur.close()
    conn.close()


def init_au_schema_extensions():
    """Add AU race_slots columns used by analysis UI if they don't exist."""
    conn = get_au_connection()
    cur = conn.cursor()

    try:
        cur.execute("ALTER TABLE race_slots ADD COLUMN rug VARCHAR(40)")
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        cur.execute(
            """
            UPDATE race_slots
            SET rug = CASE
                WHEN COALESCE(actual_box_no, slot_no) = 1 THEN 'Red'
                WHEN COALESCE(actual_box_no, slot_no) = 2 THEN 'Blue'
                WHEN COALESCE(actual_box_no, slot_no) = 3 THEN 'White'
                WHEN COALESCE(actual_box_no, slot_no) = 4 THEN 'Black'
                WHEN COALESCE(actual_box_no, slot_no) = 5 THEN 'Orange'
                WHEN COALESCE(actual_box_no, slot_no) = 6 THEN 'Green'
                WHEN COALESCE(actual_box_no, slot_no) = 7 THEN 'Black/White Stripes'
                WHEN COALESCE(actual_box_no, slot_no) = 8 THEN 'Pink'
                WHEN COALESCE(actual_box_no, slot_no) IS NULL THEN NULL
                ELSE 'Rug ' || COALESCE(actual_box_no, slot_no)::text
            END
            WHERE COALESCE(rug, '') = ''
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()

    cur.close()
    conn.close()


def init_performance_indexes():
    """Create common query indexes used by analysis totals/history endpoints."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("CREATE INDEX IF NOT EXISTS idx_runners_race_dog ON runners (race_id, dog_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_runners_dog_race ON runners (dog_id, race_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dogs_name_norm ON dogs ((LOWER(TRIM(name))))")
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runners_winner_time_race
        ON runners (race_id)
        WHERE finishing_position = 1 AND official_time IS NOT NULL
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_races_meeting_id ON races (meeting_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meetings_meeting_date ON meetings (meeting_date)")

    conn.commit()
    cur.close()
    conn.close()


def _list_uk_tracks(cur):
    cur.execute(
        """
        SELECT DISTINCT track
        FROM meetings
        WHERE UPPER(COALESCE(country, '')) IN ('GB', 'UK', 'GREAT BRITAIN')
          AND COALESCE(TRIM(track), '') <> ''
        ORDER BY track ASC
        """
    )
    tracks = [r[0] for r in cur.fetchall()]
    if tracks:
        return tracks

    # Fallback for legacy rows where country may be empty but GBGB id is present.
    cur.execute(
        """
        SELECT DISTINCT track
        FROM meetings
        WHERE gbgb_meeting_id IS NOT NULL
          AND COALESCE(NULLIF(TRIM(country), ''), 'UK') IN ('UK', 'GB', 'GREAT BRITAIN')
          AND COALESCE(TRIM(track), '') <> ''
        ORDER BY track ASC
        """
    )
    return [r[0] for r in cur.fetchall()]


def _normalized_provider(raw_provider: str) -> str:
    provider = str(raw_provider or "").strip().lower()
    if provider not in {"stripe", "paypal"}:
        raise HTTPException(status_code=400, detail="Provider must be stripe or paypal")
    return provider


def _build_checkout_payload(cur, payload: BillingCheckoutRequest):
    provider = _normalized_provider(payload.provider)
    email = str(payload.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required")

    plan_code = str(payload.plan_code or "").strip().lower()
    plan = BILLING_PLANS.get(plan_code)
    if not plan:
        raise HTTPException(status_code=400, detail="Unknown plan_code")

    uk_tracks = _list_uk_tracks(cur)
    if not uk_tracks:
        raise HTTPException(status_code=500, detail="No UK tracks found to build access policy")

    track_scope = plan["track_scope"]
    if track_scope == "all":
        allowed_tracks = uk_tracks
    else:
        selected_track = str(payload.track or "").strip()
        if not selected_track:
            raise HTTPException(status_code=400, detail="Single-track plans require a track")
        match = None
        for t in uk_tracks:
            if _normalize_track_name(t) == _normalize_track_name(selected_track):
                match = t
                break
        if not match:
            raise HTTPException(status_code=400, detail="Selected track is not a UK track")
        allowed_tracks = [match]

    access_key = secrets.token_urlsafe(24)

    return {
        "provider": provider,
        "email": email,
        "plan_code": plan_code,
        "price_pence": int(plan["price_pence"]),
        "currency": str(plan["currency"]),
        "duration_days": int(plan["duration_days"]),
        "track_scope": track_scope,
        "country_scope": str(plan["country_scope"]),
        "allowed_tracks": allowed_tracks,
        "access_key": access_key,
        "plan_name": str(plan["name"]),
    }


def _build_access_url(access_key: str) -> str:
    return f"{APP_BASE_URL}/app?access_key={quote(str(access_key or '').strip())}"


def _send_paid_access_email(to_email: str, access_url: str, plan_code: str, provider: str, ends_at):
    if not SMTP_HOST:
        return False

    recipient = str(to_email or "").strip()
    if not recipient:
        return False

    expires_text = str(ends_at) if ends_at else ""
    body = (
        "Your Box2Bend access is now active.\n\n"
        f"Provider: {provider}\n"
        f"Plan: {plan_code}\n"
        f"Access link: {access_url}\n"
        f"Expires: {expires_text}\n\n"
        "Use this link to open your paid view directly."
    )

    msg = EmailMessage()
    msg["Subject"] = "Your Box2Bend access link"
    msg["From"] = EMAIL_FROM
    msg["To"] = recipient
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        if SMTP_USE_TLS:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(msg)

    return True


def _send_free_trial_email(to_email: str, access_url: str, track: str, meeting_date: str) -> bool:
    if not SMTP_HOST:
        return False

    recipient = str(to_email or "").strip()
    if not recipient:
        return False

    body = (
        "Your Box2Bend free trial is ready.\n\n"
        f"Meeting: {track} {meeting_date}\n"
        "Access type: meeting-only trial\n"
        "Tools disabled: dog search, add/remove runner tools\n\n"
        f"Open trial: {access_url}\n"
    )

    html_body = f"""
    <html>
        <body style="margin:0;padding:0;background:#f5f7fb;font-family:Segoe UI,Tahoma,Arial,sans-serif;color:#1b2430;">
            <div style="max-width:640px;margin:24px auto;background:#ffffff;border:1px solid #dce3ef;border-radius:12px;padding:24px;">
                <h2 style="margin:0 0 12px 0;font-size:24px;color:#0f172a;">Your Box2Bend free trial is ready</h2>
                <p style="margin:0 0 14px 0;font-size:15px;line-height:1.5;">
                    Meeting: <strong>{track} {meeting_date}</strong><br/>
                    Access type: <strong>meeting-only trial</strong><br/>
                    Tools disabled: <strong>dog search, add/remove runner tools</strong>
                </p>
                <p style="margin:20px 0;">
                    <a href="{access_url}" style="display:inline-block;background:#0ea5e9;color:#ffffff;text-decoration:none;font-weight:700;padding:12px 18px;border-radius:8px;">
                        Open Free Trial
                    </a>
                </p>
                <p style="margin:0;font-size:13px;line-height:1.45;color:#4b5563;word-break:break-all;">
                    If the button does not open, copy this link into your browser:<br/>
                    <a href="{access_url}" style="color:#0ea5e9;">{access_url}</a>
                </p>
            </div>
        </body>
    </html>
    """

    msg = EmailMessage()
    msg["Subject"] = "Your Box2Bend free trial link"
    msg["From"] = EMAIL_FROM
    msg["To"] = recipient
    msg.set_content(body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        if SMTP_USE_TLS:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(msg)

    return True


def _is_free_trial_signup(plan_code: str, plan_name: str, price_pence: int) -> bool:
    if int(price_pence or 0) <= 0:
        return True
    code = str(plan_code or "").strip().lower()
    name = str(plan_name or "").strip().lower()
    return "trial" in code or "trial" in name or "free" in code or "free" in name


def _send_signup_alert(message: str) -> None:
    token = SIGNUP_ALERT_BOT_TOKEN
    chat_id = SIGNUP_ALERT_CHAT_ID
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=15)


def _notify_signup_event(
    *,
    event: str,
    order_id: int,
    email: str,
    provider: str,
    plan_code: str,
    plan_name: str,
    price_pence: int,
    track_scope: str,
    allowed_tracks,
    status: str,
    starts_at=None,
    ends_at=None,
):
    price_text = f"GBP {int(price_pence or 0) / 100:.2f}"
    tracks_text = "all"
    if isinstance(allowed_tracks, list) and allowed_tracks:
        tracks_text = ", ".join([str(x) for x in allowed_tracks])

    lines = [
        f"{event}",
        f"order_id={order_id}",
        f"email={email}",
        f"provider={provider}",
        f"plan={plan_code} ({plan_name})",
        f"price={price_text}",
        f"track_scope={track_scope}",
        f"tracks={tracks_text}",
        f"status={status}",
    ]
    if starts_at:
        lines.append(f"starts_at={starts_at}")
    if ends_at:
        lines.append(f"ends_at={ends_at}")

    _send_signup_alert("\n".join(lines))


def _create_stripe_checkout(order_id: int, checkout_payload: dict):
    if not STRIPE_SECRET_KEY:
        return {
            "checkout_url": f"{APP_BASE_URL}/api/billing/simulate/complete/{order_id}",
            "external_order_id": f"stripe_sim_{order_id}",
            "external_customer_id": "",
            "mode": "simulated",
        }

    url = "https://api.stripe.com/v1/checkout/sessions"
    success_url = f"{APP_BASE_URL}/api/billing/complete/{order_id}"
    cancel_url = f"{APP_BASE_URL}/analysis?payment=cancel"
    data = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "customer_email": checkout_payload["email"],
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": checkout_payload["currency"],
        "line_items[0][price_data][unit_amount]": str(checkout_payload["price_pence"]),
        "line_items[0][price_data][product_data][name]": checkout_payload["plan_name"],
        "metadata[order_id]": str(order_id),
        "metadata[plan_code]": checkout_payload["plan_code"],
    }
    resp = requests.post(url, data=data, auth=(STRIPE_SECRET_KEY, ""), timeout=30)
    if resp.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Stripe session failed: {resp.text[:300]}")
    payload = resp.json()
    return {
        "checkout_url": payload.get("url", ""),
        "external_order_id": payload.get("id", ""),
        "external_customer_id": payload.get("customer", ""),
        "mode": "live",
    }


def _paypal_token():
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        return ""
    token_resp = requests.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        timeout=30,
    )
    if token_resp.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"PayPal token failed: {token_resp.text[:300]}")
    token_data = token_resp.json()
    return str(token_data.get("access_token") or "")


def _create_paypal_checkout(order_id: int, checkout_payload: dict):
    token = _paypal_token()
    if not token:
        return {
            "checkout_url": f"{APP_BASE_URL}/api/billing/simulate/complete/{order_id}",
            "external_order_id": f"paypal_sim_{order_id}",
            "external_customer_id": "",
            "mode": "simulated",
        }

    success_url = f"{APP_BASE_URL}/api/billing/complete/{order_id}"
    cancel_url = f"{APP_BASE_URL}/analysis?payment=cancel"

    create_payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "custom_id": str(order_id),
                "amount": {
                    "currency_code": str(checkout_payload["currency"]).upper(),
                    "value": f"{checkout_payload['price_pence'] / 100:.2f}",
                },
                "description": checkout_payload["plan_name"],
            }
        ],
        "application_context": {
            "return_url": success_url,
            "cancel_url": cancel_url,
            "user_action": "PAY_NOW",
        },
    }

    resp = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(create_payload),
        timeout=30,
    )
    if resp.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"PayPal order failed: {resp.text[:300]}")
    payload = resp.json()

    approve_url = ""
    for link in payload.get("links", []):
        if (link or {}).get("rel") == "approve":
            approve_url = (link or {}).get("href", "")
            break

    return {
        "checkout_url": approve_url,
        "external_order_id": payload.get("id", ""),
        "external_customer_id": "",
        "mode": "live",
    }


def _capture_paypal_order(paypal_order_id: str):
    order_id = str(paypal_order_id or "").strip()
    if not order_id:
        return {"ok": False, "ref": "", "payload": {}}

    token = _paypal_token()
    if not token:
        return {"ok": False, "ref": "", "payload": {}}

    resp = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        return {"ok": False, "ref": "", "payload": {}}

    payload = resp.json() or {}
    status = str(payload.get("status") or "").strip().upper()
    if status != "COMPLETED":
        return {"ok": False, "ref": "", "payload": payload}

    capture_ref = ""
    for pu in payload.get("purchase_units", []):
        payments = (pu or {}).get("payments") or {}
        for cap in payments.get("captures", []) or []:
            cap_id = str((cap or {}).get("id") or "").strip()
            if cap_id:
                capture_ref = cap_id
                break
        if capture_ref:
            break

    return {"ok": True, "ref": capture_ref or order_id, "payload": payload}


def _activate_paid_order(cur, order_id: int, provider_ref: str = ""):
    cur.execute(
        """
        SELECT
            id,
            status,
            email,
            provider,
            plan_code,
            country_scope,
            track_scope,
            allowed_tracks,
            duration_days,
            access_key,
            price_pence
        FROM billing_orders
        WHERE id = %s
        LIMIT 1
        """,
        (order_id,),
    )
    row = cur.fetchone()
    if not row:
        return False
    if str(row[1]) == "paid":
        return True

    starts_at = datetime.now(timezone.utc)
    ends_at = starts_at + timedelta(days=int(row[8]))

    track_scope = str(row[6] or "").strip().lower()
    allowed_tracks = _normalize_allowed_tracks(row[7] or [])
    is_single_meeting_plan = track_scope == "single" or bool(allowed_tracks and len(allowed_tracks) == 1)
    allow_dog_search = not is_single_meeting_plan
    allow_runner_assign = not is_single_meeting_plan
    allow_non_runner_edit = not is_single_meeting_plan

    cur.execute(
        """
        UPDATE billing_orders
        SET
            status = 'paid',
            paid_at = NOW(),
            starts_at = %s,
            ends_at = %s,
            external_order_id = COALESCE(NULLIF(%s, ''), external_order_id)
        WHERE id = %s
        """,
        (starts_at, ends_at, str(provider_ref or "").strip(), order_id),
    )

    cur.execute(
        """
        INSERT INTO paid_access_keys (
            access_key,
            email,
            provider,
            plan_code,
            country_scope,
            allowed_tracks,
            allow_dog_search,
            allow_runner_assign,
            allow_non_runner_edit,
            starts_at,
            ends_at,
            status,
            payment_ref,
            updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, NOW())
        ON CONFLICT (access_key)
        DO UPDATE SET
            email = EXCLUDED.email,
            provider = EXCLUDED.provider,
            plan_code = EXCLUDED.plan_code,
            country_scope = EXCLUDED.country_scope,
            allowed_tracks = EXCLUDED.allowed_tracks,
            allow_dog_search = EXCLUDED.allow_dog_search,
            allow_runner_assign = EXCLUDED.allow_runner_assign,
            allow_non_runner_edit = EXCLUDED.allow_non_runner_edit,
            starts_at = EXCLUDED.starts_at,
            ends_at = EXCLUDED.ends_at,
            status = 'active',
            payment_ref = EXCLUDED.payment_ref,
            updated_at = NOW()
        """,
        (
            row[9],
            row[2],
            row[3],
            row[4],
            row[5],
            allowed_tracks,
            allow_dog_search,
            allow_runner_assign,
            allow_non_runner_edit,
            starts_at,
            ends_at,
            str(provider_ref or "").strip(),
        ),
    )

    try:
        access_url = _build_access_url(row[9])
        _send_paid_access_email(
            to_email=row[2],
            access_url=access_url,
            plan_code=str(row[4] or ""),
            provider=str(row[3] or ""),
            ends_at=ends_at,
        )
    except Exception as exc:
        print(f"Warning: could not send billing access email for order {order_id}: {exc}")

    try:
        _notify_signup_event(
            event="SIGNUP ACTIVATED" if not _is_free_trial_signup(str(row[4] or ""), str(row[4] or ""), int(row[10] or 0)) else "FREE TRIAL ACTIVATED",
            order_id=int(row[0]),
            email=str(row[2] or ""),
            provider=str(row[3] or ""),
            plan_code=str(row[4] or ""),
            plan_name=str(row[4] or ""),
            price_pence=int(row[10] or 0),
            track_scope=str(row[6] or ""),
            allowed_tracks=allowed_tracks,
            status="paid",
            starts_at=starts_at,
            ends_at=ends_at,
        )
    except Exception as exc:
        print(f"Warning: could not send signup activation alert for order {order_id}: {exc}")

    return True


def _sync_pending_stripe_orders(cur, limit: int = 30):
    if not STRIPE_SECRET_KEY:
        return 0

    cur.execute(
        """
        SELECT id, external_order_id
        FROM billing_orders
        WHERE provider = 'stripe'
          AND status = 'pending'
                    AND LEFT(COALESCE(external_order_id, ''), 3) = 'cs_'
        ORDER BY id DESC
        LIMIT %s
        """,
        (int(limit),),
    )
    pending_rows = cur.fetchall()
    if not pending_rows:
        return 0

    activated = 0
    for order_id, session_id in pending_rows:
        sid = str(session_id or "").strip()
        if not sid:
            continue
        try:
            resp = requests.get(
                f"https://api.stripe.com/v1/checkout/sessions/{sid}",
                auth=(STRIPE_SECRET_KEY, ""),
                timeout=20,
            )
            if resp.status_code >= 300:
                continue

            payload = resp.json() or {}
            checkout_status = str(payload.get("status") or "").strip().lower()
            payment_status = str(payload.get("payment_status") or "").strip().lower()
            if checkout_status == "complete" and payment_status == "paid":
                if _activate_paid_order(cur, int(order_id), provider_ref=sid):
                    activated += 1
        except Exception:
            continue

    return activated


@app.get("/api/billing/plans")
def billing_plans():
    conn = get_connection()
    cur = conn.cursor()
    try:
        uk_tracks = _list_uk_tracks(cur)
    finally:
        cur.close()
        conn.close()

    plans = []
    for code, plan in BILLING_PLANS.items():
        plans.append(
            {
                "code": code,
                "name": plan["name"],
                "price_pounds": plan["price_pence"] / 100,
                "price_pence": plan["price_pence"],
                "currency": plan["currency"],
                "duration_days": plan["duration_days"],
                "track_scope": plan["track_scope"],
                "country_scope": plan["country_scope"],
            }
        )

    return {
        "providers": ["stripe", "paypal"],
        "all_tracks_default": True,
        "country_scope": "GB",
        "plans": plans,
        "uk_tracks": uk_tracks,
    }


@app.get("/api/billing/recent")
def billing_recent_payments(request: Request, limit: int = Query(25, ge=1, le=200)):
    policy = _resolve_access_policy(request)
    if not _is_admin_policy(policy):
        raise HTTPException(status_code=403, detail="Admin access is required")

    conn = get_connection()
    cur = conn.cursor()
    try:
        # Reconcile recently pending Stripe sessions in case webhook delivery was missed.
        _sync_pending_stripe_orders(cur, limit=max(10, int(limit)))

        cur.execute(
            """
            SELECT
                bo.id,
                bo.provider,
                bo.email,
                bo.plan_code,
                bo.price_pence,
                bo.currency,
                bo.status,
                bo.external_order_id,
                bo.access_key,
                bo.created_at,
                bo.paid_at,
                bo.starts_at,
                bo.ends_at,
                COALESCE(pak.status, '') AS access_status
            FROM billing_orders bo
            LEFT JOIN paid_access_keys pak
                ON pak.access_key = bo.access_key
            ORDER BY bo.id DESC
            LIMIT %s
            """,
            (int(limit),),
        )
        rows = cur.fetchall()
        conn.commit()
    finally:
        cur.close()
        conn.close()

    items = []
    for row in rows:
        access_key = str(row[8] or "")
        items.append(
            {
                "order_id": row[0],
                "provider": row[1],
                "email": row[2],
                "plan_code": row[3],
                "price_pence": row[4],
                "currency": row[5],
                "status": row[6],
                "external_order_id": row[7],
                "access_key_preview": f"...{access_key[-8:]}" if len(access_key) > 8 else access_key,
                "created_at": str(row[9]) if row[9] else "",
                "paid_at": str(row[10]) if row[10] else "",
                "starts_at": str(row[11]) if row[11] else "",
                "ends_at": str(row[12]) if row[12] else "",
                "access_status": row[13],
            }
        )

    return {
        "success": True,
        "count": len(items),
        "items": items,
    }


@app.post("/api/billing/checkout")
def billing_checkout(payload: BillingCheckoutRequest):
    conn = get_connection()
    cur = conn.cursor()
    try:
        checkout_payload = _build_checkout_payload(cur, payload)

        cur.execute(
            """
            INSERT INTO billing_orders (
                provider,
                email,
                plan_code,
                price_pence,
                currency,
                duration_days,
                track_scope,
                country_scope,
                allowed_tracks,
                status,
                access_key
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
            RETURNING id
            """,
            (
                checkout_payload["provider"],
                checkout_payload["email"],
                checkout_payload["plan_code"],
                checkout_payload["price_pence"],
                checkout_payload["currency"],
                checkout_payload["duration_days"],
                checkout_payload["track_scope"],
                checkout_payload["country_scope"],
                checkout_payload["allowed_tracks"],
                checkout_payload["access_key"],
            ),
        )
        order_id = cur.fetchone()[0]

        if checkout_payload["provider"] == "stripe":
            created = _create_stripe_checkout(order_id, checkout_payload)
        else:
            created = _create_paypal_checkout(order_id, checkout_payload)

        cur.execute(
            """
            UPDATE billing_orders
            SET external_order_id = %s,
                external_customer_id = %s,
                checkout_url = %s
            WHERE id = %s
            """,
            (
                created.get("external_order_id", ""),
                created.get("external_customer_id", ""),
                created.get("checkout_url", ""),
                order_id,
            ),
        )
        conn.commit()

        try:
            is_free_trial = _is_free_trial_signup(
                plan_code=checkout_payload["plan_code"],
                plan_name=checkout_payload["plan_name"],
                price_pence=int(checkout_payload["price_pence"]),
            )
            _notify_signup_event(
                event="FREE TRIAL SIGNUP" if is_free_trial else "NEW SIGNUP",
                order_id=int(order_id),
                email=str(checkout_payload["email"]),
                provider=str(checkout_payload["provider"]),
                plan_code=str(checkout_payload["plan_code"]),
                plan_name=str(checkout_payload["plan_name"]),
                price_pence=int(checkout_payload["price_pence"]),
                track_scope=str(checkout_payload["track_scope"]),
                allowed_tracks=checkout_payload.get("allowed_tracks") or [],
                status="pending",
            )
        except Exception as exc:
            print(f"Warning: could not send signup alert for order {order_id}: {exc}")

        return {
            "success": True,
            "order_id": order_id,
            "provider": checkout_payload["provider"],
            "mode": created.get("mode", "simulated"),
            "checkout_url": created.get("checkout_url", ""),
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Checkout creation failed: {exc}") from exc
    finally:
        cur.close()
        conn.close()


@app.api_route("/api/billing/simulate/complete/{order_id}", methods=["GET", "POST"])
def billing_simulate_complete(order_id: int, request: Request):
    conn = get_connection()
    cur = conn.cursor()
    try:
        ok = _activate_paid_order(cur, order_id, provider_ref=f"sim_{order_id}")
        if not ok:
            raise HTTPException(status_code=404, detail="Order not found")
        conn.commit()

        cur.execute("SELECT access_key, ends_at FROM billing_orders WHERE id = %s", (order_id,))
        row = cur.fetchone()

        access_key = str(row[0]).strip() if row and row[0] else ""
        if request.method.upper() == "GET":
            if access_key:
                return RedirectResponse(url=_build_access_url(access_key), status_code=302)
            return RedirectResponse(url=f"/analysis?payment=pending&order_id={order_id}", status_code=302)

        return {
            "success": True,
            "order_id": order_id,
            "access_key": access_key,
            "expires_at": str(row[1]) if row and row[1] else "",
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Simulated completion failed: {exc}") from exc
    finally:
        cur.close()
        conn.close()


@app.get("/api/billing/complete/{order_id}")
def billing_complete_redirect(order_id: int, request: Request):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT provider, status, external_order_id, access_key
            FROM billing_orders
            WHERE id = %s
            LIMIT 1
            """,
            (order_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")

        provider = str(row[0] or "").strip().lower()
        status = str(row[1] or "").strip().lower()
        external_order_id = str(row[2] or "").strip()

        if status != "paid" and provider == "stripe" and external_order_id.startswith("cs_") and STRIPE_SECRET_KEY:
            try:
                resp = requests.get(
                    f"https://api.stripe.com/v1/checkout/sessions/{external_order_id}",
                    auth=(STRIPE_SECRET_KEY, ""),
                    timeout=20,
                )
                if resp.status_code < 300:
                    stripe_payload = resp.json() or {}
                    checkout_status = str(stripe_payload.get("status") or "").strip().lower()
                    payment_status = str(stripe_payload.get("payment_status") or "").strip().lower()
                    if checkout_status == "complete" and payment_status == "paid":
                        _activate_paid_order(cur, order_id, provider_ref=external_order_id)
                        conn.commit()
            except Exception:
                pass

        if status != "paid" and provider == "paypal":
            paypal_order_id = str(request.query_params.get("token") or "").strip() or external_order_id
            try:
                captured = _capture_paypal_order(paypal_order_id)
                if captured.get("ok"):
                    _activate_paid_order(cur, order_id, provider_ref=str(captured.get("ref") or paypal_order_id))
                    conn.commit()
            except Exception:
                pass

        cur.execute(
            "SELECT access_key FROM billing_orders WHERE id = %s AND status = 'paid' LIMIT 1",
            (order_id,),
        )
        paid_row = cur.fetchone()
        if not paid_row or not paid_row[0]:
            return RedirectResponse(url=f"/analysis?payment=pending&order_id={order_id}", status_code=302)

        access_key = str(paid_row[0]).strip()
        return RedirectResponse(url=_build_access_url(access_key), status_code=302)
    finally:
        cur.close()
        conn.close()


@app.post("/api/billing/webhook/stripe")
async def billing_webhook_stripe(request: Request):
    body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if STRIPE_WEBHOOK_SECRET and sig_header:
        try:
            parts = {}
            for item in sig_header.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    parts[k.strip()] = v.strip()
            ts = parts.get("t", "")
            sent_sig = parts.get("v1", "")
            signed_payload = f"{ts}.{body.decode('utf-8')}".encode("utf-8")
            expected_sig = hmac.new(
                STRIPE_WEBHOOK_SECRET.encode("utf-8"),
                signed_payload,
                hashlib.sha256,
            ).hexdigest()
            if not sent_sig or not hmac.compare_digest(sent_sig, expected_sig):
                raise HTTPException(status_code=400, detail="Invalid Stripe signature")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid Stripe signature: {exc}") from exc

    event = json.loads(body.decode("utf-8") or "{}")
    event_type = str(event.get("type") or "")
    if event_type != "checkout.session.completed":
        return {"received": True, "ignored": event_type or "unknown"}

    obj = (((event.get("data") or {}).get("object")) or {})
    metadata = obj.get("metadata") or {}
    order_id = _to_int(metadata.get("order_id"))
    if order_id is None:
        return {"received": True, "ignored": "no_order_id"}

    conn = get_connection()
    cur = conn.cursor()
    try:
        _activate_paid_order(cur, order_id, provider_ref=str(obj.get("id") or ""))
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return {"received": True, "activated_order_id": order_id}


@app.post("/api/billing/webhook/paypal")
async def billing_webhook_paypal(request: Request):
    body = await request.json()
    event_type = str((body or {}).get("event_type") or "")
    if event_type not in {"CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED"}:
        return {"received": True, "ignored": event_type or "unknown"}

    resource = (body or {}).get("resource") or {}
    purchase_units = resource.get("purchase_units") or []
    custom_id = ""
    if purchase_units and isinstance(purchase_units, list):
        custom_id = str((purchase_units[0] or {}).get("custom_id") or "")
    order_id = _to_int(custom_id)
    if order_id is None:
        return {"received": True, "ignored": "no_order_id"}

    conn = get_connection()
    cur = conn.cursor()
    try:
        _activate_paid_order(cur, order_id, provider_ref=str(resource.get("id") or ""))
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return {"received": True, "activated_order_id": order_id}


# Initialize table on startup
try:
    init_comments_table()
    init_dog_comments_table()
    init_member_comments_tables()
    init_meeting_video_links_table()
    init_race_video_links_table()
    init_billing_tables()
    init_schema_extensions()
    init_au_schema_extensions()
    init_performance_indexes()
except Exception as e:
    print(f"Warning: Could not initialize tables: {e}")


@app.get("/api/dogs/search")
def search_dogs(request: Request, q: str = ""):
    policy = _resolve_access_policy(request)
    if not policy.get("allow_dog_search", True):
        raise HTTPException(status_code=403, detail="Dog search is disabled for your subscription")

    conn = get_connection()
    cur = conn.cursor()

    if q.strip():
        cur.execute(
            """
            SELECT id, name
            FROM dogs
            WHERE name ILIKE %s
            ORDER BY name
            LIMIT 30
            """,
            (f"%{q.strip()}%",),
        )
    else:
        cur.execute(
            """
            SELECT id, name
            FROM dogs
            ORDER BY name
            LIMIT 30
            """
        )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return [{"id": r[0], "name": r[1]} for r in rows]


@app.get("/api/dog_comment")
def get_dog_comment(request: Request, dog_id: int):
    policy = _resolve_access_policy(request)
    member_key = _comment_scope_key(policy)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT comment FROM member_dog_comments WHERE dog_id=%s AND member_key=%s",
        (dog_id, member_key),
    )
    row = cur.fetchone()

    if not row:
        cur.execute("SELECT comment FROM dog_comments WHERE dog_id=%s", (dog_id,))
        row = cur.fetchone()

    cur.close()
    conn.close()

    return {"comment": row[0] if row else ""}


@app.post("/api/dog_comment")
def save_dog_comment(request: Request, data: dict):
    policy = _resolve_access_policy(request)
    member_key = _comment_scope_key(policy)

    dog_id = data.get("dog_id")
    comment = data.get("comment", "")[:30]

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO member_dog_comments (dog_id, member_key, comment)
        VALUES (%s, %s, %s)
        ON CONFLICT (dog_id, member_key)
        DO UPDATE SET comment = EXCLUDED.comment
        """,
        (dog_id, member_key, comment),
    )

    conn.commit()
    cur.close()
    conn.close()

    return {"success": True}


# --------------------------------------------------
# ANALYSIS PAGE
# --------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def public_home_page(request: Request):
    return templates.TemplateResponse(
        "public_home.html",
        {
            "request": request,
            "year": dt_date.today().year,
        },
    )


@app.get("/purchase", response_class=HTMLResponse)
def purchase_page(request: Request):
    return templates.TemplateResponse(
        "public_home.html",
        {
            "request": request,
            "year": dt_date.today().year,
        },
    )


@app.get("/free-trial", response_class=HTMLResponse)
def free_trial_page(request: Request):
    return templates.TemplateResponse(
        "free_trial.html",
        {
            "request": request,
            "year": dt_date.today().year,
        },
    )


@app.post("/api/free-trial/start")
async def start_free_trial(request: Request):
    email = ""
    name = ""

    content_type = str(request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        body = await request.json()
        email = str((body or {}).get("email") or "").strip().lower()
        name = str((body or {}).get("name") or "").strip()
    elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        email = str(form.get("email") or "").strip().lower()
        name = str(form.get("name") or "").strip()
    else:
        # Fallback: try JSON first, then form for unknown/empty content types.
        try:
            body = await request.json()
            email = str((body or {}).get("email") or "").strip().lower()
            name = str((body or {}).get("name") or "").strip()
        except Exception:
            try:
                form = await request.form()
                email = str(form.get("email") or "").strip().lower()
                name = str(form.get("name") or "").strip()
            except Exception:
                email = ""
                name = ""

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email is required")

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO free_trial_requests (email, name, access_key, track, meeting_date)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (email, name or None, FREE_TRIAL_ACCESS_KEY, "Towcester", "2026-04-07"),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    base_url = str(request.base_url).rstrip("/")
    access_url = f"{base_url}/app?access_key={quote(FREE_TRIAL_ACCESS_KEY)}"

    email_sent = False
    try:
        email_sent = _send_free_trial_email(
            to_email=email,
            access_url=access_url,
            track="Towcester",
            meeting_date="2026-04-07",
        )
    except Exception as exc:
        print(f"Warning: free trial email failed for {email}: {exc}")

    try:
        _notify_signup_event(
            event="FREE TRIAL SIGNUP",
            order_id=0,
            email=email,
            provider="trial",
            plan_code=FREE_TRIAL_PLAN_CODE,
            plan_name="Towcester 2026-04-07 trial",
            price_pence=0,
            track_scope="single",
            allowed_tracks=["Towcester"],
            status="active",
        )
    except Exception as exc:
        print(f"Warning: free trial Telegram alert failed for {email}: {exc}")

    return {
        "success": True,
        "email": email,
        "access_url": access_url,
        "email_sent": bool(email_sent),
    }


@app.get("/payment/form.txt", response_class=PlainTextResponse)
def payment_form_text(request: Request):
    base_url = str(request.base_url).rstrip("/")
    lines = [
        "BOX2BEND PAYMENT FORM (TEXT)",
        "",
        "Page:",
        f"- Home payment form page: {base_url}/",
        "",
        "Fields:",
        "- Email",
        "- Track access mode (all tracks or individual track)",
        "- Plan selection",
        "",
        "Payment Flow Links:",
        f"- Plans API (GET): {base_url}/api/billing/plans",
        f"- Checkout API (POST): {base_url}/api/billing/checkout",
        f"- Complete redirect: {base_url}/api/billing/complete/{{order_id}}",
        f"- Simulated complete: {base_url}/api/billing/simulate/complete/{{order_id}}",
        "",
        "Provider Webhooks:",
        f"- Stripe webhook (POST): {base_url}/api/billing/webhook/stripe",
        f"- PayPal webhook (POST): {base_url}/api/billing/webhook/paypal",
        "",
        "App Entry:",
        f"- App: {base_url}/app",
        f"- App with access key: {base_url}/app?access_key=<your_access_key>",
    ]
    return "\n".join(lines)


@app.get("/app", response_class=HTMLResponse)
def analysis_page(request: Request):
    order_id_text = str(request.query_params.get("order_id") or "").strip()
    order_id = _to_int(order_id_text)
    bound_access_key = ""
    if order_id is not None:
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT provider, status, external_order_id, access_key
                FROM billing_orders
                WHERE id = %s
                LIMIT 1
                """,
                (order_id,),
            )
            row = cur.fetchone()
            if row:
                provider = str(row[0] or "").strip().lower()
                status = str(row[1] or "").strip().lower()
                external_order_id = str(row[2] or "").strip()

                # Reconcile Stripe completion on return URL in case webhook arrives late.
                if provider == "stripe" and status != "paid" and external_order_id.startswith("cs_") and STRIPE_SECRET_KEY:
                    try:
                        resp = requests.get(
                            f"https://api.stripe.com/v1/checkout/sessions/{external_order_id}",
                            auth=(STRIPE_SECRET_KEY, ""),
                            timeout=20,
                        )
                        if resp.status_code < 300:
                            stripe_payload = resp.json() or {}
                            checkout_status = str(stripe_payload.get("status") or "").strip().lower()
                            payment_status = str(stripe_payload.get("payment_status") or "").strip().lower()
                            if checkout_status == "complete" and payment_status == "paid":
                                _activate_paid_order(cur, order_id, provider_ref=external_order_id)
                                conn.commit()
                    except Exception:
                        pass

                cur.execute(
                    "SELECT access_key FROM billing_orders WHERE id = %s AND status = 'paid' LIMIT 1",
                    (order_id,),
                )
                paid_row = cur.fetchone()
                if paid_row and paid_row[0]:
                    bound_access_key = str(paid_row[0]).strip()
        finally:
            cur.close()
            conn.close()

    policy = _resolve_access_policy(request)
    active_access_key = _get_access_key_from_request(request)
    key_preview = ""
    if active_access_key:
        key_preview = f"...{active_access_key[-6:]}" if len(active_access_key) > 6 else active_access_key

    if FULL_ACCESS_MODE:
        access_mode_label = "Local Full Access"
        if active_access_key:
            access_mode_label = "Keyed Tab Session"
    else:
        access_mode_label = "Default Access"
        if active_access_key:
            access_mode_label = "Access Key Session"

    response = templates.TemplateResponse(
        "analysis.html",
        {
            "request": request,
            "dog_search_enabled": bool(policy.get("allow_dog_search", True)),
            "can_assign_runners": bool(policy.get("allow_runner_assign", True)),
            "can_edit_non_runners": bool(policy.get("allow_non_runner_edit", True)),
            "is_trial_version": bool(policy.get("is_trial", False)),
            "access_mode_label": access_mode_label,
            "access_key_preview": key_preview,
            "db_target_label": "boxdb@localhost:5433",
        },
    )

    access_key = request.query_params.get("access_key")
    if access_key is not None:
        cleaned_key = str(access_key).strip()
        if cleaned_key:
            response.set_cookie(
                ACCESS_KEY_COOKIE_NAME,
                cleaned_key,
                httponly=True,
                samesite="lax",
            )
        else:
            response.delete_cookie(ACCESS_KEY_COOKIE_NAME, samesite="lax")
    elif bound_access_key:
        response.set_cookie(
            ACCESS_KEY_COOKIE_NAME,
            bound_access_key,
            httponly=True,
            samesite="lax",
        )

    return response


@app.get("/analysis", response_class=HTMLResponse)
def analysis_page_alias(request: Request):
    return analysis_page(request)


@app.get("/app/reset", response_class=HTMLResponse)
def analysis_page_reset(request: Request):
    response = analysis_page(request)
    response.delete_cookie(ACCESS_KEY_COOKIE_NAME, samesite="lax")
    return response


@app.get("/aus", response_class=HTMLResponse)
def analysis_au_page(request: Request):
    return templates.TemplateResponse(
        "analysis_au_front.html",
        {
            "request": request,
            "today": dt_date.today().isoformat(),
        },
    )


@app.get("/analysis-au-sandbox", response_class=HTMLResponse)
def analysis_au_sandbox_page(request: Request):
    return analysis_au_page(request)


@app.get("/aus/monitor", response_class=HTMLResponse)
def aus_monitor_page(request: Request):
    return templates.TemplateResponse(
        "aus_monitor.html",
        {
            "request": request,
        },
    )


@app.get("/aus-monitor", response_class=HTMLResponse)
def aus_monitor_page_alias(request: Request):
    return aus_monitor_page(request)


@app.get("/api/au/tracks")
def get_au_tracks():
    conn = get_au_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT t.track_name
        FROM meetings m
        JOIN tracks t ON t.id = m.track_id
        WHERE m.meeting_date <= CURRENT_DATE
        ORDER BY t.track_name
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]


@app.get("/api/au/meetings")
def get_au_meetings(track: str | None = Query(default=None), date: str | None = Query(default=None)):
    conn = get_au_connection()
    cur = conn.cursor()

    query = """
        SELECT
            m.id,
            t.track_name,
            m.meeting_date,
            COALESCE(m.external_meeting_slug, '')
        FROM meetings m
        JOIN tracks t ON t.id = m.track_id
        WHERE m.meeting_date <= CURRENT_DATE
    """
    params = []

    if track:
        query += " AND t.track_name ILIKE %s"
        params.append(track)

    if date:
        query += " AND m.meeting_date = %s"
        params.append(date)

    query += " ORDER BY m.meeting_date DESC, t.track_name ASC, m.id DESC LIMIT 200"
    cur.execute(query, tuple(params))
    rows = cur.fetchall()

    cur.close()
    conn.close()

    return [
        {
            "id": r[0],
            "track": r[1],
            "date": str(r[2]),
            "slug": r[3],
        }
        for r in rows
    ]


@app.get("/api/au/meeting/{meeting_id}/races")
def get_au_races(meeting_id: int):
    conn = get_au_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, race_number, COALESCE(race_time, ''), distance_m, COALESCE(grade, '')
        FROM races
        WHERE meeting_id = %s
        ORDER BY race_number
        """,
        (meeting_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "id": r[0],
            "number": r[1],
            "time": r[2],
            "distance": r[3],
            "grade": r[4],
        }
        for r in rows
    ]


@app.get("/api/au/results")
def get_au_results(
    date: str | None = Query(default=None),
    meeting_id: int | None = Query(default=None),
):
    conn = get_au_connection()
    cur = conn.cursor()

    query = """
        SELECT
            r.id AS race_id,
            m.id AS meeting_id,
            t.track_name,
            m.meeting_date,
            r.race_number,
            COALESCE(r.grade, '') AS grade,
            r.distance_m,
            COALESCE(w.winner, '') AS winner,
            w.time
        FROM races r
        JOIN meetings m ON m.id = r.meeting_id
        JOIN tracks t ON t.id = m.track_id
        LEFT JOIN LATERAL (
            SELECT
                STRING_AGG(d.dog_name, ' / ' ORDER BY d.dog_name) AS winner,
                ROUND(MIN(rs.run_time)::numeric, 3) AS time
            FROM race_slots rs
            JOIN dogs d ON d.id = rs.dog_id
            WHERE rs.race_id = r.id
              AND rs.finish_pos = 1
              AND rs.run_time IS NOT NULL
              AND UPPER(COALESCE(rs.slot_state, '')) NOT IN ('SCRATCHED', 'VACANT', 'RESERVE', 'WITHDRAWN', 'W', 'NON RUNNER', 'NON-RUNNER', 'NR', 'N/R')
        ) w ON TRUE
        WHERE m.meeting_date <= CURRENT_DATE
    """
    params: list = []

    if date:
        query += " AND m.meeting_date = %s"
        params.append(date)

    if meeting_id is not None:
        query += " AND m.id = %s"
        params.append(meeting_id)

    query += """
        ORDER BY
            m.meeting_date DESC,
            t.track_name ASC,
            m.id ASC,
            r.race_number ASC
        LIMIT 500
    """

    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [
        {
            "race_id": r[0],
            "meeting_id": r[1],
            "track": r[2],
            "meeting_date": str(r[3]),
            "race_number": r[4],
            "grade": r[5],
            "distance": r[6],
            "winner": r[7],
            "time": float(r[8]) if r[8] is not None else None,
        }
        for r in rows
    ]


@app.get("/api/au/race/{race_id}/info")
def get_au_race_info(race_id: int):
    conn = get_au_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            r.id,
            r.race_number,
            COALESCE(r.race_time, ''),
            r.distance_m,
            COALESCE(r.grade, ''),
            r.declared_slots,
            r.max_starters,
            r.results_finalized,
            m.id,
            m.meeting_date,
            t.track_name
        FROM races r
        JOIN meetings m ON m.id = r.meeting_id
        JOIN tracks t ON t.id = m.track_id
        WHERE r.id = %s
        """,
        (race_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return {}

    return {
        "race_id": row[0],
        "race_number": row[1],
        "time": row[2],
        "distance": row[3],
        "grade": row[4],
        "declared_slots": row[5],
        "max_starters": row[6],
        "results_finalized": bool(row[7]),
        "meeting_id": row[8],
        "date": str(row[9]),
        "track": row[10],
    }


@app.get("/api/au/race/{race_id}/slots")
def get_au_race_slots(race_id: int):
    conn = get_au_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            rs.slot_no,
            rs.actual_box_no,
            COALESCE(NULLIF(rs.rug, ''), NULL),
            rs.slot_state,
            rs.is_scratched,
            rs.is_reserve,
            d.dog_name,
            rs.finish_pos,
            rs.run_time,
            rs.margin,
            rs.split_time,
            rs.in_run
        FROM race_slots rs
        LEFT JOIN dogs d ON d.id = rs.dog_id
        WHERE rs.race_id = %s
        ORDER BY rs.slot_no
        """,
        (race_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [
        {
            "slot_no": r[0],
            "actual_box_no": r[1],
            "rug": r[2] or _au_rug_colour(r[0], r[1]),
            "slot_state": r[3],
            "is_scratched": bool(r[4]),
            "is_reserve": bool(r[5]),
            "dog_name": r[6] or "",
            "finish_pos": r[7],
            "run_time": float(r[8]) if r[8] is not None else None,
            "margin": r[9] or "",
            "split_time": float(r[10]) if r[10] is not None else None,
            "in_run": r[11] or "",
        }
        for r in rows
    ]


@app.get("/api/au/race/{race_id}/runners")
def get_au_race_runners(race_id: int):
    conn = get_au_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            rs.slot_no,
            rs.actual_box_no,
            COALESCE(NULLIF(rs.rug, ''), NULL),
            rs.slot_state,
            d.id,
            COALESCE(d.dog_name, ''),
            latest.last_run_date,
            m.meeting_date
        FROM race_slots rs
        JOIN races r ON r.id = rs.race_id
        JOIN meetings m ON m.id = r.meeting_id
        LEFT JOIN dogs d ON d.id = rs.dog_id
        LEFT JOIN LATERAL (
            SELECT GREATEST(
                (
                    SELECT MAX(cr.run_date)
                    FROM dog_career_runs cr
                    WHERE cr.dog_id = d.id
                      AND cr.run_date < m.meeting_date
                ),
                (
                    SELECT MAX(m2.meeting_date)
                    FROM race_slots rs2
                    JOIN races r2 ON r2.id = rs2.race_id
                    JOIN meetings m2 ON m2.id = r2.meeting_id
                    WHERE rs2.dog_id = d.id
                      AND m2.meeting_date < m.meeting_date
                                            AND UPPER(COALESCE(rs2.slot_state, '')) NOT IN ('SCRATCHED', 'VACANT', 'RESERVE', 'WITHDRAWN', 'W', 'NON RUNNER', 'NON-RUNNER', 'NR', 'N/R')
                                            AND COALESCE(rs2.finish_pos, 0) <> -1
                      AND (rs2.finish_pos IS NOT NULL OR rs2.run_time IS NOT NULL)
                )
            ) AS last_run_date
        ) latest ON TRUE
        WHERE rs.race_id = %s
                                        AND UPPER(COALESCE(rs.slot_state, '')) NOT IN ('SCRATCHED', 'VACANT', 'RESERVE', 'WITHDRAWN', 'W', 'NON RUNNER', 'NON-RUNNER', 'NR', 'N/R')
                                        AND COALESCE(rs.finish_pos, 0) <> -1
        ORDER BY rs.slot_no
        """,
        (race_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    out = []
    for r in rows:
        days = None
        if r[6] is not None and r[7] is not None:
            days = (r[7] - r[6]).days
        out.append(
            {
                "trap": r[0],
                "actual_box_no": r[1],
                "rug": r[2] or _au_rug_colour(r[0], r[1]),
                "slot_state": r[3],
                "dog_id": r[4],
                "dog": r[5],
                "days": days,
            }
        )
    return out


@app.get("/api/au/race/{race_id}/results_runners")
def get_au_race_results_runners(race_id: int):
    conn = get_au_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            rs.slot_no,
            rs.actual_box_no,
            COALESCE(NULLIF(rs.rug, ''), NULL),
            d.id,
            COALESCE(d.dog_name, ''),
            rs.slot_state,
            rs.finish_pos,
            rs.run_time,
            COALESCE(rs.margin, ''),
            rs.split_time,
            COALESCE(rs.in_run, ''),
            COALESCE(rs.sp_price, '')
        FROM race_slots rs
        LEFT JOIN dogs d ON d.id = rs.dog_id
        WHERE rs.race_id = %s
                    AND UPPER(COALESCE(rs.slot_state, '')) NOT IN ('SCRATCHED', 'VACANT', 'RESERVE', 'WITHDRAWN', 'W', 'NON RUNNER', 'NON-RUNNER', 'NR', 'N/R')
                    AND COALESCE(rs.finish_pos, 0) <> -1
        ORDER BY rs.slot_no
        """,
        (race_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [
        {
            "trap": r[0],
            "actual_box_no": r[1],
            "rug": r[2] or _au_rug_colour(r[0], r[1]),
            "dog_id": r[3],
            "dog": r[4],
            "state": r[5],
            "position": r[6],
            "time": float(r[7]) if r[7] is not None else None,
            "beaten": r[8],
            "split": float(r[9]) if r[9] is not None else None,
            "in_run": r[10],
            "sp": r[11],
        }
        for r in rows
    ]


@app.get("/api/au/dogs/search")
def get_au_dogs_search(request: Request, q: str = Query(default=""), limit: int = Query(default=20, ge=1, le=100)):
    policy = _resolve_access_policy(request)
    if not policy.get("allow_dog_search", True):
        raise HTTPException(status_code=403, detail="Dog search is disabled for your subscription")

    term = q.strip()
    if not term:
        return []
    conn = get_au_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, dog_name
        FROM dogs
        WHERE dog_name ILIKE %s
        ORDER BY dog_name
        LIMIT %s
        """,
        (f"%{term}%", limit),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"id": r[0], "name": r[1]} for r in rows]


@app.get("/api/au/dog/{dog_id}/history")
def get_au_dog_history(
    dog_id: int,
    months: int = Query(default=3, ge=1, le=24),
    trap: str | None = Query(default="all"),
):
    conn = get_au_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
                        r.id,
                        m.meeting_date,
                        t.track_name,
                        r.distance_m,
                        COALESCE(r.grade, ''),
                        r.race_number,
                        rs.slot_no,
                        rs.actual_box_no,
                        COALESCE(NULLIF(rs.rug, ''), NULL),
                        rs.finish_pos,
                        rs.run_time,
                        COALESCE(rs.margin, ''),
                        COALESCE(rs.sp_price, ''),
                        COALESCE(rs.in_run, ''),
                        COALESCE(w.winner_name, ''),
                        w.winner_time
                FROM race_slots rs
                JOIN races r ON r.id = rs.race_id
                JOIN meetings m ON m.id = r.meeting_id
                JOIN tracks t ON t.id = m.track_id
                LEFT JOIN LATERAL (
                        SELECT
                                COALESCE(dw.dog_name, '') AS winner_name,
                                rw.run_time AS winner_time
                        FROM race_slots rw
                        LEFT JOIN dogs dw ON dw.id = rw.dog_id
                        WHERE rw.race_id = r.id
                            AND rw.finish_pos = 1
                        ORDER BY rw.slot_no
                        LIMIT 1
                ) w ON TRUE
                WHERE rs.dog_id = %s
                    AND m.meeting_date >= (CURRENT_DATE - (%s::text || ' months')::interval)
                    AND COALESCE(rs.actual_box_no, rs.slot_no) BETWEEN 1 AND 8
                    AND UPPER(COALESCE(rs.slot_state, '')) NOT IN ('SCRATCHED', 'VACANT', 'RESERVE', 'WITHDRAWN', 'W', 'NON RUNNER', 'NON-RUNNER', 'NR', 'N/R')
                    AND COALESCE(rs.finish_pos, 0) <> -1
                ORDER BY m.meeting_date DESC, r.race_number DESC
        LIMIT 80
        """,
        (dog_id, months),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    mapped = []
    for r in rows:
        trap_num = r[7] if r[7] is not None else r[6]
        trap_text = str(trap_num) if trap_num is not None else ""
        rug_text = r[8] or _au_rug_colour(r[6], r[7])
        if trap and trap != "all" and trap_text and trap_text != str(trap):
            continue
        mapped.append(
            {
                "race_id": r[0],
                "date": str(r[1]) if r[1] is not None else "",
                "track": r[2],
                "distance": r[3],
                "grade": r[4],
                "race": f"R{r[5]}" if r[5] is not None else "",
                "winner": r[14],
                "winner_time": float(r[15]) if r[15] is not None else "",
                "trap": trap_text,
                "rug": rug_text,
                "position": r[9],
                "time": float(r[10]) if r[10] is not None else "",
                "beaten": r[11],
                "sp": r[12],
                "comment": r[13],
            }
        )
    return mapped


@app.get("/api/au/meeting/{meeting_id}/dogs")
def get_au_meeting_dogs(meeting_id: int):
    conn = get_au_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            m.meeting_date,
            t.track_name,
            r.race_number,
            rs.slot_no,
            COALESCE(NULLIF(rs.rug, ''), NULL),
            COALESCE(d.dog_name, '') AS dog_name,
            rs.slot_state,
            rs.actual_box_no
        FROM meetings m
        JOIN tracks t ON t.id = m.track_id
        JOIN races r ON r.meeting_id = m.id
        JOIN race_slots rs ON rs.race_id = r.id
        LEFT JOIN dogs d ON d.id = rs.dog_id
        WHERE m.id = %s
          AND COALESCE(d.dog_name, '') <> ''
        ORDER BY r.race_number, rs.slot_no
        """,
        (meeting_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [
        {
            "meeting_date": str(r[0]),
            "track": r[1],
            "race_number": r[2],
            "slot_no": r[3],
            "rug": r[4] or _au_rug_colour(r[3], r[7]),
            "dog_name": r[5],
            "slot_state": r[6],
            "actual_box_no": r[7],
        }
        for r in rows
    ]


@app.get("/monitor", response_class=HTMLResponse)
def monitor_page(request: Request):
    policy = _resolve_access_policy(request)
    conn = get_connection()
    cur = conn.cursor()

    params = []
    query = """
        SELECT DISTINCT track
        FROM meetings
        WHERE 1 = 1
    """
    query += _apply_track_sql_filter("track", policy, params)
    query += "\n        ORDER BY track"

    cur.execute(query, tuple(params))

    tracks = [row[0] for row in cur.fetchall()]

    cur.close()
    conn.close()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "tracks": tracks,
        },
    )


@app.get("/results")
def get_results(request: Request, track: str | None = Query(default=None), date: str | None = Query(default=None)):
    policy = _resolve_access_policy(request)
    if track:
        _enforce_track_access(track, policy)

    conn = get_connection()
    cur = conn.cursor()

    query = """
    WITH result_rows AS (
        SELECT
            r.id AS race_id,
            r.meeting_id,
            m.track,
            m.meeting_date,
            m.country,
            m.gbgb_meeting_id,
            r.race_number,
            r.grade,
            r.distance,
            w.winner,
            w.time,
            ROW_NUMBER() OVER (
                PARTITION BY m.track, m.meeting_date, m.id, r.race_number
                ORDER BY
                    CASE WHEN w.winner IS NOT NULL THEN 1 ELSE 0 END DESC,
                    CASE WHEN w.time IS NOT NULL THEN 1 ELSE 0 END DESC,
                    r.id DESC
            ) AS row_rank
        FROM races r
        JOIN meetings m ON r.meeting_id = m.id
        LEFT JOIN LATERAL (
            SELECT
                STRING_AGG(d.name, ' / ' ORDER BY d.name) AS winner,
                ROUND(MIN(ru.official_time)::numeric, 2) AS time
            FROM runners ru
            JOIN dogs d ON d.id = ru.dog_id
            WHERE ru.race_id = r.id
              AND ru.finishing_position = 1
        ) w ON TRUE
    )
    SELECT
        race_id,
        meeting_id,
        track,
        meeting_date,
        race_number,
        grade,
        distance,
        winner,
        time
    FROM result_rows
    """

    conditions = [
        "meeting_date <= (CURRENT_DATE + INTERVAL '1 day')",
        """
        NOT (
            COALESCE(country, '') = 'GB'
            AND
            gbgb_meeting_id IS NULL
            AND meeting_date <= CURRENT_DATE
            AND EXISTS (
                SELECT 1
                FROM meetings m2
                WHERE m2.track = track
                  AND m2.meeting_date = meeting_date
                  AND COALESCE(m2.country, '') = 'GB'
                  AND m2.gbgb_meeting_id IS NOT NULL
            )
        )
        """
    ]
    params = []
    track_filter = _apply_track_sql_filter("track", policy, params)
    if track_filter:
        conditions.append(track_filter.replace(" AND ", "", 1))

    if track:
        conditions.append("track ILIKE %s")
        params.append(track)

    if date:
        conditions.append("meeting_date = %s")
        params.append(date)
    else:
        # Default to year-to-date so the page starts at 1 Jan unless a specific date is requested.
        conditions.append("meeting_date >= DATE_TRUNC('year', CURRENT_DATE)::date")

    conditions.append("row_rank = 1")
    query += " WHERE " + " AND ".join(conditions)

    query += """
    ORDER BY
        meeting_date DESC,
        track ASC,
        meeting_id ASC,
        race_number ASC
    """

    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description]

    cur.close()
    conn.close()

    return [dict(zip(columns, row)) for row in rows]


@app.get("/race/{race_id}", response_class=HTMLResponse)
def race_page(request: Request, race_id: int):
    return templates.TemplateResponse(
        "race.html",
        {
            "request": request,
            "race_id": race_id,
        },
    )


@app.get("/api/race/{race_id}")
def get_race_video_info(request: Request, race_id: int):
    policy = _resolve_access_policy(request)

    conn = get_connection()
    cur = conn.cursor()

    race_exists_in_db = False
    if race_id < 0:
        cur.execute("SELECT 1 FROM races WHERE id = %s LIMIT 1", (race_id,))
        race_exists_in_db = cur.fetchone() is not None

    if race_id < 0 and not race_exists_in_db:
        cur.close()
        conn.close()
        _, payload_races = _load_sportinglife_payload_index()
        race = payload_races.get(race_id)
        if not race:
            return {}
        _enforce_track_access(race["track"], policy)
        return {
            "meeting_db_id": race["meeting_id"],
            "track": race["track"],
            "meeting_date": race["meeting_date"],
            "country": "GB",
            "gbgb_meeting_id": race["gbgb_meeting_id"],
            "race_number": race["number"],
            "distance": race["distance"],
            "grade": race["grade"],
            "meeting_video_url": "",
            "video": "",
        }

    cur.execute(
        """
        SELECT
            m.id,
            m.track,
            m.meeting_date,
            m.country,
            m.gbgb_meeting_id,
            r.race_number,
            r.distance,
            r.grade,
            COALESCE(rvl.video_url, mvl.video_url, '')
        FROM races r
        JOIN meetings m ON r.meeting_id = m.id
        LEFT JOIN meeting_video_links mvl ON mvl.meeting_id = m.id
        LEFT JOIN race_video_links rvl ON rvl.race_id = r.id
        WHERE r.id = %s
        """,
        (race_id,),
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        return {}

    _enforce_race_access(cur, race_id, row[1], policy)

    race_number = str(row[5]).zfill(2)
    meeting_ref = row[4] if row[4] is not None else row[3]
    video_path = (
        "/videos/"
        + quote(str(row[1]))
        + "/"
        + str(row[2])
        + "_"
        + str(meeting_ref)
        + "/"
        + race_number
        + ".mp4"
    )

    meeting_video_url = row[8] or ""
    looks_irish = (
        str(row[3] or "").upper() == "IRE"
        or (row[4] is None and str(row[1] or "").strip() in GRI_TRACK_CODES)
    )
    if meeting_video_url and "grireland.ie" in str(meeting_video_url).lower() and looks_irish:
        normalized = build_gri_results_url(row[1], row[2])
        if normalized:
            meeting_video_url = normalized
    if not meeting_video_url and looks_irish:
        meeting_video_url = build_gri_results_url(row[1], row[2])

    return {
        "meeting_db_id": row[0],
        "track": row[1],
        "meeting_date": str(row[2]),
        "country": row[3],
        "gbgb_meeting_id": row[4],
        "race_number": row[5],
        "distance": row[6],
        "grade": row[7],
        "meeting_video_url": meeting_video_url,
        "video": video_path,
    }


@app.get("/api/meeting/{meeting_id}/video_url")
def get_meeting_video_url(meeting_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(video_url, '') FROM meeting_video_links WHERE meeting_id = %s",
        (meeting_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return {"video_url": row[0] if row else ""}


@app.get("/api/tracks")
def get_tracks_api(request: Request):
    policy = _resolve_access_policy(request)
    conn = get_connection()
    cur = conn.cursor()

    params = []
    query = """
        SELECT DISTINCT track
        FROM meetings
        WHERE track IS NOT NULL
          AND TRIM(track) <> ''
    """
    query += _apply_track_sql_filter("track", policy, params)
    query += "\n        ORDER BY track ASC"

    cur.execute(query, tuple(params))

    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [row[0] for row in rows]


@app.get("/api/meeting-video/meetings")
def get_meeting_video_meetings(
    request: Request,
    date: str | None = Query(default=None),
    track: str | None = Query(default=None),
    meeting_type: str | None = Query(default="all"),
    limit: int = Query(default=2000, ge=1, le=10000),
):
    policy = _resolve_access_policy(request)
    conn = get_connection()
    cur = conn.cursor()

    kind = (meeting_type or "all").strip().lower()
    if kind not in {"all", "races", "trials", "mixed"}:
        kind = "all"

    query = """
    WITH meeting_stats AS (
        SELECT
            m.id,
            m.track,
            m.meeting_date,
            COUNT(r.id) AS race_count,
            SUM(CASE WHEN COALESCE(r.grade, '') ILIKE 'T%%' THEN 1 ELSE 0 END) AS trial_race_count,
            SUM(CASE WHEN COALESCE(r.grade, '') ILIKE 'T%%' THEN 0 ELSE 1 END) AS official_race_count,
            COALESCE(mvl.video_url, '') AS video_url
        FROM meetings m
        JOIN races r ON r.meeting_id = m.id
        LEFT JOIN meeting_video_links mvl ON mvl.meeting_id = m.id
        WHERE 1 = 1
    """

    params = []
    if date:
        query += " AND m.meeting_date = %s"
        params.append(date)
    if track:
        _enforce_track_access(track, policy)
        query += " AND m.track = %s"
        params.append(track)

    query += _apply_track_sql_filter("m.track", policy, params)

    query += """
        GROUP BY m.id, m.track, m.meeting_date, mvl.video_url
    )
    SELECT
        id,
        track,
        meeting_date,
        race_count,
        trial_race_count,
        official_race_count,
        CASE
            WHEN trial_race_count > 0 AND official_race_count = 0 THEN 'Trials'
            WHEN official_race_count > 0 AND trial_race_count = 0 THEN 'Races'
            ELSE 'Mixed'
        END AS meeting_type,
        video_url
    FROM meeting_stats
    WHERE 1 = 1
    """

    if kind == "trials":
        query += " AND trial_race_count > 0 AND official_race_count = 0"
    elif kind == "races":
        query += " AND official_race_count > 0 AND trial_race_count = 0"
    elif kind == "mixed":
        query += " AND official_race_count > 0 AND trial_race_count > 0"

    query += """
    ORDER BY meeting_date DESC, track ASC, id ASC
    LIMIT %s
    """
    params.append(limit)

    cur.execute(query, tuple(params))
    rows = cur.fetchall()

    cur.close()
    conn.close()

    meetings = []
    for row in rows:
        meetings.append(
            {
                "id": row[0],
                "track": row[1],
                "date": str(row[2]),
                "race_count": int(row[3] or 0),
                "trial_race_count": int(row[4] or 0),
                "official_race_count": int(row[5] or 0),
                "meeting_type": row[6],
                "video_url": row[7] or "",
            }
        )

    return meetings


@app.post("/api/meeting/{meeting_id}/video_url")
def save_meeting_video_url(request: Request, meeting_id: int, data: dict):
    policy = _resolve_access_policy(request)
    raw_url = data.get("video_url", "")
    video_url = str(raw_url or "").strip()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT track FROM meetings WHERE id = %s", (meeting_id,))
    meeting_row = cur.fetchone()
    if not meeting_row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Meeting not found")

    _enforce_track_access(meeting_row[0], policy)

    cur.execute(
        """
        INSERT INTO meeting_video_links (meeting_id, video_url)
        VALUES (%s, %s)
        ON CONFLICT (meeting_id)
        DO UPDATE SET video_url = EXCLUDED.video_url
        """,
        (meeting_id, video_url),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"success": True, "video_url": video_url}


@app.get("/api/race/{race_id}/results_runners")
def get_result_runners(request: Request, race_id: int):
    policy = _resolve_access_policy(request)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT m.track
        FROM races r
        JOIN meetings m ON m.id = r.meeting_id
        WHERE r.id = %s
        """,
        (race_id,),
    )
    track_row = cur.fetchone()
    if not track_row:
        cur.close()
        conn.close()
        return []

    _enforce_race_access(cur, race_id, track_row[0], policy)

    cur.execute(
        """
        SELECT
            ru.trap,
            d.name,
            CASE
                WHEN ru.finishing_position IS NOT NULL THEN ru.finishing_position::text
                WHEN COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
                  OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                  OR (
                      ru.official_time IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM runners rr
                          WHERE rr.race_id = ru.race_id
                            AND rr.finishing_position = 1
                            AND rr.official_time IS NOT NULL
                      )
                  )
                THEN 'NR'
                ELSE NULL
            END,
            ROUND(ru.official_time,2),
            ru.distance_beaten,
            ru.sp
        FROM runners ru
        JOIN dogs d ON ru.dog_id = d.id
        WHERE ru.race_id = %s
        ORDER BY ru.finishing_position NULLS LAST, ru.trap
        """,
        (race_id,),
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    data = []
    for r in rows:
        if str(r[2] or '').upper() == 'NR':
            continue
        data.append(
            {
                "trap": r[0],
                "dog": r[1],
                "pos": r[2],
                "time": r[3],
                "beaten": r[4],
                "sp": r[5],
            }
        )

    return data


# --------------------------------------------------
# MEETINGS LIST
# --------------------------------------------------


@app.get("/api/meetings")
def get_meetings(request: Request):
    policy = _resolve_access_policy(request)
    payload_meetings, _ = _load_sportinglife_payload_index()
    allowed_meeting_ids_set = policy.get("allowed_meeting_ids_set") or set()

    conn = get_connection()
    cur = conn.cursor()

    params = []
    query = """
        SELECT id, track, meeting_date, gbgb_meeting_id
        FROM meetings
                WHERE 1 = 1
                    AND meeting_date <= (CURRENT_DATE + INTERVAL '1 day')
                    AND EXISTS (
                        SELECT 1
                        FROM races r
                        WHERE r.meeting_id = meetings.id
                          AND NOT (
                              COALESCE(r.grade, '') ILIKE 'T%%'
                              OR (
                                  COALESCE(r.race_name, '') ~* '\\mtrials?\\M'
                                  AND COALESCE(r.race_name, '') !~* '\\mtrial\\s+stakes\\M'
                              )
                          )
                    )
    """
    query += _apply_track_sql_filter("track", policy, params)
    if allowed_meeting_ids_set:
        query += "\n                    AND id = ANY(%s)"
        params.append(list(allowed_meeting_ids_set))
    query += "\n        ORDER BY meeting_date DESC\n        LIMIT 30"

    cur.execute(query, tuple(params))

    rows = cur.fetchall()

    cur.close()
    conn.close()

    meetings = []
    db_gbgb_ids = set()
    for r in rows:
        gbgb_meeting_id = _to_int(r[3])
        if gbgb_meeting_id is not None:
            db_gbgb_ids.add(gbgb_meeting_id)
        meetings.append({"id": r[0], "track": r[1], "date": str(r[2])})

    for payload in payload_meetings.values():
        if allowed_meeting_ids_set and int(payload["id"]) not in allowed_meeting_ids_set:
            continue
        if not _track_allowed(payload["track"], policy):
            continue
        if payload["gbgb_meeting_id"] in db_gbgb_ids:
            continue
        meetings.append(
            {
                "id": payload["id"],
                "track": payload["track"],
                "date": str(payload["date"]),
            }
        )

    meetings.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("track") or "")), reverse=True)
    meetings = meetings[:30]

    return meetings


# --------------------------------------------------
# RACES IN MEETING
# --------------------------------------------------


@app.get("/api/meeting/{meeting_id}/races")
def get_races(request: Request, meeting_id: int):
    policy = _resolve_access_policy(request)
    _enforce_meeting_access(meeting_id, policy)

    if meeting_id < 0:
        payload_meetings, _ = _load_sportinglife_payload_index()
        payload = payload_meetings.get(meeting_id)
        if not payload:
            return []
        _enforce_track_access(payload["track"], policy)
        return [
            {
                "id": race["id"],
                "number": race["number"],
                "time": race["time"],
            }
            for race in payload.get("races", [])
            if not str(race.get("grade") or "").strip().upper().startswith("T")
        ]

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT track FROM meetings WHERE id = %s", (meeting_id,))
    meeting_row = cur.fetchone()
    if not meeting_row:
        cur.close()
        conn.close()
        return []
    _enforce_track_access(meeting_row[0], policy)

    cur.execute(
        """
        SELECT id, race_number, scheduled_time
        FROM races
        WHERE meeting_id=%s
                    AND NOT COALESCE(grade, '') ILIKE 'T%%'
        ORDER BY scheduled_time
        """,
        (meeting_id,),
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    races = []
    for r in rows:
        races.append({"id": r[0], "number": r[1], "time": str(r[2]) if r[2] else ""})

    return races


# --------------------------------------------------
# RACE RUNNERS
# --------------------------------------------------


@app.get("/api/race/{race_id}/info")
def get_race_info(request: Request, race_id: int):
    policy = _resolve_access_policy(request)

    conn = get_connection()
    cur = conn.cursor()

    race_exists_in_db = False
    if race_id < 0:
        cur.execute("SELECT 1 FROM races WHERE id = %s LIMIT 1", (race_id,))
        race_exists_in_db = cur.fetchone() is not None

    if race_id < 0 and not race_exists_in_db:
        cur.close()
        conn.close()
        _, payload_races = _load_sportinglife_payload_index()
        race = payload_races.get(race_id)
        if not race:
            return {}
        _enforce_track_access(race["track"], policy)
        return {
            "track": race["track"],
            "date": race["meeting_date"],
            "race_number": race["number"],
            "time": race["time"],
            "distance": race["distance"],
            "grade": race["grade"] or "",
            "going": race["going"] or "",
        }

    cur.execute(
        """
        SELECT 
            m.track,
            m.meeting_date,
            r.race_number,
            r.scheduled_time,
            r.distance,
            r.grade,
            r.going
        FROM races r
        JOIN meetings m ON r.meeting_id = m.id
        WHERE r.id=%s
        """,
        (race_id,),
    )

    row = cur.fetchone()

    if row:
        _enforce_race_access(cur, race_id, row[0], policy)
        cur.close()
        conn.close()
        return {
            "track": row[0],
            "date": str(row[1]),
            "race_number": row[2],
            "time": str(row[3]) if row[3] else "",
            "distance": row[4],
            "grade": row[5] or "",
            "going": row[6] or ""
        }

    cur.close()
    conn.close()
    
    return {}


@app.get("/api/race/{race_id}/runners")
def get_runners(request: Request, race_id: int):
    policy = _resolve_access_policy(request)
    member_key = _comment_scope_key(policy)

    conn = get_connection()
    cur = conn.cursor()

    race_exists_in_db = False
    if race_id < 0:
        cur.execute("SELECT 1 FROM races WHERE id = %s LIMIT 1", (race_id,))
        race_exists_in_db = cur.fetchone() is not None

    if race_id < 0 and not race_exists_in_db:
        cur.close()
        conn.close()
        _, payload_races = _load_sportinglife_payload_index()
        race = payload_races.get(race_id)
        if not race:
            return []
        _enforce_track_access(race["track"], policy)
        return race.get("runners", [])

    # Get current race date
    cur.execute(
        """
        SELECT m.meeting_date, m.track
        FROM races r
        JOIN meetings m ON r.meeting_id = m.id
        WHERE r.id = %s
        """,
        (race_id,),
    )
    
    race_date_row = cur.fetchone()
    race_date = race_date_row[0] if race_date_row else None
    race_track = race_date_row[1] if race_date_row else None
    if race_track is not None:
        _enforce_race_access(cur, race_id, race_track, policy)

    cur.execute(
        """
        SELECT trap, d.id, d.name, ru.rug, ru.sp
        FROM runners ru
        JOIN dogs d ON ru.dog_id=d.id
        WHERE ru.race_id=%s
          AND COALESCE(ru.result_comment, '') NOT ILIKE '%%non runner%%'
          AND COALESCE(ru.result_comment, '') !~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
        ORDER BY trap
        """,
        (race_id,),
    )

    rows = cur.fetchall()

    dog_ids = [int(r[1]) for r in rows if r[1] is not None]
    days_since_by_dog = {}
    member_dog_comments = {}
    global_dog_comments = {}
    member_runner_comments_map = {}
    global_runner_comments_map = {}

    if dog_ids and race_date:
        cur.execute(
            """
            SELECT ru.dog_id, MAX(m.meeting_date) AS last_meeting_date
            FROM runners ru
            JOIN races r ON ru.race_id = r.id
            JOIN meetings m ON r.meeting_id = m.id
            WHERE ru.dog_id = ANY(%s)
              AND m.meeting_date < %s
              AND (
                  ru.finishing_position IS NOT NULL
                  OR ru.official_time IS NOT NULL
              )
              AND COALESCE(ru.result_comment, '') NOT ILIKE '%%non runner%%'
              AND COALESCE(ru.result_comment, '') !~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
            GROUP BY ru.dog_id
            """,
            (dog_ids, race_date),
        )
        for dog_id, last_meeting_date in cur.fetchall():
            if last_meeting_date:
                days_since_by_dog[int(dog_id)] = (race_date - last_meeting_date).days

    if dog_ids:
        cur.execute(
            """
            SELECT dog_id, comment
            FROM member_dog_comments
            WHERE member_key = %s
              AND dog_id = ANY(%s)
            """,
            (member_key, dog_ids),
        )
        for dog_id, comment in cur.fetchall():
            member_dog_comments[int(dog_id)] = comment or ""

        cur.execute(
            """
            SELECT dog_id, comment
            FROM dog_comments
            WHERE dog_id = ANY(%s)
            """,
            (dog_ids,),
        )
        for dog_id, comment in cur.fetchall():
            global_dog_comments[int(dog_id)] = comment or ""

        cur.execute(
            """
            SELECT dog_id, comment
            FROM member_runner_comments
            WHERE race_id = %s
              AND member_key = %s
              AND dog_id = ANY(%s)
            """,
            (race_id, member_key, dog_ids),
        )
        for dog_id, comment in cur.fetchall():
            member_runner_comments_map[int(dog_id)] = comment or ""

        cur.execute(
            """
            SELECT dog_id, comment
            FROM runner_comments
            WHERE race_id = %s
              AND dog_id = ANY(%s)
            """,
            (race_id, dog_ids),
        )
        for dog_id, comment in cur.fetchall():
            global_runner_comments_map[int(dog_id)] = comment or ""

    runners = []
    for r in rows:
        trap = r[0]
        dog_id = int(r[1]) if r[1] is not None else None
        dog_name = r[2]
        rug = r[3]
        sp = r[4]
        rug_value = rug if rug not in (None, "") else (str(trap) if trap is not None else "")

        days_since = days_since_by_dog.get(dog_id)
        comment = member_dog_comments.get(dog_id, global_dog_comments.get(dog_id, ""))
        race_comment = member_runner_comments_map.get(
            dog_id,
            global_runner_comments_map.get(dog_id, ""),
        )

        runners.append({
            "trap": trap,
            "dog": dog_name,
            "rug": rug_value,
            "sp": sp,
            "dog_id": dog_id,
            "days": days_since,
            "comment": comment,
            "race_comment": race_comment,
        })

    cur.close()
    conn.close()

    return runners


@app.post("/api/runner_comment")
def save_runner_comment(request: Request, data: dict):
    policy = _resolve_access_policy(request)
    member_key = _comment_scope_key(policy)
    race_id = data.get("race_id")
    dog_id = data.get("dog_id")
    comment = data.get("comment", "")[:40]  # Limit to 40 chars
    
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT m.track
        FROM races r
        JOIN meetings m ON m.id = r.meeting_id
        WHERE r.id = %s
        """,
        (race_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Race not found")
    _enforce_track_access(row[0], policy)
    
    cur.execute(
        """
        INSERT INTO member_runner_comments (race_id, dog_id, member_key, comment)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (race_id, dog_id, member_key)
        DO UPDATE SET comment = EXCLUDED.comment
        """,
        (race_id, dog_id, member_key, comment),
    )
    
    conn.commit()
    cur.close()
    conn.close()
    
    return {"success": True}


@app.post("/api/race/{race_id}/runner/remove")
def remove_race_runner(request: Request, race_id: int, data: dict):
    policy = _resolve_access_policy(request)
    if not bool(policy.get("allow_non_runner_edit", True)):
        raise HTTPException(status_code=403, detail="Non-runner editing is disabled for your subscription")

    trap = data.get("trap")
    try:
        trap = int(trap)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Trap must be a valid number") from exc

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT m.track
            FROM races r
            JOIN meetings m ON m.id = r.meeting_id
            WHERE r.id = %s
            """,
            (race_id,),
        )
        race_row = cur.fetchone()
        if not race_row:
            materialized = _materialize_payload_race_if_missing(race_id)
            if materialized:
                cur.execute(
                    """
                    SELECT m.track
                    FROM races r
                    JOIN meetings m ON m.id = r.meeting_id
                    WHERE r.id = %s
                    """,
                    (race_id,),
                )
                race_row = cur.fetchone()
            if not race_row:
                raise HTTPException(status_code=404, detail=f"Race id {race_id} was not found")

        _enforce_track_access(race_row[0], policy)

        cur.execute(
            """
            SELECT ru.dog_id, d.name
            FROM runners ru
            JOIN dogs d ON d.id = ru.dog_id
            WHERE ru.race_id = %s AND ru.trap = %s
            """,
            (race_id, trap),
        )
        row = cur.fetchone()

        if not row:
            return {
                "success": False,
                "message": f"Trap {trap} is already vacant",
                "trap": trap,
            }

        dog_id, dog_name = row

        cur.execute(
            "DELETE FROM runner_comments WHERE race_id = %s AND dog_id = %s",
            (race_id, dog_id),
        )
        cur.execute(
            "DELETE FROM member_runner_comments WHERE race_id = %s AND dog_id = %s",
            (race_id, dog_id),
        )
        cur.execute(
            "DELETE FROM runners WHERE race_id = %s AND trap = %s",
            (race_id, trap),
        )

        conn.commit()
        return {
            "success": True,
            "message": f"Removed {dog_name} from trap {trap}",
            "trap": trap,
            "dog": dog_name,
        }
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to remove race runner: {exc}") from exc
    finally:
        cur.close()
        conn.close()


@app.post("/api/race/{race_id}/runner/assign")
def assign_race_runner(request: Request, race_id: int, data: dict):
    policy = _resolve_access_policy(request)
    if not bool(policy.get("allow_runner_assign", True)):
        raise HTTPException(status_code=403, detail="Only admin can add or replace dogs in traps")

    trap = data.get("trap")
    dog_id = data.get("dog_id")

    try:
        trap = int(trap)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Trap must be a valid number") from exc

    try:
        dog_id = int(dog_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="dog_id must be a valid number") from exc

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT m.track
            FROM races r
            JOIN meetings m ON m.id = r.meeting_id
            WHERE r.id = %s
            """,
            (race_id,),
        )
        race_row = cur.fetchone()
        if not race_row:
            materialized = _materialize_payload_race_if_missing(race_id)
            if materialized:
                cur.execute(
                    """
                    SELECT m.track
                    FROM races r
                    JOIN meetings m ON m.id = r.meeting_id
                    WHERE r.id = %s
                    """,
                    (race_id,),
                )
                race_row = cur.fetchone()
            if not race_row:
                raise HTTPException(status_code=404, detail=f"Race id {race_id} was not found")

        _enforce_track_access(race_row[0], policy)

        cur.execute("SELECT name FROM dogs WHERE id = %s", (dog_id,))
        dog_row = cur.fetchone()
        if not dog_row:
            raise HTTPException(status_code=404, detail="Selected dog was not found")
        dog_name = dog_row[0]

        cur.execute(
            "SELECT dog_id FROM runners WHERE race_id = %s AND trap = %s",
            (race_id, trap),
        )
        trap_row = cur.fetchone()
        existing_trap_dog_id = trap_row[0] if trap_row else None

        # Prevent duplicate entries for the same dog in one race.
        cur.execute(
            "DELETE FROM runners WHERE race_id = %s AND dog_id = %s AND trap <> %s",
            (race_id, dog_id, trap),
        )

        action = "assigned"
        if existing_trap_dog_id is None:
            cur.execute(
                "INSERT INTO runners (race_id, dog_id, trap) VALUES (%s, %s, %s)",
                (race_id, dog_id, trap),
            )
            action = "added"
        elif existing_trap_dog_id != dog_id:
            cur.execute(
                "DELETE FROM runner_comments WHERE race_id = %s AND dog_id = %s",
                (race_id, existing_trap_dog_id),
            )
            cur.execute(
                "DELETE FROM member_runner_comments WHERE race_id = %s AND dog_id = %s",
                (race_id, existing_trap_dog_id),
            )
            cur.execute(
                "UPDATE runners SET dog_id = %s WHERE race_id = %s AND trap = %s",
                (dog_id, race_id, trap),
            )
            action = "replaced"
        else:
            action = "unchanged"

        conn.commit()
        return {
            "success": True,
            "message": f"{action.title()} trap {trap} with {dog_name}",
            "action": action,
            "trap": trap,
            "dog_id": dog_id,
            "dog": dog_name,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to assign race runner: {exc}") from exc
    finally:
        cur.close()
        conn.close()


# --------------------------------------------------
# DOG HISTORY
# --------------------------------------------------


@app.get("/api/dog/{dog}/history")
def dog_history(request: Request, dog: str, months: int | str = 1):
    policy = _resolve_access_policy(request)
    member_key = _comment_scope_key(policy)
    months = normalize_months(months)

    conn = get_connection()
    cur = conn.cursor()

    query = """
        SELECT
            m.meeting_date,
            m.track,
            m.gbgb_meeting_id,
            m.country,
            r.distance,
            r.grade,
            r.race_number,
            ru.trap,
            CASE
                WHEN ru.finishing_position IS NOT NULL THEN ru.finishing_position::text
                WHEN COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
                  OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                  OR (
                      ru.official_time IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM runners rrn
                          WHERE rrn.race_id = ru.race_id
                            AND rrn.finishing_position = 1
                            AND rrn.official_time IS NOT NULL
                      )
                  )
                THEN 'NR'
                ELSE NULL
            END,
            ROUND(ru.official_time,2),
            w.winner,
            w.winning_time,
            rc.comment AS runner_comment,
            ru.distance_beaten,
            ru.sp,
            ru.result_comment,
            ru.sectional_time,
            r.going,
            r.id AS race_id,
            ru.dog_id

        FROM runners ru
        JOIN dogs d ON ru.dog_id=d.id
        JOIN races r ON ru.race_id=r.id
        JOIN meetings m ON r.meeting_id=m.id
        LEFT JOIN LATERAL (
            SELECT
                STRING_AGG(dw.name, ' / ' ORDER BY dw.name) AS winner,
                ROUND(MIN(rw.official_time)::numeric, 2) AS winning_time
            FROM runners rw
            JOIN dogs dw ON dw.id = rw.dog_id
            WHERE rw.race_id = r.id
              AND rw.finishing_position = 1
        ) w ON TRUE
        LEFT JOIN LATERAL (
            SELECT COALESCE(
                (
                    SELECT mrc.comment
                    FROM member_runner_comments mrc
                    WHERE mrc.race_id = ru.race_id
                      AND mrc.dog_id = ru.dog_id
                      AND mrc.member_key = %s
                    LIMIT 1
                ),
                (
                    SELECT rcg.comment
                    FROM runner_comments rcg
                    WHERE rcg.race_id = ru.race_id
                      AND rcg.dog_id = ru.dog_id
                    LIMIT 1
                )
            ) AS comment
        ) rc ON TRUE

        WHERE LOWER(TRIM(d.name)) = LOWER(TRIM(%s))
    """
    params = [member_key, dog]

    if months and months > 0:
        query += "\n        AND m.meeting_date >= CURRENT_DATE - (%s::int * INTERVAL '1 month')"
        params.append(months)

    allowed_tracks_set = policy.get("allowed_tracks_set") or set()
    allowed_meeting_ids_set = policy.get("allowed_meeting_ids_set") or set()
    if allowed_tracks_set or allowed_meeting_ids_set:
        access_clauses = []

        if allowed_tracks_set:
            params.append(list(allowed_tracks_set))
            access_clauses.append("LOWER(TRIM(m.track)) = ANY(%s)")

        if allowed_meeting_ids_set:
            params.append(list(allowed_meeting_ids_set))
            access_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM runners ru_ent
                    JOIN races r_ent ON r_ent.id = ru_ent.race_id
                    WHERE ru_ent.dog_id = ru.dog_id
                      AND r_ent.meeting_id = ANY(%s)
                )
                """.strip()
            )

        query += "\n        AND (" + " OR ".join(access_clauses) + ")"

    query += "\n        ORDER BY m.meeting_date DESC\n        LIMIT 50"

    cur.execute(query, tuple(params))

    rows = cur.fetchall()

    cur.close()
    conn.close()

    data = []
    for r in rows:
        if str(r[8] or "").upper() == "NR":
            continue
        data.append(
            {
                "date": str(r[0]),
                "track": r[1],
                "meeting_id": r[2],
                "country": r[3],
                "dist": r[4],
                "grade": r[5],
                "race": r[6],
                "trap": r[7],
                "pos": r[8],
                "time": r[9],
                "winner": r[10],
                "winning_time": r[11],
                "comment": r[12],
                "distance_beaten": r[13],
                "sp": r[14],
                "result_comment": r[15],
                "sectional_time": r[16],
                "going": r[17],
                "race_id": r[18],
                "dog_id": r[19],
            }
        )

    return data


# --------------------------------------------------
# A v B COUNT
# --------------------------------------------------


@app.get("/api/compare")
def compare_dogs(request: Request, dog1: str, dog2: str, months: int | str = 1):
    policy = _resolve_access_policy(request)
    months = normalize_months(months)

    conn = get_connection()
    cur = conn.cursor()

    # build query and parameters with optional time filter
    query = """
        SELECT COUNT(DISTINCT r1.race_id)

        FROM runners r1
        JOIN runners r2 ON r1.race_id=r2.race_id
        JOIN dogs d1 ON r1.dog_id=d1.id
        JOIN dogs d2 ON r2.dog_id=d2.id
        JOIN races r ON r.id = r1.race_id
        JOIN meetings m ON r.meeting_id = m.id

        WHERE LOWER(TRIM(d1.name)) = LOWER(TRIM(%s))
        AND LOWER(TRIM(d2.name)) = LOWER(TRIM(%s))
        AND NOT (
            COALESCE(r1.result_comment, '') ILIKE '%%non runner%%'
            OR COALESCE(r1.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
            OR (
                r1.official_time IS NULL
                AND EXISTS (
                    SELECT 1
                    FROM runners rrn
                    WHERE rrn.race_id = r.id
                      AND rrn.finishing_position = 1
                      AND rrn.official_time IS NOT NULL
                )
            )
        )
        AND NOT (
            COALESCE(r2.result_comment, '') ILIKE '%%non runner%%'
            OR COALESCE(r2.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
            OR (
                r2.official_time IS NULL
                AND EXISTS (
                    SELECT 1
                    FROM runners rrn
                    WHERE rrn.race_id = r.id
                      AND rrn.finishing_position = 1
                      AND rrn.official_time IS NOT NULL
                )
            )
        )
                AND EXISTS (
                        SELECT 1
                        FROM runners rr
                        WHERE rr.race_id = r.id
                            AND rr.finishing_position = 1
                            AND rr.official_time IS NOT NULL
                )
    """
    params = [dog1, dog2]
    if months and months > 0:
        query += "\n        AND m.meeting_date >= CURRENT_DATE - (%s::int * INTERVAL '1 month')"
        params.append(months)

    track_filter = _apply_track_sql_filter("m.track", policy, params)
    if track_filter:
        query += "\n        " + track_filter.strip()

    cur.execute(query, tuple(params))

    count = cur.fetchone()[0]

    cur.close()
    conn.close()

    return {"count": count}


class RaceTotalsRequest(BaseModel):
    dogs: list[str]
    months: int | str = 1


def _count_common_for_pair(cur, dog1_id: int, dog2_id: int, months: int) -> int:
    query = """
    SELECT COUNT(*)
    FROM dogs d
    WHERE d.id NOT IN (%s, %s)

    AND EXISTS (
        SELECT 1
        FROM races ra
        JOIN meetings ma ON ra.meeting_id = ma.id
        WHERE (
              %s = 0
              OR ma.meeting_date >= CURRENT_DATE - (%s::int * INTERVAL '1 month')
          )
                    AND EXISTS (
                            SELECT 1
                            FROM runners rr
                            WHERE rr.race_id = ra.id
                                AND rr.finishing_position = 1
                                AND rr.official_time IS NOT NULL
                    )
          AND EXISTS (
              SELECT 1
              FROM runners rx
              WHERE rx.race_id = ra.id
                AND rx.dog_id = d.id
          )
          AND EXISTS (
              SELECT 1
              FROM runners r1
              WHERE r1.race_id = ra.id
                AND r1.dog_id = %s
          )
          AND NOT EXISTS (
              SELECT 1
              FROM runners r2
              WHERE r2.race_id = ra.id
                AND r2.dog_id = %s
          )
    )

    AND EXISTS (
        SELECT 1
        FROM races rb
        JOIN meetings mb ON rb.meeting_id = mb.id
        WHERE (
              %s = 0
              OR mb.meeting_date >= CURRENT_DATE - (%s::int * INTERVAL '1 month')
          )
                    AND EXISTS (
                            SELECT 1
                            FROM runners rr
                            WHERE rr.race_id = rb.id
                                AND rr.finishing_position = 1
                                AND rr.official_time IS NOT NULL
                    )
          AND EXISTS (
              SELECT 1
              FROM runners rx
              WHERE rx.race_id = rb.id
                AND rx.dog_id = d.id
          )
          AND EXISTS (
              SELECT 1
              FROM runners r2
              WHERE r2.race_id = rb.id
                AND r2.dog_id = %s
          )
          AND NOT EXISTS (
              SELECT 1
              FROM runners r1
              WHERE r1.race_id = rb.id
                AND r1.dog_id = %s
          )
    )
    """

    cur.execute(
        query,
        (
            dog1_id,
            dog2_id,
            months,
            months,
            dog1_id,
            dog2_id,
            months,
            months,
            dog2_id,
            dog1_id,
        ),
    )

    return cur.fetchone()[0] or 0


@app.post("/api/race_totals")
def race_totals(request: Request, payload: RaceTotalsRequest):
    policy = _resolve_access_policy(request)
    months = normalize_comparison_months(payload.months)
    raw_dogs = payload.dogs or []

    # Preserve order while removing blanks/duplicates.
    seen = set()
    dogs = []
    for name in raw_dogs:
        clean = (name or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        dogs.append(clean)

    if len(dogs) < 2:
        return {"avb_total": 0, "common_total": 0, "pair_count": 0}

    conn = get_connection()
    cur = conn.cursor()

    has_track_restriction = bool(policy.get("allowed_tracks_set"))
    allowed_tracks = list(policy.get("allowed_tracks_set") or [])

    normalized_dog_keys = [name.strip().lower() for name in dogs]

    cur.execute(
        """
        SELECT id, name
        FROM dogs
        WHERE LOWER(TRIM(name)) = ANY(%s)
        """,
        (normalized_dog_keys,),
    )

    rows = cur.fetchall()
    id_by_name = {str(name or "").strip().lower(): dog_id for dog_id, name in rows}
    dog_ids = [id_by_name[n.strip().lower()] for n in dogs if n.strip().lower() in id_by_name]

    if len(dog_ids) < 2:
        cur.close()
        conn.close()
        return {"avb_total": 0, "common_total": 0, "pair_count": 0}

    # Short-lived in-memory cache for repeated totals filter requests.
    # Include access scope so cached totals cannot leak across restricted plans.
    cache_key = (
        months,
        tuple(sorted(dog_ids)),
        tuple(sorted(allowed_tracks)) if has_track_restriction else None,
    )
    now = time.time()
    cached_entry = race_totals_cache.get(cache_key)
    if cached_entry and now - cached_entry["ts"] <= RACE_TOTALS_CACHE_TTL_SECONDS:
        cur.close()
        conn.close()
        return cached_entry["value"]

    pair_count = (len(dog_ids) * (len(dog_ids) - 1)) // 2

    cur.execute(
        """
        WITH winner_races AS MATERIALIZED (
            SELECT DISTINCT rr.race_id
            FROM runners rr
            WHERE rr.finishing_position = 1
              AND rr.official_time IS NOT NULL
        ),
        candidate_races AS MATERIALIZED (
            SELECT DISTINCT ru.race_id
            FROM runners ru
            JOIN races r ON r.id = ru.race_id
            JOIN meetings m ON m.id = r.meeting_id
            JOIN winner_races wr ON wr.race_id = r.id
            WHERE ru.dog_id = ANY(%s)
              AND (
                    %s = 0
                    OR m.meeting_date >= CURRENT_DATE - (%s::int * INTERVAL '1 month')
                  )
              AND (
                  %s = FALSE
                  OR LOWER(TRIM(m.track)) = ANY(%s)
              )
              AND NOT (
                  COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
                  OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                  OR ru.official_time IS NULL
              )
        ),
        race_dogs AS MATERIALIZED (
            SELECT ru.race_id, ru.dog_id
            FROM runners ru
            JOIN candidate_races cr ON cr.race_id = ru.race_id
            WHERE NOT (
                COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
                OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                OR ru.official_time IS NULL
            )
        ),
        pair_races AS (
            SELECT
                rd1.race_id,
                LEAST(rd1.dog_id, rd2.dog_id) AS dog1_id,
                GREATEST(rd1.dog_id, rd2.dog_id) AS dog2_id
            FROM race_dogs rd1
            JOIN race_dogs rd2
              ON rd1.race_id = rd2.race_id
             AND rd1.dog_id < rd2.dog_id
            WHERE rd1.dog_id = ANY(%s)
              AND rd2.dog_id = ANY(%s)
        ),
        pair_counts AS (
            SELECT dog1_id, dog2_id, COUNT(DISTINCT race_id) AS cnt
            FROM pair_races
            GROUP BY dog1_id, dog2_id
        )
        SELECT COALESCE(SUM(cnt), 0)
        FROM pair_counts
        """,
        (
            dog_ids,
            months,
            months,
            has_track_restriction,
            allowed_tracks,
            dog_ids,
            dog_ids,
        ),
    )
    avb_total = int((cur.fetchone() or [0])[0] or 0)

    cur.execute(
        """
        WITH selected_dogs AS (
            SELECT UNNEST(%s::int[]) AS dog_id
        ),
        pairs AS (
            SELECT
                LEAST(a.dog_id, b.dog_id) AS dog1_id,
                GREATEST(a.dog_id, b.dog_id) AS dog2_id
            FROM selected_dogs a
            JOIN selected_dogs b ON a.dog_id < b.dog_id
        ),
        winner_races AS MATERIALIZED (
            SELECT DISTINCT rr.race_id
            FROM runners rr
            WHERE rr.finishing_position = 1
              AND rr.official_time IS NOT NULL
        ),
        candidate_races AS MATERIALIZED (
            SELECT DISTINCT ru.race_id
            FROM runners ru
            JOIN races r ON r.id = ru.race_id
            JOIN meetings m ON m.id = r.meeting_id
            JOIN winner_races wr ON wr.race_id = r.id
            WHERE ru.dog_id = ANY(%s)
              AND (
                    %s = 0
                    OR m.meeting_date >= CURRENT_DATE - (%s::int * INTERVAL '1 month')
                  )
              AND (
                  %s = FALSE
                  OR LOWER(TRIM(m.track)) = ANY(%s)
              )
              AND NOT (
                  COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
                  OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                  OR ru.official_time IS NULL
              )
        ),
        race_dogs AS MATERIALIZED (
            SELECT ru.race_id, ru.dog_id
            FROM runners ru
            JOIN candidate_races cr ON cr.race_id = ru.race_id
            WHERE NOT (
                COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
                OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                OR ru.official_time IS NULL
            )
        ),
        race_selected_presence AS (
            SELECT rd.race_id, rd.dog_id
            FROM race_dogs rd
            WHERE rd.dog_id = ANY(%s)
        ),
        pair_race_side AS (
            SELECT
                p.dog1_id,
                p.dog2_id,
                rsp.race_id,
                MAX(CASE WHEN rsp.dog_id = p.dog1_id THEN 1 ELSE 0 END) AS has_dog1,
                MAX(CASE WHEN rsp.dog_id = p.dog2_id THEN 1 ELSE 0 END) AS has_dog2
            FROM pairs p
            JOIN race_selected_presence rsp
              ON rsp.dog_id = p.dog1_id
              OR rsp.dog_id = p.dog2_id
            GROUP BY p.dog1_id, p.dog2_id, rsp.race_id
        ),
        pair_opponent_side AS (
            SELECT
                prs.dog1_id,
                prs.dog2_id,
                rd.dog_id AS opponent_id,
                CASE
                    WHEN prs.has_dog1 = 1 AND prs.has_dog2 = 0 THEN 1
                    WHEN prs.has_dog1 = 0 AND prs.has_dog2 = 1 THEN 2
                    ELSE 0
                END AS side
            FROM pair_race_side prs
            JOIN race_dogs rd ON rd.race_id = prs.race_id
            WHERE rd.dog_id <> prs.dog1_id
              AND rd.dog_id <> prs.dog2_id
        ),
        pair_common AS (
            SELECT
                dog1_id,
                dog2_id,
                COUNT(*) AS common_cnt
            FROM (
                SELECT
                    dog1_id,
                    dog2_id,
                    opponent_id
                FROM pair_opponent_side
                WHERE side IN (1, 2)
                GROUP BY dog1_id, dog2_id, opponent_id
                HAVING BOOL_OR(side = 1) AND BOOL_OR(side = 2)
            ) matched_opponents
            GROUP BY dog1_id, dog2_id
        )
        SELECT COALESCE(SUM(common_cnt), 0)
        FROM pair_common
        """,
        (
            dog_ids,
            dog_ids,
            months,
            months,
            has_track_restriction,
            allowed_tracks,
            dog_ids,
        ),
    )
    common_total = int((cur.fetchone() or [0])[0] or 0)

    cur.close()
    conn.close()

    result = {
        "avb_total": avb_total,
        "common_total": common_total,
        "pair_count": pair_count,
    }

    race_totals_cache[cache_key] = {"ts": now, "value": result}
    if len(race_totals_cache) > RACE_TOTALS_CACHE_MAX_ITEMS:
        oldest_key = min(race_totals_cache.items(), key=lambda x: x[1]["ts"])[0]
        race_totals_cache.pop(oldest_key, None)

    return result


# --------------------------------------------------
# COMMON OPPONENT LIST
# --------------------------------------------------


@app.get("/api/common")
def common_opponents(request: Request, dog1: str, dog2: str, months: int | str = 1):
    t0 = time.perf_counter()
    policy = _resolve_access_policy(request)
    months = normalize_comparison_months(months)

    conn = get_connection()
    cur = conn.cursor()

    d1_key = str(dog1 or "").strip().lower()
    d2_key = str(dog2 or "").strip().lower()
    if not d1_key or not d2_key:
        cur.close()
        conn.close()
        return []

    cur.execute(
        """
        SELECT id, LOWER(TRIM(name))
        FROM dogs
        WHERE LOWER(TRIM(name)) IN (%s, %s)
        """,
        (d1_key, d2_key),
    )
    dog_rows = cur.fetchall()
    id_by_key = {str(name_key or ""): int(dog_id) for dog_id, name_key in dog_rows}
    dog1_id = id_by_key.get(d1_key)
    dog2_id = id_by_key.get(d2_key)
    if not dog1_id or not dog2_id:
        cur.close()
        conn.close()
        return []

    has_track_restriction = bool(policy.get("allowed_tracks_set"))
    allowed_tracks = list(policy.get("allowed_tracks_set") or [])
    cache_key = (
        months,
        tuple(sorted((dog1_id, dog2_id))),
        tuple(sorted(allowed_tracks)) if has_track_restriction else None,
    )
    now = time.time()
    cached_entry = common_opponents_cache.get(cache_key)
    if cached_entry and now - cached_entry["ts"] <= COMMON_OPP_CACHE_TTL_SECONDS:
        total_ms = int((time.perf_counter() - t0) * 1000)
        if PERF_LOG_ALWAYS or PERF_LOG_CACHE_HITS:
            print(
                f"[perf] /api/common total={total_ms}ms cache=hit rows={len(cached_entry['value'])} "
                f"dog1={dog1!r} dog2={dog2!r} months={months}"
            )
        cur.close()
        conn.close()
        return cached_entry["value"]

    query = """
    WITH winner_races AS MATERIALIZED (
        SELECT DISTINCT rr.race_id
        FROM runners rr
        WHERE rr.finishing_position = 1
          AND rr.official_time IS NOT NULL
    ),
    candidate_races AS MATERIALIZED (
        SELECT DISTINCT ru.race_id
        FROM runners ru
        JOIN races r ON r.id = ru.race_id
        JOIN meetings m ON m.id = r.meeting_id
        JOIN winner_races wr ON wr.race_id = r.id
        WHERE (ru.dog_id = %s OR ru.dog_id = %s)
          AND NOT (
              COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
              OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
              OR ru.official_time IS NULL
          )
    """
    params = []

    if months and months > 0:
        query += "\n          AND m.meeting_date >= CURRENT_DATE - (%s::int * INTERVAL '1 month')"
        params.append(months)

    track_filter = _apply_track_sql_filter("m.track", policy, params)
    if track_filter:
        query += "\n" + track_filter.strip()

    query += """
    ),
    valid_runners AS MATERIALIZED (
        SELECT ru.race_id, ru.dog_id
        FROM runners ru
        JOIN candidate_races cr ON cr.race_id = ru.race_id
        WHERE NOT (
            COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
            OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
            OR ru.official_time IS NULL
        )
    ),
    race_flags AS MATERIALIZED (
        SELECT
            vr.race_id,
            MAX(
                CASE
                    WHEN vr.dog_id = %s
                    THEN 1
                    ELSE 0
                END
            ) AS has_dog1,
            MAX(
                CASE
                    WHEN vr.dog_id = %s
                    THEN 1
                    ELSE 0
                END
            ) AS has_dog2
        FROM valid_runners vr
        WHERE vr.dog_id = %s
           OR vr.dog_id = %s
        GROUP BY vr.race_id
    ),
    opponent_flags AS (
        SELECT
            vr.dog_id AS opponent_id,
            MAX(CASE WHEN rf.has_dog1 = 1 AND rf.has_dog2 = 0 THEN 1 ELSE 0 END) AS seen_with_dog1_only,
            MAX(CASE WHEN rf.has_dog2 = 1 AND rf.has_dog1 = 0 THEN 1 ELSE 0 END) AS seen_with_dog2_only
        FROM valid_runners vr
        JOIN race_flags rf ON rf.race_id = vr.race_id
        WHERE vr.dog_id <> %s
          AND vr.dog_id <> %s
        GROUP BY vr.dog_id
    )
    SELECT d.name
    FROM opponent_flags ofl
    JOIN dogs d ON d.id = ofl.opponent_id
    WHERE ofl.seen_with_dog1_only = 1
      AND ofl.seen_with_dog2_only = 1
    ORDER BY d.name
    """

    params = [dog1_id, dog2_id] + params
    params.extend([dog1_id, dog2_id, dog1_id, dog2_id, dog1_id, dog2_id])
    cur.execute(query, tuple(params))

    rows = cur.fetchall()

    cur.close()
    conn.close()

    data = []

    for r in rows:
        data.append({"dog": r[0], "count": 1})

    common_opponents_cache[cache_key] = {"ts": now, "value": data}
    if len(common_opponents_cache) > COMMON_OPP_CACHE_MAX_ITEMS:
        oldest_key = min(common_opponents_cache.items(), key=lambda x: x[1]["ts"])[0]
        common_opponents_cache.pop(oldest_key, None)

    total_ms = int((time.perf_counter() - t0) * 1000)
    if _should_log_perf(total_ms, PERF_COMMON_THRESHOLD_MS):
        print(
            f"[perf] /api/common total={total_ms}ms cache=miss rows={len(rows)} "
            f"dog1={dog1!r} dog2={dog2!r} months={months}"
        )

    return data


# --------------------------------------------------
# A v B RACES (FIXED ? RETURNS 7 NOT 14)
# --------------------------------------------------


@app.get("/api/avb_races")
def avb_races(request: Request, dog1: str, dog2: str, months: int | str = 1):
    policy = _resolve_access_policy(request)
    member_key = _comment_scope_key(policy)
    months = normalize_comparison_months(months)

    conn = get_connection()
    cur = conn.cursor()

    # Return runner-level rows for both selected dogs in shared races.
    query = """
        SELECT
            m.meeting_date,
            m.track,
            m.gbgb_meeting_id,
            m.country,
            r.distance,
            r.grade,
            r.race_number,
            d.name,
            ru.trap,
            CASE
                WHEN ru.finishing_position IS NOT NULL THEN ru.finishing_position::text
                WHEN COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
                  OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                  OR (
                      ru.official_time IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM runners rrn
                          WHERE rrn.race_id = ru.race_id
                            AND rrn.finishing_position = 1
                            AND rrn.official_time IS NOT NULL
                      )
                  )
                THEN 'NR'
                ELSE NULL
            END,
            ROUND(ru.official_time,2),
            w.winner,
            w.winning_time,
            rc.comment AS runner_comment,
            ru.distance_beaten,
            ru.sp,
            r.going
            ,r.id AS race_id
            ,ru.dog_id

        FROM runners ru
        JOIN dogs d ON ru.dog_id = d.id
        JOIN races r ON ru.race_id = r.id
        JOIN meetings m ON r.meeting_id=m.id
        LEFT JOIN LATERAL (
            SELECT
                STRING_AGG(dw.name, ' / ' ORDER BY dw.name) AS winner,
                ROUND(MIN(rw.official_time)::numeric, 2) AS winning_time
            FROM runners rw
            JOIN dogs dw ON dw.id = rw.dog_id
            WHERE rw.race_id = r.id
              AND rw.finishing_position = 1
        ) w ON TRUE
                LEFT JOIN LATERAL (
                        SELECT COALESCE(
                                (
                                        SELECT mrc.comment
                                        FROM member_runner_comments mrc
                                        WHERE mrc.race_id = ru.race_id
                                            AND mrc.dog_id = ru.dog_id
                                            AND mrc.member_key = %s
                                        LIMIT 1
                                ),
                                (
                                        SELECT rcg.comment
                                        FROM runner_comments rcg
                                        WHERE rcg.race_id = ru.race_id
                                            AND rcg.dog_id = ru.dog_id
                                        LIMIT 1
                                )
                        ) AS comment
                ) rc ON TRUE

        WHERE ru.race_id IN (

            SELECT r1.race_id
            FROM runners r1
            JOIN runners r2 ON r1.race_id=r2.race_id
            JOIN dogs d1 ON r1.dog_id=d1.id
            JOIN dogs d2 ON r2.dog_id=d2.id

            WHERE (
                (LOWER(TRIM(d1.name))=LOWER(TRIM(%s)) AND LOWER(TRIM(d2.name))=LOWER(TRIM(%s)))
                OR (LOWER(TRIM(d1.name))=LOWER(TRIM(%s)) AND LOWER(TRIM(d2.name))=LOWER(TRIM(%s)))
            )
            AND NOT (
                COALESCE(r1.result_comment, '') ILIKE '%%non runner%%'
                OR COALESCE(r1.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                OR (
                    r1.official_time IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM runners rrn
                        WHERE rrn.race_id = r1.race_id
                          AND rrn.finishing_position = 1
                          AND rrn.official_time IS NOT NULL
                    )
                )
            )
            AND NOT (
                COALESCE(r2.result_comment, '') ILIKE '%%non runner%%'
                OR COALESCE(r2.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                OR (
                    r2.official_time IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM runners rrn
                        WHERE rrn.race_id = r2.race_id
                          AND rrn.finishing_position = 1
                          AND rrn.official_time IS NOT NULL
                    )
                )
            )

        )
        AND LOWER(TRIM(d.name)) IN (LOWER(TRIM(%s)), LOWER(TRIM(%s)))
        AND NOT (
            COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
            OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
            OR (
                ru.official_time IS NULL
                AND EXISTS (
                    SELECT 1
                    FROM runners rrn
                    WHERE rrn.race_id = ru.race_id
                      AND rrn.finishing_position = 1
                      AND rrn.official_time IS NOT NULL
                )
            )
        )
                AND EXISTS (
                        SELECT 1
                        FROM runners rr
                        WHERE rr.race_id = r.id
                            AND rr.finishing_position = 1
                            AND rr.official_time IS NOT NULL
                )
    """
    params = [member_key, dog1, dog2, dog2, dog1, dog1, dog2]
    if months and months > 0:
        query += "\n        AND m.meeting_date >= CURRENT_DATE - (%s::int * INTERVAL '1 month')"
        params.append(months)

    track_filter = _apply_track_sql_filter("m.track", policy, params)
    if track_filter:
        query += "\n        " + track_filter.strip()

    query += "\n        ORDER BY m.meeting_date DESC, r.race_number ASC, d.name ASC"

    cur.execute(query, tuple(params))

    rows = cur.fetchall()

    cur.close()
    conn.close()

    data = []
    for r in rows:
        if str(r[9] or "").upper() == "NR":
            continue
        data.append(
            {
                "date": str(r[0]),
                "track": r[1],
                "meeting_id": r[2],
                "country": r[3],
                "dist": r[4],
                "grade": r[5],
                "race": r[6],
                "dog": r[7],
                "trap": r[8],
                "pos": r[9],
                "time": r[10],
                "winner": r[11],
                "winning_time": r[12],
                "comment": r[13],
                "distance_beaten": r[14],
                "sp": r[15],
                "going": r[16],
                "race_id": r[17],
                "dog_id": r[18],
            }
        )

    return data


# --------------------------------------------------
# COMMON OPPONENT RACES
# --------------------------------------------------


@app.get("/api/common_races")
def common_races(
    request: Request,
    dog1: str,
    dog2: str,
    opponent: str,
    months: int | str = 1,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    t0 = time.perf_counter()
    policy = _resolve_access_policy(request)
    member_key = _comment_scope_key(policy)
    months = normalize_comparison_months(months)

    conn = get_connection()
    cur = conn.cursor()

    # Resolve dog ids once, then keep the main query id-based for speed.
    d1_key = str(dog1 or "").strip().lower()
    d2_key = str(dog2 or "").strip().lower()
    opp_key = str(opponent or "").strip().lower()
    if not d1_key or not d2_key or not opp_key:
        cur.close()
        conn.close()
        return []

    cur.execute(
        """
        SELECT id, LOWER(TRIM(name))
        FROM dogs
        WHERE LOWER(TRIM(name)) IN (%s, %s, %s)
        """,
        (d1_key, d2_key, opp_key),
    )
    id_rows = cur.fetchall()
    t_ids = time.perf_counter()
    id_by_key = {str(name_key or ""): int(dog_id) for dog_id, name_key in id_rows}
    dog1_id = id_by_key.get(d1_key)
    dog2_id = id_by_key.get(d2_key)
    opponent_id = id_by_key.get(opp_key)
    if not dog1_id or not dog2_id or not opponent_id:
        cur.close()
        conn.close()
        return []

    has_track_restriction = bool(policy.get("allowed_tracks_set"))
    allowed_tracks = list(policy.get("allowed_tracks_set") or [])
    race_cache_key = (
        months,
        tuple(sorted((dog1_id, dog2_id))),
        opponent_id,
        int(limit),
        int(offset),
        tuple(sorted(allowed_tracks)) if has_track_restriction else None,
    )
    now = time.time()
    cached_races = common_races_cache.get(race_cache_key)
    if cached_races and now - cached_races["ts"] <= COMMON_RACES_CACHE_TTL_SECONDS:
        total_ms = int((time.perf_counter() - t0) * 1000)
        if PERF_LOG_ALWAYS or PERF_LOG_CACHE_HITS:
            print(
                f"[perf] /api/common_races total={total_ms}ms cache=hit races=? rows={len(cached_races['value'])} "
                f"dog1={dog1!r} dog2={dog2!r} opp={opponent!r} months={months} limit={limit} offset={offset}"
            )
        cur.close()
        conn.close()
        return cached_races["value"]

    # Step 1: page race ids from recent meetings using indexed EXISTS probes.
    race_id_query = """
        WITH recent_races AS (
            SELECT
                r.id,
                m.meeting_date,
                r.race_number
            FROM races r
            JOIN meetings m ON m.id = r.meeting_id
            WHERE 1=1
    """
    race_id_params = []

    if months and months > 0:
        race_id_query += "\n              AND m.meeting_date >= CURRENT_DATE - (%s::int * INTERVAL '1 month')"
        race_id_params.append(months)

    track_filter = _apply_track_sql_filter("m.track", policy, race_id_params)
    if track_filter:
        race_id_query += "\n" + track_filter.strip()

    race_id_query += """
        )
        SELECT
            rr.id,
            rr.meeting_date,
            rr.race_number
        FROM recent_races rr
        WHERE EXISTS (
            SELECT 1
            FROM runners ro
            WHERE ro.race_id = rr.id
              AND ro.dog_id = %s
              AND NOT (
                  COALESCE(ro.result_comment, '') ILIKE '%%non runner%%'
                  OR COALESCE(ro.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                  OR (
                    ro.official_time IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM runners rrn
                        WHERE rrn.race_id = ro.race_id
                        AND rrn.finishing_position = 1
                        AND rrn.official_time IS NOT NULL
                    )
                  )
              )
        )
          AND EXISTS (
            SELECT 1
            FROM runners rp
            WHERE rp.race_id = rr.id
              AND rp.dog_id = ANY(%s::int[])
              AND NOT (
                  COALESCE(rp.result_comment, '') ILIKE '%%non runner%%'
                  OR COALESCE(rp.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                  OR (
                    rp.official_time IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM runners rrn
                        WHERE rrn.race_id = rp.race_id
                        AND rrn.finishing_position = 1
                        AND rrn.official_time IS NOT NULL
                    )
                  )
              )
        )
          AND EXISTS (
            SELECT 1
            FROM runners rw
            WHERE rw.race_id = rr.id
              AND rw.finishing_position = 1
              AND rw.official_time IS NOT NULL
        )
        ORDER BY rr.meeting_date DESC, rr.race_number DESC, rr.id DESC
    """
    race_id_params.extend([opponent_id, [dog1_id, dog2_id]])

    race_id_query += "\n    LIMIT %s OFFSET %s"
    race_id_params.extend([int(limit), int(offset)])

    cur.execute(race_id_query, tuple(race_id_params))
    paged_races = cur.fetchall()
    t_page_ids = time.perf_counter()
    if not paged_races:
        cur.close()
        conn.close()
        total_ms = int((time.perf_counter() - t0) * 1000)
        if _should_log_perf(total_ms, PERF_COMMON_RACES_THRESHOLD_MS):
            print(
                f"[perf] /api/common_races total={total_ms}ms ids={int((t_ids-t0)*1000)}ms "
                f"page={int((t_page_ids-t_ids)*1000)}ms rows=0 cache=miss "
                f"dog1={dog1!r} dog2={dog2!r} opp={opponent!r} months={months} limit={limit} offset={offset}"
            )
        return []

    race_ids = [int(r[0]) for r in paged_races if r and r[0] is not None]

    rows_query = """
    SELECT
        m.meeting_date,
        m.track,
        m.gbgb_meeting_id,
        m.country,
        r.race_number,
        r.id AS race_id,
        STRING_AGG(
            COALESCE(ru.trap::text, '') || '|' || COALESCE(d.name, ''),
            '||'
            ORDER BY ru.trap ASC, d.name ASC
        ) AS dogs_compact
    FROM races r
    JOIN meetings m ON m.id = r.meeting_id
    JOIN runners ru ON ru.race_id = r.id
    JOIN dogs d ON d.id = ru.dog_id
    WHERE r.id = ANY(%s::int[])
      AND ru.dog_id = ANY(%s::int[])
            AND NOT (
                        COALESCE(ru.result_comment, '') ILIKE '%%non runner%%'
                        OR COALESCE(ru.result_comment, '') ~* '(^|[^A-Za-z])nr([^A-Za-z]|$)'
                        OR (
                                ru.official_time IS NULL
                                AND EXISTS (
                                        SELECT 1
                                        FROM runners rrn
                                        WHERE rrn.race_id = ru.race_id
                                            AND rrn.finishing_position = 1
                                            AND rrn.official_time IS NOT NULL
                                )
                        )
            )
    GROUP BY m.meeting_date, m.track, m.gbgb_meeting_id, m.country, r.race_number, r.id
    ORDER BY m.meeting_date DESC, r.race_number DESC, r.id DESC
    """

    cur.execute(rows_query, (race_ids, [dog1_id, dog2_id, opponent_id]))
    rows = cur.fetchall()
    t_rows = time.perf_counter()

    cur.close()
    conn.close()

    data = []

    for r in rows:
        data.append(
            {
                "date": str(r[0]),
                "track": r[1],
                "meeting_id": r[2],
                "country": r[3],
                "dist": None,
                "grade": None,
                "race": r[4],
                "race_id": r[5],
                "dogs_compact": r[6] or "",
                "winner": None,
                "winning_time": None,
                "going": None,
            }
        )

    t_serialize = time.perf_counter()
    total_ms = int((t_serialize - t0) * 1000)
    if _should_log_perf(total_ms, PERF_COMMON_RACES_THRESHOLD_MS):
        print(
            f"[perf] /api/common_races total={total_ms}ms ids={int((t_ids-t0)*1000)}ms "
            f"page={int((t_page_ids-t_ids)*1000)}ms rows={int((t_rows-t_page_ids)*1000)}ms "
            f"json={int((t_serialize-t_rows)*1000)}ms races={len(race_ids)} rows={len(rows)} cache=miss "
            f"dog1={dog1!r} dog2={dog2!r} opp={opponent!r} months={months} limit={limit} offset={offset}"
        )

    common_races_cache[race_cache_key] = {"ts": now, "value": data}
    if len(common_races_cache) > COMMON_RACES_CACHE_MAX_ITEMS:
        oldest_key = min(common_races_cache.items(), key=lambda x: x[1]["ts"])[0]
        common_races_cache.pop(oldest_key, None)

    return data


@app.get("/manual-racecard", response_class=HTMLResponse)
def manual_racecard_page(request: Request):
    return templates.TemplateResponse("manual_racecard.html", {"request": request})


@app.post("/api/manual_racecard/import")
def manual_racecard_import(data: dict):
    racecard = data.get("racecard") if isinstance(data, dict) else None
    payload = racecard if isinstance(racecard, dict) else data
    dry_run = bool(data.get("dry_run", False)) if isinstance(data, dict) else False
    allow_overwrite_results = (
        bool(data.get("allow_overwrite_results", False)) if isinstance(data, dict) else False
    )

    try:
        result = import_manual_payload(
            payload,
            allow_overwrite_results=allow_overwrite_results,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Manual import failed: {exc}") from exc

    return {"success": True, **result}


@app.get("/manual-results", response_class=HTMLResponse)
def manual_results_page(request: Request):
    return templates.TemplateResponse("manual_results.html", {"request": request})


@app.post("/api/manual_results/import")
def manual_results_import(data: dict):
    results_payload = data.get("results") if isinstance(data, dict) else None
    payload = results_payload if isinstance(results_payload, dict) else data
    dry_run = bool(data.get("dry_run", False)) if isinstance(data, dict) else False
    allow_overwrite_results = (
        bool(data.get("allow_overwrite_results", False)) if isinstance(data, dict) else False
    )

    try:
        result = import_manual_results_payload(
            payload,
            allow_overwrite_results=allow_overwrite_results,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Manual results import failed: {exc}") from exc

    return {"success": True, **result}


@app.get("/dog-rename", response_class=HTMLResponse)
def dog_rename_page(request: Request):
    return templates.TemplateResponse("dog_rename.html", {"request": request})


@app.post("/api/dog/rename")
def dog_rename(data: dict):
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid request body")

    old_name = data.get("old_name", "")
    new_name = data.get("new_name", "")
    dry_run = bool(data.get("dry_run", False))
    preview_only = bool(data.get("preview_only", False))

    try:
        result = rename_or_merge_dog(
            old_name=old_name,
            new_name=new_name,
            dry_run=dry_run,
            preview_only=preview_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Dog rename failed: {exc}") from exc

    return {"success": True, **result}
