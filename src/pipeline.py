import csv
import json
import re
import sqlite3
from datetime import datetime, date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = PROJECT_ROOT / "reports"
DB_PATH = DATA_DIR / "consultbae.db"


def normalise_email(value):
    """Lowercase and trim an email address."""
    return (value or "").strip().lower()


def normalise_phone(value):
    """Normalize valid Indian phone formats without blindly truncating."""
    digits = re.sub(r"\D", "", value or "")

    if len(digits) == 10:
        return digits

    if len(digits) == 12 and digits.startswith("91"):
        return digits[-10:]

    return ""


def normalise_name(value):
    """Standardise whitespace and case for comparison/display."""
    return " ".join((value or "").split()).title()


def normalise_city(value):
    """Standardise known city aliases without using city to merge people."""
    city = " ".join((value or "").split()).lower()
    aliases = {
        "bangalore": "Bengaluru",
        "bengaluru": "Bengaluru",
        "gurgaon": "Gurugram",
        "gurugram": "Gurugram",
        "new delhi": "New Delhi",
        "delhi ncr": "Delhi NCR",
        "delhi": "Delhi",
        "noida": "Noida",
        "pune": "Pune",
    }
    return aliases.get(city, city.title())


def parse_ctc(raw_value):
    """
    Approved business rule:
    decimal values are LPA; integer values are annual INR.
    Returns raw value, unit, and annual INR.
    """
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return raw_value, None, None

    value = float(raw_value)
    if "." in raw_value:
        return raw_value, "LPA", round(value * 100_000, 2)

    return raw_value, "INR_ANNUAL", round(value, 2)


def parse_rate(raw_value):
    """Keep original rate and separately parse amount and period."""
    raw_value = (raw_value or "").strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(k)?/(hr|hour|month)", raw_value)

    if not match:
        return raw_value, None, None

    amount = float(match.group(1))
    if match.group(2) == "k":
        amount *= 1000

    period = "hour" if match.group(3) in {"hr", "hour"} else "month"
    return raw_value, amount, period


def parse_date(raw_value):
    """Normalise known formats to YYYY-MM-DD."""
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None

    formats = [
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d %b %Y",
        "%m/%d/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(raw_value, fmt).date().isoformat()
        except ValueError:
            pass

    return None


def create_schema(connection):
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS people (
            person_id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL,
            canonical_email TEXT,
            canonical_phone TEXT,
            canonical_city TEXT,
            match_status TEXT NOT NULL DEFAULT 'matched'
        );

        CREATE TABLE IF NOT EXISTS identifiers (
            identifier_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            identifier_type TEXT NOT NULL,
            raw_value TEXT NOT NULL,
            normalised_value TEXT NOT NULL,
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        );

        CREATE TABLE IF NOT EXISTS source_records (
            source_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER,
            source_name TEXT NOT NULL,
            source_row_number INTEGER NOT NULL,
            ingestion_status TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        );

        CREATE TABLE IF NOT EXISTS naukri_profiles (
            profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            experience_years REAL,
            current_ctc_raw TEXT,
            current_ctc_unit TEXT,
            current_ctc_annual_inr REAL,
            applied_date_raw TEXT,
            applied_date_iso TEXT,
            skills_raw TEXT,
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        );

        CREATE TABLE IF NOT EXISTS gig_profiles (
            profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            rate_raw TEXT,
            rate_amount REAL,
            rate_period TEXT,
            worker_status TEXT,
            skills_raw TEXT,
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        );

        CREATE TABLE IF NOT EXISTS cbnexus_profiles (
            profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            verified INTEGER,
            projects_completed INTEGER,
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        );

        CREATE TABLE IF NOT EXISTS quality_issues (
            issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            source_row_number INTEGER,
            issue_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            details TEXT NOT NULL,
            raw_json TEXT
        );
        """
    )


def add_issue(connection, source, row_number, issue_type, severity, details, row):
    connection.execute(
        """
        INSERT INTO quality_issues
        (source_name, source_row_number, issue_type, severity, details, raw_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            row_number,
            issue_type,
            severity,
            details,
            json.dumps(row, ensure_ascii=False),
        ),
    )


def find_matching_person(connection, email, phone):
    """
    Match only on approved strong identifiers:
    exact normalised email or exact normalised phone.
    """
    person_ids = set()

    if email:
        rows = connection.execute(
            """
            SELECT DISTINCT person_id
            FROM identifiers
            WHERE identifier_type = 'email' AND normalised_value = ?
            """,
            (email,),
        ).fetchall()
        person_ids.update(row[0] for row in rows)

    if phone:
        rows = connection.execute(
            """
            SELECT DISTINCT person_id
            FROM identifiers
            WHERE identifier_type = 'phone' AND normalised_value = ?
            """,
            (phone,),
        ).fetchall()
        person_ids.update(row[0] for row in rows)

    if len(person_ids) == 1:
        return person_ids.pop(), "matched"

    if len(person_ids) > 1:
        return None, "conflict_review"

    return None, "new"


def add_identifier(connection, person_id, identifier_type, raw_value, normalised_value):
    if not normalised_value:
        return

    exists = connection.execute(
        """
        SELECT 1 FROM identifiers
        WHERE person_id = ? AND identifier_type = ? AND normalised_value = ?
        """,
        (person_id, identifier_type, normalised_value),
    ).fetchone()

    if not exists:
        connection.execute(
            """
            INSERT INTO identifiers
            (person_id, identifier_type, raw_value, normalised_value)
            VALUES (?, ?, ?, ?)
            """,
            (person_id, identifier_type, raw_value, normalised_value),
        )


def get_or_create_person(connection, name, raw_email, raw_phone, city, source, row_number, row):
    email = normalise_email(raw_email)
    phone = normalise_phone(raw_phone)
    person_id, match_result = find_matching_person(connection, email, phone)

    if match_result == "conflict_review":
        add_issue(
            connection,
            source,
            row_number,
            "CONFLICTING_IDENTIFIERS",
            "high",
            "Email and phone resolve to different existing people. No automatic merge.",
            row,
        )
        person_id = None

    if person_id is None:
        connection.execute(
            """
            INSERT INTO people
            (canonical_name, canonical_email, canonical_phone, canonical_city, match_status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                normalise_name(name) or "Unknown",
                email or None,
                phone or None,
                normalise_city(city) or None,
                "conflict_review" if match_result == "conflict_review" else "matched",
            ),
        )
        person_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    else:
        existing = connection.execute(
            "SELECT canonical_name FROM people WHERE person_id = ?",
            (person_id,),
        ).fetchone()[0]

        # Keep the more complete display name, e.g. Rohit Verma over R. Verma.
        candidate_name = normalise_name(name)
        if len(candidate_name) > len(existing):
            connection.execute(
                "UPDATE people SET canonical_name = ? WHERE person_id = ?",
                (candidate_name, person_id),
            )

    add_identifier(connection, person_id, "email", raw_email, email)
    add_identifier(connection, person_id, "phone", raw_phone, phone)
    return person_id


def save_source_record(connection, person_id, source, row_number, status, row):
    connection.execute(
        """
        INSERT INTO source_records
        (person_id, source_name, source_row_number, ingestion_status, raw_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (person_id, source, row_number, status, json.dumps(row, ensure_ascii=False)),
    )


def ingest_naukri(connection):
    source = "source1_naukri_applicants.csv"

    with (DATA_DIR / source).open(encoding="utf-8-sig", newline="") as file:
        for row_number, row in enumerate(csv.DictReader(file), start=2):
            person_id = get_or_create_person(
                connection,
                row["Full Name"],
                row["Email"],
                row["Phone"],
                row["City"],
                source,
                row_number,
                row,
            )

            raw_ctc, ctc_unit, annual_inr = parse_ctc(row["Current CTC"])
            applied_date_iso = parse_date(row["Applied Date"])

            if ctc_unit == "LPA":
                add_issue(
                    connection,
                    source,
                    row_number,
                    "CTC_UNIT_LPA",
                    "info",
                    "Decimal CTC interpreted as LPA using approved rule.",
                    row,
                )

            if applied_date_iso and date.fromisoformat(applied_date_iso) > date.today():
                add_issue(
                    connection,
                    source,
                    row_number,
                    "FUTURE_DATED_APPLICATION",
                    "medium",
                    "Application date is after the pipeline run date; retained and flagged.",
                    row,
                )

            save_source_record(connection, person_id, source, row_number, "ingested", row)

            connection.execute(
                """
                INSERT INTO naukri_profiles
                (person_id, experience_years, current_ctc_raw, current_ctc_unit,
                 current_ctc_annual_inr, applied_date_raw, applied_date_iso, skills_raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    float(row["Experience (Years)"]) if row["Experience (Years)"] else None,
                    raw_ctc,
                    ctc_unit,
                    annual_inr,
                    row["Applied Date"],
                    applied_date_iso,
                    row["Skills"],
                ),
            )


def ingest_gig_workers(connection):
    source = "source2_gig_workers.csv"

    with (DATA_DIR / source).open(encoding="utf-8-sig", newline="") as file:
        for row_number, row in enumerate(csv.DictReader(file), start=2):
            values = [value for value in row.values() if value and value.strip()]

            if not values:
                add_issue(
                    connection,
                    source,
                    row_number,
                    "BLANK_ROW",
                    "low",
                    "Blank row excluded from ingestion.",
                    row,
                )
                save_source_record(connection, None, source, row_number, "quarantined", row)
                continue

            email = row["email_id"].strip()

            if "@" not in email:
                add_issue(
                    connection,
                    source,
                    row_number,
                    "MALFORMED_SHIFTED_ROW",
                    "high",
                    "Columns are shifted; quarantined instead of auto-repairing.",
                    row,
                )
                save_source_record(connection, None, source, row_number, "quarantined", row)
                continue

            person_id = get_or_create_person(
                connection,
                row["worker_name"],
                row["email_id"],
                "",
                row["location"],
                source,
                row_number,
                row,
            )

            rate_raw, rate_amount, rate_period = parse_rate(row["rate"])
            save_source_record(connection, person_id, source, row_number, "ingested", row)

            connection.execute(
                """
                INSERT INTO gig_profiles
                (person_id, rate_raw, rate_amount, rate_period, worker_status, skills_raw)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    rate_raw,
                    rate_amount,
                    rate_period,
                    row["status"].strip().lower(),
                    row["skill_tags"],
                ),
            )


def ingest_cbnexus(connection):
    source = "source3_cbnexus_contacts.csv"

    with (DATA_DIR / source).open(
        encoding="utf-8-sig",
        newline=""
    ) as file:

        for row_number, row in enumerate(
            csv.DictReader(file),
            start=2
        ):

            if row["Name"].strip() == "Name":
                add_issue(
                    connection,
                    source,
                    row_number,
                    "EMBEDDED_HEADER_ROW",
                    "high",
                    "Repeated header row excluded from ingestion.",
                    row,
                )

                save_source_record(
                    connection,
                    None,
                    source,
                    row_number,
                    "quarantined",
                    row,
                )

                continue

            person_id = get_or_create_person(
                connection,
                row["Name"],
                "",
                row["Phone Number"],
                row["City"],
                source,
                row_number,
                row,
            )

            verified_raw = row["Verified"].strip().lower()

            if verified_raw in {"y", "yes"}:
                verified = 1

            elif verified_raw in {"n", "no"}:
                verified = 0

            else:
                verified = None

                add_issue(
                    connection,
                    source,
                    row_number,
                    "INVALID_VERIFIED_VALUE",
                    "medium",
                    f"Unexpected Verified value: {row['Verified']!r}",
                    row,
                )

            save_source_record(
                connection,
                person_id,
                source,
                row_number,
                "ingested",
                row,
            )

            connection.execute(
                """
                INSERT INTO cbnexus_profiles
                (person_id, verified, projects_completed)
                VALUES (?, ?, ?)
                """,
                (
                    person_id,
                    verified,
                    int(row["Projects Completed"]),
                ),
            )

def write_summary(connection):
    REPORTS_DIR.mkdir(exist_ok=True)

    summary = {
        "database": str(DB_PATH.relative_to(PROJECT_ROOT)),
        "people": connection.execute("SELECT COUNT(*) FROM people").fetchone()[0],
        "source_records": connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0],
        "quarantined_records": connection.execute(
            "SELECT COUNT(*) FROM source_records WHERE ingestion_status = 'quarantined'"
        ).fetchone()[0],
        "quality_issues": connection.execute("SELECT COUNT(*) FROM quality_issues").fetchone()[0],
    }

    (REPORTS_DIR / "etl_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print("\nPipeline completed successfully.")
    for label, value in summary.items():
        print(f"{label}: {value}")


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()

    connection = sqlite3.connect(DB_PATH)
    create_schema(connection)

    ingest_naukri(connection)
    ingest_gig_workers(connection)
    ingest_cbnexus(connection)

    connection.commit()
    write_summary(connection)
    connection.close()


if __name__ == "__main__":
    main()