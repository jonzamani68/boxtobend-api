import argparse
import json
import re
import zlib
from datetime import datetime

from db import get_connection


def parse_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_race_time(value: str | None):
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    datetime.strptime(value, "%H:%M")
    return value


def normalize_country(value: str | None) -> str:
    if not value:
        return "GB"
    raw = value.strip().upper()
    if raw in {"UK", "GB", "GBR"}:
        return "GB"
    if raw in {"IRE", "IRL", "IE"}:
        return "IRE"
    return raw


def make_base_race_id(track: str, meeting_date, race_number: int):
    key = f"MANUAL|{track.upper()}|{meeting_date.isoformat()}|{race_number}"
    return -int((zlib.crc32(key.encode("utf-8")) % 1900000000) + 1)


def resolve_race_id(cursor, proposed_id: int, meeting_id: int, race_number: int):
    race_id = proposed_id
    while True:
        cursor.execute("SELECT meeting_id, race_number FROM races WHERE id = %s", (race_id,))
        row = cursor.fetchone()
        if not row:
            return race_id
        if row[0] == meeting_id and row[1] == race_number:
            return race_id
        race_id -= 1


def get_or_create_dog(cursor, name: str):
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Dog name cannot be empty")

    cursor.execute(
        "SELECT id FROM dogs WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s)) ORDER BY id LIMIT 1",
        (cleaned,),
    )
    row = cursor.fetchone()
    if row:
        return row[0]

    cursor.execute("INSERT INTO dogs (name) VALUES (%s) RETURNING id", (cleaned,))
    return cursor.fetchone()[0]


def ensure_meeting(
    cursor,
    track: str,
    meeting_date,
    country: str,
    gbgb_meeting_id: int | None = None,
):
    if gbgb_meeting_id is not None:
        cursor.execute(
            """
            SELECT id
            FROM meetings
            WHERE gbgb_meeting_id = %s
            ORDER BY id
            LIMIT 1
            """,
            (gbgb_meeting_id,),
        )
        row = cursor.fetchone()
        if row:
            return row[0]

        # Claim an unbound same-day meeting row if one exists; otherwise create a new meeting.
        cursor.execute(
            """
            SELECT id
            FROM meetings
            WHERE track = %s AND meeting_date = %s AND country = %s
              AND gbgb_meeting_id IS NULL
            ORDER BY id
            LIMIT 1
            """,
            (track, meeting_date, country),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                """
                UPDATE meetings
                SET gbgb_meeting_id = %s
                WHERE id = %s
                """,
                (gbgb_meeting_id, row[0]),
            )
            return row[0]

        cursor.execute(
            """
            INSERT INTO meetings (gbgb_meeting_id, track, meeting_date, country)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (gbgb_meeting_id, track, meeting_date, country),
        )
        return cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT id
        FROM meetings
        WHERE track = %s AND meeting_date = %s AND country = %s
        ORDER BY id
        LIMIT 1
        """,
        (track, meeting_date, country),
    )
    row = cursor.fetchone()
    if row:
        return row[0]

    cursor.execute(
        """
        INSERT INTO meetings (gbgb_meeting_id, track, meeting_date, country)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (gbgb_meeting_id, track, meeting_date, country),
    )
    return cursor.fetchone()[0]


def ensure_race(cursor, meeting_id: int, track: str, meeting_date, race_data: dict):
    race_number = race_data["race_number"]

    cursor.execute(
        """
        SELECT id
        FROM races
        WHERE meeting_id = %s AND race_number = %s
        ORDER BY id
        LIMIT 1
        """,
        (meeting_id, race_number),
    )
    existing = cursor.fetchone()

    if existing:
        race_id = existing[0]
    else:
        base_id = make_base_race_id(track, meeting_date, race_number)
        race_id = resolve_race_id(cursor, base_id, meeting_id, race_number)

    cursor.execute(
        """
        INSERT INTO races (id, meeting_id, race_number, race_name, distance, grade, scheduled_time, going)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id)
        DO UPDATE SET
            meeting_id = EXCLUDED.meeting_id,
            race_number = EXCLUDED.race_number,
            race_name = EXCLUDED.race_name,
            distance = EXCLUDED.distance,
            grade = EXCLUDED.grade,
            scheduled_time = EXCLUDED.scheduled_time,
            going = EXCLUDED.going
        """,
        (
            race_id,
            meeting_id,
            race_number,
            race_data.get("race_name"),
            race_data.get("distance"),
            race_data.get("grade"),
            race_data.get("scheduled_time"),
            race_data.get("going"),
        ),
    )

    return race_id


def validate_payload(payload: dict):
    if not isinstance(payload, dict):
        raise ValueError("Top-level JSON must be an object")

    required = ["track", "meeting_date", "races"]
    for key in required:
        if key not in payload:
            raise ValueError(f"Missing required field: {key}")

    if not isinstance(payload["races"], list) or not payload["races"]:
        raise ValueError("races must be a non-empty array")

    race_numbers = set()
    for idx, race in enumerate(payload["races"], start=1):
        if "race_number" not in race:
            raise ValueError(f"Race #{idx} missing race_number")

        race_number = race["race_number"]
        if not isinstance(race_number, int) or race_number <= 0:
            raise ValueError(f"Race #{idx} has invalid race_number: {race_number}")

        if race_number in race_numbers:
            raise ValueError(f"Duplicate race_number in payload: {race_number}")
        race_numbers.add(race_number)

        runners = race.get("runners", [])
        if not isinstance(runners, list) or not runners:
            raise ValueError(f"Race {race_number} must include at least one runner")

        seen_traps = set()
        for runner in runners:
            trap = runner.get("trap")
            dog = (runner.get("dog") or "").strip()

            if not isinstance(trap, int) or trap < 1 or trap > 6:
                raise ValueError(f"Race {race_number} has invalid trap: {trap}")
            if trap in seen_traps:
                raise ValueError(f"Race {race_number} has duplicate trap: {trap}")
            seen_traps.add(trap)

            if not dog:
                raise ValueError(f"Race {race_number} trap {trap} is missing dog name")


def import_manual_payload(payload: dict, allow_overwrite_results: bool = False, dry_run: bool = False):
    validate_payload(payload)

    track = payload["track"].strip()
    meeting_date = parse_date(payload["meeting_date"])
    country = normalize_country(payload.get("country"))
    gbgb_meeting_id = payload.get("gbgb_meeting_id")
    if gbgb_meeting_id is not None:
        gbgb_meeting_id = int(gbgb_meeting_id)

    conn = get_connection()
    cur = conn.cursor()

    races_written = 0
    runners_written = 0

    try:
        meeting_id = ensure_meeting(
            cur,
            track,
            meeting_date,
            country,
            gbgb_meeting_id=gbgb_meeting_id,
        )

        for race in payload["races"]:
            race_number = race["race_number"]
            scheduled_time = parse_race_time(race.get("scheduled_time"))

            race_data = {
                "race_number": race_number,
                "race_name": race.get("race_name"),
                "distance": race.get("distance"),
                "grade": race.get("grade"),
                "scheduled_time": scheduled_time,
                "going": race.get("going") or "",
            }

            race_id = ensure_race(cur, meeting_id, track, meeting_date, race_data)

            cur.execute(
                """
                SELECT COUNT(*)
                FROM runners
                WHERE race_id = %s
                  AND (finishing_position IS NOT NULL OR official_time IS NOT NULL)
                """,
                (race_id,),
            )
            has_results = cur.fetchone()[0] > 0
            if has_results and not allow_overwrite_results:
                raise ValueError(
                    f"Race {race_number} already has result data. "
                    "Use --allow-overwrite-results to replace it."
                )

            # Replace current runner rows so repeated imports remain idempotent.
            cur.execute("DELETE FROM runners WHERE race_id = %s", (race_id,))

            for runner in race["runners"]:
                dog_id = get_or_create_dog(cur, runner["dog"].strip())

                cur.execute(
                    """
                    INSERT INTO runners (
                        race_id, dog_id, trap, finishing_position, official_time,
                        distance_beaten, sp, result_comment, sectional_time
                    )
                    VALUES (%s, %s, %s, NULL, NULL, NULL, %s, %s, NULL)
                    """,
                    (
                        race_id,
                        dog_id,
                        runner["trap"],
                        runner.get("sp"),
                        runner.get("comment"),
                    ),
                )
                runners_written += 1

            races_written += 1

        if dry_run:
            conn.rollback()
            message = (
                f"Dry run only. meeting='{track}' date={meeting_date} country={country} "
                f"races={races_written} runners={runners_written}"
            )
            print(message)
        else:
            conn.commit()
            message = (
                f"Imported manual racecard. meeting='{track}' date={meeting_date} country={country} "
                f"races={races_written} runners={runners_written}"
            )
            print(message)

        return {
            "meeting": track,
            "meeting_date": str(meeting_date),
            "country": country,
            "races": races_written,
            "runners": runners_written,
            "dry_run": dry_run,
            "message": message,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def import_manual_racecard(json_path: str, allow_overwrite_results: bool = False, dry_run: bool = False):
    with open(json_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    return import_manual_payload(
        payload,
        allow_overwrite_results=allow_overwrite_results,
        dry_run=dry_run,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Manual racecard importer for outages (JSON -> meetings/races/runners)"
    )
    parser.add_argument("--file", required=True, help="Path to JSON racecard file")
    parser.add_argument(
        "--allow-overwrite-results",
        action="store_true",
        help="Allow replacement even when race already has finishing positions/times",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and simulate import without committing to database",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    import_manual_racecard(
        json_path=args.file,
        allow_overwrite_results=args.allow_overwrite_results,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
