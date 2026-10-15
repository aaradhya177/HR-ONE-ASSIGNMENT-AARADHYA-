# Employee Attendance & Analytics API

A production-minded REST API for employee attendance, punch workflows, regularization, and workforce analytics. The service is built with FastAPI and MongoDB, follows the contract in [`openapi.yaml`](openapi.yaml), and handles concurrency-sensitive operations such as duplicate punch-ins, punch-outs, and audit-safe corrections.

## Highlights

- Employee creation and paginated directory listing
- IST-aware punch-in and punch-out, including overnight shifts
- Automatic calculation of late minutes, work hours, overtime, and half days
- Attendance filtering by employee, date range, and status
- Manual regularization with an immutable audit history
- Monthly employee and department analytics
- Late-arrival leaderboard with competition ranking and cutoff ties
- Gap-filled daily department trends
- MongoDB execution-plan inspection for key queries
- Integration tests for validation, time boundaries, analytics, indexes, and concurrent requests

## Tech stack

| Layer | Technology |
|---|---|
| API | Python 3.11+, FastAPI, Pydantic v2 |
| Database | MongoDB 6.0+ and PyMongo |
| Server | Uvicorn |
| Testing | pytest, FastAPI TestClient, HTTPX |
| API contract | OpenAPI 3.0 YAML |

## Project structure

```text
.
|-- app/
|   |-- __init__.py
|   `-- main.py                 # Models, business rules, routes, indexes, pipelines
|-- sample_data/
|   |-- attendance_logs.json   # MongoDB Extended JSON attendance examples
|   `-- employees.json         # MongoDB Extended JSON employee examples
|-- tests/
|   `-- test_api.py            # Integration and concurrency tests
|-- DATA_MODEL.md               # Collection schemas and stored-value rules
|-- DECISIONS.md                # Key implementation decisions
|-- REVIEW.md                   # Implementation review notes
|-- openapi.yaml                # Source API contract
|-- sample_seed.py              # Destructive local sample-data loader
|-- requirements.txt
`-- README.md
```

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (MongoDB 7 is suitable for local development)
- `pip` and a Python virtual environment

## Quick start

### 1. Create and activate a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2. Configure the environment

The application requires both variables below. Environment variables take precedence over values loaded from `.env`.

| Variable | Required | Example | Description |
|---|---:|---|---|
| `MONGO_URI` | Yes | `mongodb://localhost:27017` | MongoDB connection URI |
| `MONGO_DB` | Yes | `attendance_db` | Database used by the application |

Copy `.env.example` to an ignored `.env` file in the repository root and adjust it if needed:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Never commit database credentials or a real `.env` file.

### 3. Load the sample data (optional)

Start MongoDB, then run:

```bash
python sample_seed.py
```

> **Warning:** the seed script deletes all documents in the configured `employees` and `attendance_logs` collections before inserting the supplied samples. Use a disposable local database only.

### 4. Run the API

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Once running:

- Swagger UI: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>
- Health check: <http://127.0.0.1:8000/health>

The application creates its required MongoDB indexes idempotently at startup. If MongoDB is unavailable, the process remains available and `GET /health` reports `503`.

## API overview

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Check MongoDB readiness |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | List employees with department filtering and pagination |
| `POST` | `/attendance/punch-in` | Create the employee's attendance record for the applicable IST date |
| `POST` | `/attendance/punch-out` | Close the latest eligible open attendance record |
| `GET` | `/attendance` | Filter and paginate attendance records |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct a record and append an audit entry |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Return one employee's monthly metrics |
| `GET` | `/analytics/departments/summary` | Compare department-level monthly metrics |
| `GET` | `/analytics/leaderboard/late` | Rank employees by late arrivals |
| `GET` | `/analytics/departments/{department}/trend` | Return a gap-filled daily attendance trend |
| `GET` | `/admin/explain/{endpoint}` | Inspect the MongoDB execution plan for supported queries |

The checked-in [`openapi.yaml`](openapi.yaml) contains the complete request schemas, response schemas, status codes, and business-rule descriptions.

## Example requests

Create an employee:

```bash
curl -X POST http://127.0.0.1:8000/employees \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP1001",
    "name": "Asha Rao",
    "email": "asha.rao@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-05"
  }'
```

Punch in using epoch milliseconds:

```bash
curl -X POST http://127.0.0.1:8000/attendance/punch-in \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP1001",
    "punched_at": 1783310280000,
    "status": "PRESENT"
  }'
```

Punch out:

```bash
curl -X POST http://127.0.0.1:8000/attendance/punch-out \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP1001",
    "punched_at": 1783343100000
  }'
```

Filter attendance:

```bash
curl "http://127.0.0.1:8000/attendance?emp_code=EMP1001&date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20"
```

Regularize an incorrect punch and record who made the correction:

```bash
curl -X PATCH http://127.0.0.1:8000/attendance/EMP1001/2026-07-06 \
  -H "Content-Type: application/json" \
  -d '{
    "punch_in": 1783303080000,
    "reason": "Biometric terminal synchronization issue",
    "regularized_by": "hr.admin"
  }'
```

Query analytics:

```bash
curl "http://127.0.0.1:8000/analytics/employees/EMP1001/monthly?month=2026-07"
curl "http://127.0.0.1:8000/analytics/departments/summary?month=2026-07"
curl "http://127.0.0.1:8000/analytics/leaderboard/late?month=2026-07&limit=10"
curl "http://127.0.0.1:8000/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-31"
```

## Business rules

- All API instants are strict integer epoch milliseconds; MongoDB stores them as UTC BSON datetimes.
- Calendar dates and shift times remain strings because they represent IST calendar values, not instants.
- For an overnight shift, the attendance date is the IST date on which the shift starts.
- A punch-in more than 10 minutes after shift start is late; the stored late value is the full delay in minutes.
- Overtime is recorded only when punch-out is at least 30 minutes after shift end.
- Work hours are rounded half-up to two decimal places. Less than 4.50 hours is a half day.
- `PRESENT`, `WFH`, and `ON_DUTY` count as presence; a half day contributes `0.5`.
- A punch interval must be positive and no longer than 24 hours.
- Employee codes and `(emp_code, date)` attendance pairs are unique.
- Regularization recalculates derived values and appends only fields that actually changed to `history`.

See [`DATA_MODEL.md`](DATA_MODEL.md) for the complete persistence model and [`DECISIONS.md`](DECISIONS.md) for the reasoning behind indexes, concurrency control, ranking ties, and scaling.

## Concurrency and data integrity

MongoDB constraints and atomic updates are used as the source of truth:

- A unique `(emp_code, date)` index allows only one punch-in to win a race.
- Punch-out uses a conditional atomic update so concurrent requests cannot close the same record twice.
- Regularization uses optimistic compare-and-update plus `$push`, preventing successful concurrent edits from silently losing audit history.
- Stored derived values are authoritative for analytics; aggregation pipelines do not recalculate them from timestamps.

## Indexes

Startup creates indexes for:

- Unique employee codes
- Unique employee/date attendance records
- Employee department and join-date access patterns
- Attendance date, employee, and status filters
- Monthly analytics, trends, and leaderboard access patterns

To inspect actual query behavior, use `/admin/explain/{endpoint}` with one of: `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, or `department_trend`.

## Testing

Tests require MongoDB and intentionally use the isolated database name `attendance_api_test`.

```bash
python -m pytest -q
```

The suite covers:

- Request validation and pagination
- IST date assignment and overnight shifts
- Grace, overtime, duration, and rounding boundaries
- Duplicate punch-in and punch-out races
- Concurrent regularization and nested audit-history serialization
- Monthly, department, leaderboard, and trend aggregations
- Gap filling, join-date-aware headcount, and ranking ties
- Health-check failure behavior and index-backed query plans

## HTTP behavior

| Status | Meaning |
|---:|---|
| `200` | Successful read, punch-out, or regularization |
| `201` | Employee or punch-in created |
| `404` | Employee, attendance record, or department not found |
| `409` | Duplicate or concurrent state conflict |
| `422` | Invalid request or business-rule violation |
| `503` | MongoDB is unavailable |

## Known limitations

The assignment contract intentionally omits authentication, authorization, employee updates, holiday calendars, and automatic absence generation. The current design targets roughly 100,000 attendance records. At substantially larger scale, the next steps would be measured pre-aggregation, change-aware refreshes, and a deliberate sharding strategy based on production query plans.

## Additional documentation

- [`openapi.yaml`](openapi.yaml) — complete API contract
- [`DATA_MODEL.md`](DATA_MODEL.md) — MongoDB document shapes and field semantics
- [`DECISIONS.md`](DECISIONS.md) — implementation trade-offs
- [`REVIEW.md`](REVIEW.md) — review and verification notes
- [`PROBLEM_STATEMENT.docx`](PROBLEM_STATEMENT.docx) — original assignment brief
