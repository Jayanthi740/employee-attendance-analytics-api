import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Body
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb://localhost:27017/"
)

MONGO_DB = os.getenv(
    "MONGO_DB",
    "attendance_db"
)

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    tz_aware=True
)

db = client[MONGO_DB]

employees_collection = db["employees"]
attendance_collection = db["attendance_logs"]

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

ALL_STATUSES = [
    "PRESENT",
    "WFH",
    "ON_DUTY",
    "ABSENT",
    "LEAVE"
]

EMP_CODE_PATTERN = re.compile(r"^EMP\d{4,6}$")

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0"
)


# ============================================================
# COMMON HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def to_epoch_ms(value):
    """Convert a datetime to epoch milliseconds."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return int(value.timestamp() * 1000)


def from_epoch_ms(value):
    """Convert epoch milliseconds to an aware UTC datetime."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(
            status_code=422,
            detail="Timestamp must be an integer in epoch milliseconds"
        )

    if not 100000000000 <= value <= 4102444800000:
        raise HTTPException(
            status_code=422,
            detail="Invalid epoch milliseconds"
        )

    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(
            status_code=422,
            detail="Invalid timestamp"
        )


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)

    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP
        )
    )


def parse_date(value, field_name="date"):
    if not isinstance(value, str):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD format"
        )

    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD format"
        )

    if parsed.isoformat() != value:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD format"
        )

    return parsed


def parse_month(value):
    if not isinstance(value, str):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format"
        )

    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format"
        )

    if parsed.strftime("%Y-%m") != value:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format"
        )

    first = date(parsed.year, parsed.month, 1)
    last = date(
        parsed.year,
        parsed.month,
        monthrange(parsed.year, parsed.month)[1]
    )

    return first, last


def next_month_start(first):
    if first.month == 12:
        return date(first.year + 1, 1, 1)

    return date(first.year, first.month + 1, 1)


def normalize_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()

    if isinstance(value, date):
        return value.isoformat()

    return str(value)


def normalize_datetime(value):
    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value


def employee_response(employee):
    if not employee:
        return None

    result = {
        key: value
        for key, value in employee.items()
        if key != "_id"
    }

    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = to_epoch_ms(result["created_at"])

    return result


def attendance_response(record):
    if not record:
        return None

    result = {
        key: value
        for key, value in record.items()
        if key != "_id"
    }

    result["date"] = normalize_date(result.get("date"))

    for field in ("punch_in", "punch_out"):
        if isinstance(result.get(field), datetime):
            result[field] = to_epoch_ms(result[field])

    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])

    return result


def validate_emp_code(emp_code):
    if not isinstance(emp_code, str) or not EMP_CODE_PATTERN.fullmatch(emp_code):
        raise HTTPException(
            status_code=422,
            detail="emp_code must match EMP followed by 4 to 6 digits"
        )


def validate_status(status, allowed=None):
    allowed = allowed or ALL_STATUSES

    if status not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status. Allowed values: {allowed}"
        )


def get_employee_or_404(emp_code):
    employee = employees_collection.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    return employee


def validate_pagination(page, page_size):
    if page < 1:
        raise HTTPException(
            status_code=422,
            detail="page must be at least 1"
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100"
        )


def parse_shift(employee):
    try:
        start = datetime.strptime(
            employee.get("shift_start", "09:30"),
            "%H:%M"
        ).time()

        end = datetime.strptime(
            employee.get("shift_end", "18:30"),
            "%H:%M"
        ).time()

        return start, end
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Employee shift times must use HH:MM format"
        )


def calculate_attendance_date(punch_in, shift_start, shift_end):
    """
    R1:
    Attendance date is the IST punch-in date, except when an
    overnight shift ends the following day and the punch-in
    occurs before that shift's end time.
    """
    local = punch_in.astimezone(IST)
    result = local.date()

    if shift_end <= shift_start and local.time() < shift_end:
        result -= timedelta(days=1)

    return result


def calculate_late_minutes(punch_in, attendance_date, shift_start, shift_end):
    """
    R2:
    More than 10 minutes late triggers a late arrival.
    The value is the whole number of minutes after shift start,
    not the number of minutes after the grace period.
    """
    start_dt = datetime.combine(
        attendance_date,
        shift_start,
        tzinfo=IST
    )

    end_dt = datetime.combine(
        attendance_date,
        shift_end,
        tzinfo=IST
    )

    if shift_end <= shift_start:
        end_dt += timedelta(days=1)

    local_punch = punch_in.astimezone(IST)

    # A punch before the scheduled shift start is not late.
    if local_punch < start_dt:
        return 0

    elapsed_seconds = (
        local_punch - start_dt
    ).total_seconds()

    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)


def calculate_attendance_metrics(
    employee,
    attendance_date,
    punch_in,
    punch_out
):
    """
    Calculate work hours, overtime and half-day status.
    """
    if punch_in is None or punch_out is None:
        return {
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False
        }

    punch_in = normalize_datetime(punch_in)
    punch_out = normalize_datetime(punch_out)

    duration_seconds = (
        punch_out - punch_in
    ).total_seconds()

    if duration_seconds <= 0:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in"
        )

    if duration_seconds > 86400:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in"
        )

    work_hours = round_half_up(
        duration_seconds / 3600,
        2
    )

    shift_start, shift_end = parse_shift(employee)

    start_dt = datetime.combine(
        attendance_date,
        shift_start,
        tzinfo=IST
    )

    end_dt = datetime.combine(
        attendance_date,
        shift_end,
        tzinfo=IST
    )

    if shift_end <= shift_start:
        end_dt += timedelta(days=1)

    overtime_seconds = (
        punch_out.astimezone(UTC)
        - end_dt.astimezone(UTC)
    ).total_seconds()

    if overtime_seconds >= 1800:
        overtime_minutes = int(overtime_seconds // 60)
    else:
        overtime_minutes = 0

    return {
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": work_hours < 4.50
    }


def validate_attendance_punches(punch_in, punch_out):
    if punch_in is not None and punch_out is not None:
        punch_in = normalize_datetime(punch_in)
        punch_out = normalize_datetime(punch_out)

        duration = (punch_out - punch_in).total_seconds()

        if duration <= 0:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in"
            )

        if duration > 86400:
            raise HTTPException(
                status_code=422,
                detail="Punch duration cannot exceed 24 hours"
            )


def build_attendance_month_match(emp_code, first_day, last_day):
    return {
        "emp_code": emp_code,
        "date": {
            "$gte": first_day.isoformat(),
            "$lte": last_day.isoformat()
        }
    }


def working_days_count(joined_on, first_day, last_day):
    joined = parse_date(
        normalize_date(joined_on),
        "joined_on"
    )

    current = max(joined, first_day)
    total = 0

    while current <= last_day:
        if current.weekday() < 5:
            total += 1

        current += timedelta(days=1)

    return total


def database_explain(collection_name, pipeline):
    """
    Return MongoDB execution statistics for an aggregation.
    """
    result = db.command({
        "explain": {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {}
        },
        "verbosity": "executionStats"
    })

    return result


# ============================================================
# INDEXES
# ============================================================

@app.on_event("startup")
def startup_event():
    """
    Indexes are created at startup. Existing conflicting index
    definitions must be inspected and reconciled rather than
    deleting existing data.
    """
    employees_collection.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="emp_code_1"
    )

    attendance_collection.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING)
        ],
        unique=True,
        name="emp_code_1_date_1"
    )

    # Helpful indexes for common filters and sorting.
    attendance_collection.create_index(
        [
            ("date", DESCENDING),
            ("emp_code", ASCENDING)
        ],
        name="date_desc_emp_code_asc"
    )

    attendance_collection.create_index(
        [
            ("status", ASCENDING),
            ("date", DESCENDING)
        ],
        name="status_date"
    )

    employees_collection.create_index(
        [
            ("department", ASCENDING),
            ("joined_on", ASCENDING)
        ],
        name="department_joined_on"
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health_check():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable"
        )


@app.get("/health/db")
def database_health_check():
    try:
        client.admin.command("ping")

        return {
            "status": "ok",
            "database": MONGO_DB,
            "mongodb": "connected"
        }

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable"
        )


# ============================================================
# EMPLOYEE MANAGEMENT
# ============================================================

@app.post("/employees", status_code=201)
def create_employee(employee: dict = Body(...)):
    required_fields = [
        "emp_code",
        "name",
        "email",
        "department",
        "joined_on"
    ]

    missing = [
        field
        for field in required_fields
        if field not in employee
        or employee[field] is None
        or employee[field] == ""
    ]

    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required fields: {missing}"
        )

    validate_emp_code(employee["emp_code"])

    for field in ("name", "email", "department"):
        if not isinstance(employee[field], str) or not employee[field].strip():
            raise HTTPException(
                status_code=422,
                detail=f"{field} must be a non-empty string"
            )

    joined = parse_date(employee["joined_on"], "joined_on")

    shift_start = employee.get("shift_start", "09:30")
    shift_end = employee.get("shift_end", "18:30")

    try:
        start_time = datetime.strptime(shift_start, "%H:%M").time()
        end_time = datetime.strptime(shift_end, "%H:%M").time()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Shift times must use HH:MM format"
        )

    if start_time == end_time:
        raise HTTPException(
            status_code=422,
            detail="shift_start and shift_end must differ"
        )

    email = employee["email"].strip()

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(
            status_code=422,
            detail="Invalid email address"
        )

    employee_doc = {
        "emp_code": employee["emp_code"],
        "name": employee["name"].strip(),
        "email": email,
        "department": employee["department"].strip(),
        "joined_on": joined.isoformat(),
        "shift_start": shift_start,
        "shift_end": shift_end,
        "created_at": now_ist()
    }

    try:
        employees_collection.insert_one(employee_doc)

        return employee_response(employee_doc)

    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Employee code already exists"
        )

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Failed to create employee"
        )


@app.get("/employees")
def get_all_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    query = {}

    if department is not None:
        query["department"] = department

    try:
        total = employees_collection.count_documents(query)

        cursor = (
            employees_collection
            .find(query, {"_id": 0})
            .sort("emp_code", ASCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        items = [
            employee_response(item)
            for item in cursor
        ]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size
        }

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve employees"
        )


@app.get("/employees/{emp_code}")
def get_employee(emp_code: str):
    try:
        employee = employees_collection.find_one(
            {"emp_code": emp_code},
            {"_id": 0}
        )

        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found"
            )

        return employee_response(employee)

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve employee"
        )


# ============================================================
# ATTENDANCE: PUNCH-IN
# ============================================================

@app.post("/attendance/punch-in", status_code=201)
def punch_in(data: dict = Body(...)):
    emp_code = data.get("emp_code")
    validate_emp_code(emp_code)

    status = data.get("status", "PRESENT")
    validate_status(status, PRESENT_STATUSES)

    punched_at = data.get("punched_at")

    if punched_at is None:
        punch_time = now_ist()
    else:
        punch_time = from_epoch_ms(punched_at).astimezone(IST)

    try:
        employee = get_employee_or_404(emp_code)

        shift_start, shift_end = parse_shift(employee)

        attendance_date = calculate_attendance_date(
            punch_time,
            shift_start,
            shift_end
        )

        late_minutes = calculate_late_minutes(
            punch_time,
            attendance_date,
            shift_start,
            shift_end
        )

        record = {
            "emp_code": emp_code,
            "date": attendance_date.isoformat(),
            "status": status,
            "punch_in": punch_time,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        }

        attendance_collection.insert_one(record)

        return attendance_response(record)

    except HTTPException:
        raise

    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already exists for this employee and date"
        )

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Database error during punch-in"
        )


# ============================================================
# ATTENDANCE: PUNCH-OUT
# ============================================================

@app.post("/attendance/punch-out")
def punch_out(data: dict = Body(...)):
    emp_code = data.get("emp_code")
    validate_emp_code(emp_code)

    punched_at = data.get("punched_at")

    if punched_at is None:
        punch_time = now_ist()
    else:
        punch_time = from_epoch_ms(punched_at).astimezone(IST)

    try:
        employee = get_employee_or_404(emp_code)

        record = attendance_collection.find_one(
            {
                "emp_code": emp_code,
                "punch_in": {
                    "$ne": None,
                    "$lte": punch_time
                },
                "punch_out": None
            },
            sort=[("punch_in", DESCENDING)]
        )

        if record is None:
            closed_record = attendance_collection.find_one(
                {
                    "emp_code": emp_code,
                    "punch_in": {"$lte": punch_time},
                    "punch_out": {"$ne": None}
                },
                sort=[("punch_in", DESCENDING)]
            )

            if closed_record:
                raise HTTPException(
                    status_code=409,
                    detail="Attendance record is already punched out"
                )

            raise HTTPException(
                status_code=404,
                detail="No open punch-in record found"
            )

        punch_in_time = normalize_datetime(record["punch_in"])
        punch_out_time = normalize_datetime(punch_time)

        duration = (
            punch_out_time - punch_in_time
        ).total_seconds()

        if duration <= 0:
            raise HTTPException(
                status_code=422,
                detail="Punch-out must be after punch-in"
            )

        if duration > 86400:
            raise HTTPException(
                status_code=422,
                detail="Punch-out cannot be more than 24 hours after punch-in"
            )

        attendance_date = parse_date(record["date"])

        metrics = calculate_attendance_metrics(
            employee,
            attendance_date,
            punch_in_time,
            punch_out_time
        )

        result = attendance_collection.update_one(
            {
                "_id": record["_id"],
                "punch_out": None
            },
            {
                "$set": {
                    "punch_out": punch_out_time,
                    **metrics
                }
            }
        )

        if result.modified_count != 1:
            raise HTTPException(
                status_code=409,
                detail="Attendance record was updated by another request"
            )

        updated = attendance_collection.find_one(
            {"_id": record["_id"]}
        )

        return attendance_response(updated)

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Database error during punch-out"
        )


# ============================================================
# ATTENDANCE: LIST AND FILTER
# ============================================================

@app.get("/attendance")
def get_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if status is not None:
        validate_status(status)
        query["status"] = status

    if date_from is not None:
        parse_date(date_from, "date_from")

    if date_to is not None:
        parse_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to"
        )

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = date_from

        if date_to:
            query["date"]["$lte"] = date_to

    try:
        total = attendance_collection.count_documents(query)

        records = list(
            attendance_collection
            .find(query, {"_id": 0})
            .sort([
                ("date", DESCENDING),
                ("emp_code", ASCENDING)
            ])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        return {
            "items": [
                attendance_response(record)
                for record in records
            ],
            "total": total,
            "page": page,
            "page_size": page_size
        }

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Database error while retrieving attendance"
        )


# ============================================================
# ATTENDANCE: PATCH / REGULARIZATION
# ============================================================

@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(
    emp_code: str,
    attendance_date: str,
    correction: dict = Body(...)
):
    validate_emp_code(emp_code)
    target_date = parse_date(attendance_date, "date")

    reason = correction.get("reason")
    regularized_by = correction.get("regularized_by")

    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(
            status_code=422,
            detail="reason is required"
        )

    if not isinstance(regularized_by, str) or not regularized_by.strip():
        raise HTTPException(
            status_code=422,
            detail="regularized_by is required"
        )

    allowed_fields = {
        "status",
        "punch_in",
        "punch_out",
        "reason",
        "regularized_by"
    }

    unexpected = set(correction.keys()) - allowed_fields

    if unexpected:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported fields: {sorted(unexpected)}"
        )

    if not any(
        field in correction
        for field in ("status", "punch_in", "punch_out")
    ):
        raise HTTPException(
            status_code=422,
            detail="At least one attendance field must be corrected"
        )

    try:
        employee = get_employee_or_404(emp_code)

        record = attendance_collection.find_one({
            "emp_code": emp_code,
            "date": target_date.isoformat()
        })

        if record is None:
            raise HTTPException(
                status_code=404,
                detail="Attendance record not found"
            )

        old_status = record["status"]
        old_in = record.get("punch_in")
        old_out = record.get("punch_out")

        new_status = correction.get("status", old_status)

        validate_status(new_status)

        new_in = old_in
        new_out = old_out

        if "punch_in" in correction:
            new_in = (
                None
                if correction["punch_in"] is None
                else from_epoch_ms(correction["punch_in"])
            )

        if "punch_out" in correction:
            new_out = (
                None
                if correction["punch_out"] is None
                else from_epoch_ms(correction["punch_out"])
            )

        # ABSENT and LEAVE must not retain punches.
        if new_status in ("ABSENT", "LEAVE"):
            new_in = None
            new_out = None

        elif new_in is None:
            raise HTTPException(
                status_code=422,
                detail="PRESENT, WFH and ON_DUTY require punch_in"
            )

        validate_attendance_punches(new_in, new_out)

        new_late = record.get("late_minutes", 0)
        new_work_hours = None
        new_overtime = 0
        new_half_day = False

        if new_in is not None:
            shift_start, shift_end = parse_shift(employee)

            computed_date = calculate_attendance_date(
                new_in.astimezone(IST),
                shift_start,
                shift_end
            )

            if computed_date != target_date:
                raise HTTPException(
                    status_code=422,
                    detail="Corrected punch_in does not belong to the requested attendance date"
                )

            new_late = calculate_late_minutes(
                new_in,
                target_date,
                shift_start,
                shift_end
            )

        if new_in is not None and new_out is not None:
            metrics = calculate_attendance_metrics(
                employee,
                target_date,
                new_in,
                new_out
            )

            new_work_hours = metrics["work_hours"]
            new_overtime = metrics["overtime_minutes"]
            new_half_day = metrics["half_day"]

        changes = {}

        if old_status != new_status:
            changes["status"] = {
                "from": old_status,
                "to": new_status
            }

        if old_in != new_in:
            changes["punch_in"] = {
                "from": to_epoch_ms(old_in),
                "to": to_epoch_ms(new_in)
            }

        if old_out != new_out:
            changes["punch_out"] = {
                "from": to_epoch_ms(old_out),
                "to": to_epoch_ms(new_out)
            }

        if not changes:
            raise HTTPException(
                status_code=422,
                detail="The correction does not change the record"
            )

        history_entry = {
            "at": now_ist(),
            "by": regularized_by.strip(),
            "reason": reason.strip(),
            "changes": changes
        }

        update_fields = {
            "status": new_status,
            "punch_in": new_in,
            "punch_out": new_out,
            "late_minutes": new_late,
            "work_hours": new_work_hours,
            "overtime_minutes": new_overtime,
            "half_day": new_half_day
        }

        result = attendance_collection.update_one(
            {"_id": record["_id"]},
            {
                "$set": update_fields,
                "$push": {"history": history_entry}
            }
        )

        if result.modified_count != 1:
            raise HTTPException(
                status_code=409,
                detail="Attendance record changed during correction"
            )

        updated = attendance_collection.find_one(
            {"_id": record["_id"]}
        )

        return attendance_response(updated)

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Database error during attendance correction"
        )


# ============================================================
# MONTHLY EMPLOYEE ANALYTICS
# ============================================================

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str
):
    first_day, last_day = parse_month(month)

    try:
        employee = employees_collection.find_one(
            {"emp_code": emp_code}
        )

        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found"
            )

        joined = parse_date(
            normalize_date(employee["joined_on"]),
            "joined_on"
        )

        working_days = working_days_count(
            joined,
            first_day,
            last_day
        )

        pipeline = [
            {
                "$match": build_attendance_month_match(
                    emp_code,
                    first_day,
                    last_day
                )
            },
            {
                "$addFields": {
                    "_date": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d"
                        }
                    }
                }
            },
            {
                "$addFields": {
                    "_weekday": {
                        "$dayOfWeek": "$_date"
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", PRESENT_STATUSES]},
                                        {"$gte": ["$_weekday", 2]},
                                        {"$lte": ["$_weekday", 6]},
                                        {"$gte": ["$_date", datetime.combine(max(joined, first_day), time.min)]}
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    }
                }
            }
        ]

        rows = list(attendance_collection.aggregate(pipeline))

        stats = rows[0] if rows else {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }

        attendance_pct = (
            round_half_up(stats["present_days"] / working_days * 100, 2)
            if working_days > 0
            else None
        )

        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": stats["present_days"],
            "leave_days": stats["leave_days"],
            "late_count": stats["late_count"],
            "total_late_minutes": stats["total_late_minutes"],
            "total_overtime_minutes": stats["total_overtime_minutes"],
            "attendance_pct": attendance_pct
        }

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Database error while calculating monthly analytics"
        )


# ============================================================
# DEPARTMENT SUMMARY
# ============================================================

@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None
):
    first_day, last_day = parse_month(month)

    try:
        employee_match = {
            "joined_on": {"$lte": last_day.isoformat()}
        }

        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", first_day.isoformat()]},
                                        {"$lte": ["$date", last_day.isoformat()]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$addFields": {
                    "_date": {
                        "$cond": [
                            {"$ne": ["$logs.date", None]},
                            {
                                "$dateFromString": {
                                    "dateString": "$logs.date",
                                    "format": "%Y-%m-%d"
                                }
                            },
                            None
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", PRESENT_STATUSES]},
                                        {"$ne": ["$_date", None]},
                                        {"$gte": [{"$dayOfWeek": "$_date"}, 2]},
                                        {"$lte": [{"$dayOfWeek": "$_date"}, 6]}
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$logs.half_day", True]},
                                        0.5,
                                        1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    "work_hours_values": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", PRESENT_STATUSES]},
                                        {"$ne": ["$logs.work_hours", None]}
                                    ]
                                },
                                "$logs.work_hours",
                                None
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                1,
                                0
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1,
                                0
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1,
                                0
                            ]
                        }
                    }
                }
            },
            {
                "$addFields": {
                    "headcount": {"$size": "$headcount"},
                    "valid_hours": {
                        "$filter": {
                            "input": "$work_hours_values",
                            "as": "hours",
                            "cond": {"$ne": ["$$hours", None]}
                        }
                    }
                }
            },
            {
                "$addFields": {
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": [{"$size": "$valid_hours"}, 0]},
                            {"$avg": "$valid_hours"},
                            None
                        ]
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "avg_work_hours": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1
                }
            },
            {"$sort": {"department": 1}}
        ]

        results = list(employees_collection.aggregate(pipeline))

        for item in results:
            if item.get("avg_work_hours") is not None:
                item["avg_work_hours"] = round_half_up(
                    item["avg_work_hours"],
                    2
                )

        return results

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Database error while calculating department summary"
        )


# ============================================================
# LATE ARRIVAL LEADERBOARD
# ============================================================

@app.get("/analytics/leaderboard/late")
def late_arrival_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    first_day, last_day = parse_month(month)

    try:
        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": first_day.isoformat(),
                        "$lte": last_day.isoformat()
                    }
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee"
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append({
                "$match": {
                    "employee.department": department
                }
            })

        pipeline.extend([
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {
                        "total_late_minutes": -1
                    },
                    "output": {
                        "rank": {
                            "$rank": {}
                        }
                    }
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "rank": 1
                }
            },
            {
                "$sort": {
                    "rank": 1,
                    "emp_code": 1
                }
            }
        ])

        leaderboard = list(
            attendance_collection.aggregate(pipeline)
        )

        return {
            "month": month,
            "leaderboard": leaderboard
        }

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Database error while calculating leaderboard"
        )


# ============================================================
# DEPARTMENT TREND
# ============================================================

@app.get("/analytics/departments/{department}/trend")
def department_attendance_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    start_date = parse_date(from_date, "from")
    end_date = parse_date(to_date, "to")

    if end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail="to must be on or after from"
        )

    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days"
        )

    try:
        if not employees_collection.find_one(
            {"department": department},
            {"_id": 1}
        ):
            raise HTTPException(
                status_code=404,
                detail="Department not found"
            )

        start_dt = datetime.combine(
            start_date,
            time.min,
            tzinfo=IST
        )

        end_dt = datetime.combine(
            end_date,
            time.min,
            tzinfo=IST
        )

        # Start with one seed date, then $densify fills the
        # entire requested range, including days without logs.
        pipeline = [
            {
                "$documents": [
                    {"date_dt": start_dt}
                ]
            },
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [
                            start_dt,
                            end_dt + timedelta(days=1)
                        ]
                    }
                }
            },
            {
                "$addFields": {
                    "date_string": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date_dt",
                            "timezone": "Asia/Kolkata"
                        }
                    },
                    "is_working_day": {
                        "$and": [
                            {"$gte": [{"$dayOfWeek": "$date_dt"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$date_dt"}, 6]}
                        ]
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {
                        "dept": department,
                        "day": "$date_string"
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", "$$dept"]},
                                        {"$lte": ["$joined_on", "$$day"]}
                                    ]
                                }
                            }
                        },
                        {"$project": {"_id": 0, "emp_code": 1}}
                    ],
                    "as": "active_employees"
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "day": "$date_string",
                        "codes": "$active_employees.emp_code"
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$date", "$$day"]},
                                        {"$in": ["$emp_code", "$$codes"]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {
                "$addFields": {
                    "headcount": {"$size": "$active_employees"},
                    "present_count": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$logs",
                                        "as": "log",
                                        "cond": {
                                            "$in": [
                                                "$$log.status",
                                                PRESENT_STATUSES
                                            ]
                                        }
                                    }
                                },
                                "as": "log",
                                "in": {
                                    "$cond": [
                                        {"$eq": ["$$log.half_day", True]},
                                        0.5,
                                        1
                                    ]
                                }
                            }
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$gt": [
                                        {"$ifNull": ["$$log.late_minutes", 0]},
                                        0
                                    ]
                                }
                            }
                        }
                    }
                }
            },
            {
                "$addFields": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]}
                                ]
                            },
                            {
                                "$round": [
                                    {
                                        "$divide": [
                                            "$present_count",
                                            "$headcount"
                                        ]
                                    },
                                    4
                                ]
                            },
                            None
                        ]
                    }
                }
            },
            {"$sort": {"date_dt": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"date_dt": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, 0]
                            }
                        }
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": "$date_string",
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": {
                        "$cond": [
                            {"$eq": ["$moving_avg_7d", None]},
                            None,
                            {"$round": ["$moving_avg_7d", 4]}
                        ]
                    }
                }
            },
            {"$sort": {"date": 1}}
        ]

        return list(db.aggregate(pipeline))

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Database error while calculating department trend"
        )


# ============================================================
# ADMIN: MONGODB EXPLAIN
# ============================================================

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=50)
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    }

    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of {sorted(allowed)}"
        )

    try:
        if endpoint == "attendance_list":
            query = {}

            if emp_code:
                query["emp_code"] = emp_code

            if date_from:
                parse_date(date_from, "date_from")
                query.setdefault("date", {})["$gte"] = date_from

            if date_to:
                parse_date(date_to, "date_to")
                query.setdefault("date", {})["$lte"] = date_to

            pipeline = [
                {"$match": query},
                {
                    "$sort": {
                        "date": -1,
                        "emp_code": 1
                    }
                },
                {"$skip": (page - 1) * page_size},
                {"$limit": page_size}
            ]

            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": database_explain(
                    "attendance_logs",
                    pipeline
                )
            }

        if endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(
                    status_code=422,
                    detail="emp_code and month are required"
                )

            first, last = parse_month(month)

            pipeline = [
                {
                    "$match": build_attendance_month_match(
                        emp_code,
                        first,
                        last
                    )
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {
                            "$sum": {"$ifNull": ["$late_minutes", 0]}
                        }
                    }
                }
            ]

            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": database_explain(
                    "attendance_logs",
                    pipeline
                )
            }

        if endpoint == "department_summary":
            if not month:
                raise HTTPException(
                    status_code=422,
                    detail="month is required"
                )

            first, last = parse_month(month)

            match = {
                "joined_on": {"$lte": last.isoformat()}
            }

            if department:
                match["department"] = department

            pipeline = [
                {"$match": match},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "logs"
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "emp_code": 1,
                        "department": 1,
                        "logs": 1
                    }
                }
            ]

            return {
                "endpoint": endpoint,
                "collection": "employees",
                "explain": database_explain(
                    "employees",
                    pipeline
                )
            }

        if endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(
                    status_code=422,
                    detail="month is required"
                )

            first, last = parse_month(month)

            pipeline = [
                {
                    "$match": {
                        "date": {
                            "$gte": first.isoformat(),
                            "$lte": last.isoformat()
                        }
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {
                            "$sum": {"$ifNull": ["$late_minutes", 0]}
                        }
                    }
                },
                {
                    "$sort": {
                        "total_late_minutes": -1,
                        "_id": 1
                    }
                },
                {"$limit": limit}
            ]

            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": database_explain(
                    "attendance_logs",
                    pipeline
                )
            }

        if endpoint == "department_trend":
            if not department or not date_from or not date_to:
                raise HTTPException(
                    status_code=422,
                    detail="department, date_from and date_to are required"
                )

            parse_date(date_from, "date_from")
            parse_date(date_to, "date_to")

            pipeline = [
                {
                    "$match": {
                        "department": department
                    }
                },
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "logs"
                    }
                }
            ]

            return {
                "endpoint": endpoint,
                "collection": "employees",
                "explain": database_explain(
                    "employees",
                    pipeline
                )
            }

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="MongoDB explain failed"
        )