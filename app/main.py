"""Employee Attendance & Analytics API.

Run from the repository root with ``uvicorn app.main:app --port 8000``.
Persisted instants are UTC BSON datetimes. Calendar dates and shift times stay
as strings because the contract defines them as IST calendar values.
"""

import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB environment variables are required")
client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=2000,
    tz_aware=True,
    tzinfo=timezone.utc,
)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

EpochMillis = Annotated[
    int,
    Field(
        strict=True,
        ge=EPOCH_MIN,
        le=EPOCH_MAX,
        json_schema_extra={"format": "int64"},
    ),
]
AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
ExplainEndpoint = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


def create_indexes() -> None:
    """Create correctness and query indexes idempotently at startup."""
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="emp_code_1"
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employees_joined_department",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_joined_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_emp_date_desc",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_status_date_emp",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("emp_code", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_emp_date",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        create_indexes()
    except ServerSelectionTimeoutError:
        # Keep the process alive so /health can report the database outage as 503.
        # A healthy application start still creates every index before serving.
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Validation, time, and serialization helpers
# ---------------------------------------------------------------------------
def validation_error(message: str) -> HTTPException:
    """Return a semantic 422 using FastAPI's validation-error body shape."""
    return HTTPException(
        status_code=422,
        detail=[
            {
                "type": "value_error",
                "loc": ["request"],
                "msg": message,
                "input": None,
            }
        ],
    )


def parse_date(value: str, field_name: str = "date") -> date:
    if not DATE_RE.fullmatch(value):
        raise validation_error(f"{field_name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise validation_error(f"{field_name} must be a valid date") from exc


def validate_month(value: str) -> None:
    if not MONTH_RE.fullmatch(value):
        raise validation_error("month must be YYYY-MM")


def month_bounds(month: str) -> tuple[str, str]:
    validate_month(month)
    year, month_number = map(int, month.split("-"))
    start = date(year, month_number, 1)
    if month_number == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month_number + 1, 1)
    return start.isoformat(), following.isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def epoch_to_utc(value: int) -> datetime:
    """Convert epoch milliseconds and truncate punch instants to whole seconds."""
    return datetime.fromtimestamp(value // 1000, tz=timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_epoch_millis(value: datetime) -> int:
    return int(ensure_utc(value).timestamp() * 1000)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = ensure_utc(punch_in).astimezone(IST)
    start_clock = time.fromisoformat(shift_start)
    end_clock = time.fromisoformat(shift_end)
    attendance_day = local.date()
    if end_clock <= start_clock and local.time() < end_clock:
        attendance_day -= timedelta(days=1)
    return attendance_day.isoformat()


def shift_instant(date_str: str, clock: str, *, next_day: bool = False) -> datetime:
    day = date.fromisoformat(date_str) + (timedelta(days=1) if next_day else timedelta())
    local = datetime.combine(day, time.fromisoformat(clock), tzinfo=IST)
    return local.astimezone(timezone.utc)


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    start = shift_instant(date_str, shift_start)
    elapsed_seconds = int((ensure_utc(punch_in) - start).total_seconds())
    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(int((ensure_utc(punch_out) - ensure_utc(punch_in)).total_seconds()))
    value = (seconds / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(value)


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    overnight = time.fromisoformat(shift_end) <= time.fromisoformat(shift_start)
    end = shift_instant(date_str, shift_end, next_day=overnight)
    seconds = int((ensure_utc(punch_out) - end).total_seconds())
    return seconds // 60 if seconds >= 30 * 60 else 0


def validate_punch_interval(punch_in: datetime, punch_out: datetime) -> None:
    duration = ensure_utc(punch_out) - ensure_utc(punch_in)
    if duration <= timedelta(0):
        raise validation_error("punched_at must be after punch_in")
    if duration > timedelta(hours=24):
        raise validation_error("punch interval cannot exceed 24 hours")


def derived_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    employee: dict[str, Any],
    date_str: str,
) -> dict[str, Any]:
    if status not in PRESENCE_STATUSES:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    if punch_in is None:
        raise validation_error("a presence status requires punch_in")
    late = compute_late_minutes(punch_in, employee["shift_start"], date_str)
    if punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }
    validate_punch_interval(punch_in, punch_out)
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(
            punch_out,
            employee["shift_start"],
            employee["shift_end"],
            date_str,
        ),
        "half_day": hours < 4.50,
    }


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_epoch_millis(value)
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items() if key != "_id"}
    return value


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "emp_code",
        "name",
        "email",
        "department",
        "shift_start",
        "shift_end",
        "joined_on",
        "created_at",
    )
    return {field: json_value(doc.get(field)) for field in fields}


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": json_value(doc.get("punch_in")),
        "punch_out": json_value(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": json_value(doc.get("history", [])),
    }


def bson_json(value: Any) -> Any:
    return json.loads(json_util.dumps(value))


def half_up_expression(expression: Any, places: int) -> dict[str, Any]:
    factor = 10**places
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [expression, factor]}, 0.5]}},
            factor,
        ]
    }


def weekday_expression(date_expression: Any) -> dict[str, Any]:
    iso_day = {
        "$isoDayOfWeek": {
            "$dateFromString": {"dateString": date_expression, "timezone": "UTC"}
        }
    }
    return {"$lte": [iso_day, 5]}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[
        str,
        Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    ]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: Annotated[str, Field(json_schema_extra={"format": "date"})]

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        if not DATE_RE.fullmatch(value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must be different")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis = Field(  # type: ignore[assignment]
        default_factory=lambda: None
    )
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis = Field(  # type: ignore[assignment]
        default_factory=lambda: None
    )


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: AttendanceStatus = Field(  # type: ignore[assignment]
        default_factory=lambda: None
    )
    punch_in: EpochMillis = Field(  # type: ignore[assignment]
        default_factory=lambda: None
    )
    punch_out: EpochMillis = Field(  # type: ignore[assignment]
        default_factory=lambda: None
    )
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for field in ("status", "punch_in", "punch_out"):
                if field in value and value[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return value


# ---------------------------------------------------------------------------
# Response models (also drive the generated OpenAPI document)
# ---------------------------------------------------------------------------
class ErrorOut(BaseModel):
    detail: str


class HealthOut(BaseModel):
    status: Literal["ok"]


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: Annotated[str, Field(json_schema_extra={"format": "date"})]
    created_at: EpochMillis


class EmployeePageOut(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int


class HistoryEntryOut(BaseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: Annotated[str, Field(json_schema_extra={"format": "date"})]
    status: AttendanceStatus
    punch_in: Optional[EpochMillis]
    punch_out: Optional[EpochMillis]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntryOut]


class AttendancePageOut(BaseModel):
    items: list[AttendanceRecordOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItemOut(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: list[DepartmentSummaryItemOut]


class LeaderboardItemOut(BaseModel):
    rank: Annotated[int, Field(ge=1)]
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: list[LeaderboardItemOut]


class TrendItemOut(BaseModel):
    date: Annotated[str, Field(json_schema_extra={"format": "date"})]
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendOut(BaseModel):
    department: str
    items: list[TrendItemOut]


class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


NOT_FOUND_RESPONSE = {"model": ErrorOut, "description": "Not found"}
CONFLICT_RESPONSE = {"model": ErrorOut, "description": "Conflict"}
UNAVAILABLE_RESPONSE = {"model": ErrorOut, "description": "Database unavailable"}


# ---------------------------------------------------------------------------
# System, employee, and attendance endpoints
# ---------------------------------------------------------------------------
@app.get(
    "/health",
    response_model=HealthOut,
    responses={503: UNAVAILABLE_RESPONSE},
    tags=["system"],
    operation_id="health",
)
def health():
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"status": "ok"}


@app.post(
    "/employees",
    status_code=201,
    response_model=EmployeeOut,
    responses={409: CONFLICT_RESPONSE},
    tags=["employees"],
    operation_id="createEmployee",
)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(doc)


@app.get(
    "/employees",
    response_model=EmployeePageOut,
    tags=["employees"],
    operation_id="listEmployees",
)
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department is not None else {}
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(item) for item in items],
        "total": db.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.post(
    "/attendance/punch-in",
    status_code=201,
    response_model=AttendanceRecordOut,
    responses={404: NOT_FOUND_RESPONSE, 409: CONFLICT_RESPONSE},
    tags=["attendance"],
    operation_id="punchIn",
)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = (
        epoch_to_utc(body.punched_at)
        if body.punched_at is not None
        else utc_now().replace(microsecond=0)
    )
    date_str = attendance_date(
        punched_at, employee["shift_start"], employee["shift_end"]
    )
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at, employee["shift_start"], date_str
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post(
    "/attendance/punch-out",
    response_model=AttendanceRecordOut,
    responses={404: NOT_FOUND_RESPONSE, 409: CONFLICT_RESPONSE},
    tags=["attendance"],
    operation_id="punchOut",
)
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = (
        epoch_to_utc(body.punched_at)
        if body.punched_at is not None
        else utc_now().replace(microsecond=0)
    )
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance record is already punched out")
    validate_punch_interval(record["punch_in"], punched_at)
    calculated = derived_fields(
        record["status"], record["punch_in"], punched_at, employee, record["date"]
    )
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_in": record["punch_in"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **calculated}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record was updated concurrently")
    return serialize_attendance(updated)


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict[str, Any]:
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise validation_error("date_from cannot be after date_to")
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        date_query: dict[str, str] = {}
        if date_from is not None:
            date_query["$gte"] = date_from
        if date_to is not None:
            date_query["$lte"] = date_to
        query["date"] = date_query
    return query


@app.get(
    "/attendance",
    response_model=AttendancePageOut,
    tags=["attendance"],
    operation_id="listAttendance",
)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[
        Optional[str], Query(json_schema_extra={"format": "date"})
    ] = None,
    date_to: Annotated[
        Optional[str], Query(json_schema_extra={"format": "date"})
    ] = None,
    status: Optional[AttendanceStatus] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    items = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(item) for item in items],
        "total": db.attendance_logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.patch(
    "/attendance/{emp_code}/{date}",
    response_model=AttendanceRecordOut,
    responses={404: NOT_FOUND_RESPONSE, 409: CONFLICT_RESPONSE},
    tags=["attendance"],
    operation_id="regularizeAttendance",
)
def regularize_attendance(
    emp_code: str,
    date: Annotated[str, Path(json_schema_extra={"format": "date"})],
    body: RegularizeIn,
):
    date_str = date
    parse_date(date_str)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if original is None:
        raise HTTPException(status_code=404, detail="attendance record not found")
    supplied = body.model_fields_set
    if body.status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & supplied):
        raise validation_error("ABSENT or LEAVE cannot be supplied with punch times")
    status = body.status if "status" in supplied else original["status"]
    punch_in_value = (
        epoch_to_utc(body.punch_in)
        if "punch_in" in supplied and body.punch_in is not None
        else original.get("punch_in")
    )
    punch_out_value = (
        epoch_to_utc(body.punch_out)
        if "punch_out" in supplied and body.punch_out is not None
        else original.get("punch_out")
    )
    if status in ("ABSENT", "LEAVE"):
        punch_in_value = None
        punch_out_value = None
    else:
        if punch_in_value is None:
            raise validation_error("a presence status requires punch_in")
        actual_date = attendance_date(
            punch_in_value, employee["shift_start"], employee["shift_end"]
        )
        if actual_date != date_str:
            raise validation_error("punch_in must belong to the record attendance date")
        if punch_out_value is not None:
            validate_punch_interval(punch_in_value, punch_out_value)
    final_values = {
        "status": status,
        "punch_in": punch_in_value,
        "punch_out": punch_out_value,
        **derived_fields(status, punch_in_value, punch_out_value, employee, date_str),
    }
    tracked_fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    )
    defaults = {
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    changes: dict[str, dict[str, Any]] = {}
    for field in tracked_fields:
        old = original.get(field, defaults.get(field))
        new = final_values[field]
        if old != new:
            changes[field] = {"from": old, "to": new}
    if not changes:
        raise validation_error("regularization changes nothing")
    history_entry = {
        "at": utc_now(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    optimistic_filter: dict[str, Any] = {"_id": original["_id"]}
    for field in (*tracked_fields, "history"):
        optimistic_filter[field] = (
            original[field] if field in original else {"$exists": False}
        )
    updated = db.attendance_logs.find_one_and_update(
        optimistic_filter,
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record was updated concurrently")
    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# Aggregation pipeline builders
# ---------------------------------------------------------------------------
def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    presence_condition = {
        "$and": [
            {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
            weekday_expression("$$log.date"),
        ]
    }
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code", "joined": "$joined_on"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lt": ["$date", end]},
                                    {"$gte": ["$date", "$$joined"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "working_days": {
                    "$let": {
                        "vars": {
                            "period_start": {
                                "$max": [
                                    {"$dateFromString": {"dateString": start}},
                                    {"$dateFromString": {"dateString": "$joined_on"}},
                                ]
                            },
                            "period_end": {"$dateFromString": {"dateString": end}},
                        },
                        "in": {
                            "$size": {
                                "$filter": {
                                    "input": {
                                        "$map": {
                                            "input": {
                                                "$range": [
                                                    0,
                                                    {
                                                        "$max": [
                                                            0,
                                                            {
                                                                "$dateDiff": {
                                                                    "startDate": "$$period_start",
                                                                    "endDate": "$$period_end",
                                                                    "unit": "day",
                                                                }
                                                            },
                                                        ]
                                                    },
                                                ]
                                            },
                                            "as": "offset",
                                            "in": {
                                                "$dateAdd": {
                                                    "startDate": "$$period_start",
                                                    "unit": "day",
                                                    "amount": "$$offset",
                                                }
                                            },
                                        }
                                    },
                                    "as": "day",
                                    "cond": {"$lte": [{"$isoDayOfWeek": "$$day"}, 5]},
                                }
                            }
                        },
                    }
                },
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    presence_condition,
                                    {
                                        "$cond": [
                                            {"$ifNull": ["$$log.half_day", False]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.overtime_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        half_up_expression(
                            {
                                "$multiply": [
                                    {"$divide": ["$present_days", "$working_days"]},
                                    100,
                                ]
                            },
                            2,
                        ),
                        None,
                    ]
                },
            }
        },
    ]


def department_summary_pipeline(
    month: str, department: Optional[str]
) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    eligible: dict[str, Any] = {"joined_on": {"$lt": end}}
    if department is not None:
        eligible["department"] = department
    present_and_weekday = {
        "$and": [
            {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
            weekday_expression("$$log.date"),
        ]
    }
    work_condition = {
        "$and": [
            {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
            {"$ne": [{"$ifNull": ["$$log.work_hours", None]}, None]},
        ]
    }
    return [
        {"$match": eligible},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code", "joined": "$joined_on"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lt": ["$date", end]},
                                    {"$gte": ["$date", "$$joined"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    present_and_weekday,
                                    {
                                        "$cond": [
                                            {"$ifNull": ["$$log.half_day", False]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "work_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [work_condition, "$$log.work_hours", 0]},
                        }
                    }
                },
                "work_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": work_condition,
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_sum": {"$sum": "$work_sum"},
                "work_count": {"$sum": "$work_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_count", 0]},
                        half_up_expression(
                            {"$divide": ["$work_sum", "$work_count"]}, 2
                        ),
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str]
) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(
    department: str, date_from: str, date_to: str
) -> list[dict[str, Any]]:
    start_date = parse_date(date_from, "from")
    end_date = parse_date(date_to, "to")
    if end_date < start_date:
        raise validation_error("to cannot be before from")
    number_of_days = (end_date - start_date).days + 1
    if number_of_days > 92:
        raise validation_error("date range cannot exceed 92 days")
    start_datetime = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}
                },
            }
        },
        {
            "$project": {
                "days": {
                    "$map": {
                        "input": {"$range": [0, number_of_days]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_datetime,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
                "employees": 1,
            }
        },
        {"$unwind": "$days"},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$days",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$days"}, 5]},
            }
        },
        {
            "$set": {
                "active_employees": {
                    "$filter": {
                        "input": "$employees",
                        "as": "employee",
                        "cond": {"$lte": ["$$employee.joined_on", "$date"]},
                    }
                }
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$active_employees"},
                "active_codes": {
                    "$map": {
                        "input": "$active_employees",
                        "as": "employee",
                        "in": "$$employee.emp_code",
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date", "codes": "$active_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                                    {
                                        "$cond": [
                                            {"$ifNull": ["$$log.half_day", False]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        half_up_expression(
                            {"$divide": ["$present_count", "$headcount"]}, 4
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"days": 1},
                "output": {
                    "moving_average_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_average_raw", None]},
                        half_up_expression("$moving_average_raw", 4),
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------
@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthlyOut,
    responses={404: NOT_FOUND_RESPONSE},
    tags=["analytics"],
    operation_id="employeeMonthly",
)
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
):
    results = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    if not results:
        raise HTTPException(status_code=404, detail="employee not found")
    return results[0]


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummaryOut,
    tags=["analytics"],
    operation_id="departmentSummary",
)
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: Optional[str] = None,
):
    items = list(
        db.employees.aggregate(
            department_summary_pipeline(month, department), allowDiskUse=True
        )
    )
    return {"month": month, "items": items}


@app.get(
    "/analytics/leaderboard/late",
    response_model=LeaderboardOut,
    tags=["analytics"],
    operation_id="lateLeaderboard",
)
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    items = list(
        db.attendance_logs.aggregate(
            late_leaderboard_pipeline(month, limit, department), allowDiskUse=True
        )
    )
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=TrendOut,
    responses={404: NOT_FOUND_RESPONSE},
    tags=["analytics"],
    operation_id="departmentTrend",
)
def department_trend(
    department: str,
    date_from: Annotated[
        str, Query(alias="from", json_schema_extra={"format": "date"})
    ],
    date_to: Annotated[
        str, Query(alias="to", json_schema_extra={"format": "date"})
    ],
):
    pipeline = department_trend_pipeline(department, date_from, date_to)
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    if not items:
        raise HTTPException(status_code=404, detail="department not found")
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------
def explain_aggregate(collection: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainOut,
    tags=["admin"],
    operation_id="explainEndpoint",
)
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Annotated[
        Optional[str], Query(json_schema_extra={"format": "date"})
    ] = None,
    date_to: Annotated[
        Optional[str], Query(json_schema_extra={"format": "date"})
    ] = None,
    status: Optional[AttendanceStatus] = None,
    range_from: Annotated[
        Optional[str], Query(alias="from", json_schema_extra={"format": "date"})
    ] = None,
    range_to: Annotated[
        Optional[str], Query(alias="to", json_schema_extra={"format": "date"})
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        operation = {
            "find": "attendance_logs",
            "filter": query,
            "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
        explained = db.command("explain", operation, verbosity="executionStats")
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise validation_error("emp_code and month are required")
        collection = "employees"
        explained = explain_aggregate(
            collection, employee_monthly_pipeline(emp_code, month)
        )
    elif endpoint == "department_summary":
        if month is None:
            raise validation_error("month is required")
        collection = "employees"
        explained = explain_aggregate(
            collection, department_summary_pipeline(month, department)
        )
    elif endpoint == "late_leaderboard":
        if month is None:
            raise validation_error("month is required")
        collection = "attendance_logs"
        explained = explain_aggregate(
            collection, late_leaderboard_pipeline(month, limit, department)
        )
    else:
        if department is None or range_from is None or range_to is None:
            raise validation_error("department, from and to are required")
        collection = "employees"
        explained = explain_aggregate(
            collection,
            department_trend_pipeline(department, range_from, range_to),
        )
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": bson_json(explained),
    }

