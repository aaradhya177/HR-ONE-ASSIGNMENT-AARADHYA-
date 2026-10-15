"""Contract-focused integration tests against an isolated local MongoDB database."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "attendance_api_test"

from app import main  # noqa: E402


IST = timezone(timedelta(hours=5, minutes=30))


def epoch_ms(day: str, clock: str, *, zone=IST, milliseconds: int = 0) -> int:
    instant = datetime.combine(
        date.fromisoformat(day), datetime.strptime(clock, "%H:%M:%S").time(), zone
    )
    return int(instant.timestamp() * 1000) + milliseconds


def employee(
    code: str,
    *,
    department: str = "Engineering",
    joined_on: str = "2026-08-01",
    shift_start: str = "09:30",
    shift_end: str = "18:30",
) -> dict:
    return {
        "emp_code": code,
        "name": f"Employee {code}",
        "email": f"{code.lower()}@example.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
    }


@pytest.fixture(scope="session", autouse=True)
def isolated_database():
    assert main.MONGO_DB == "attendance_api_test"
    main.client.drop_database(main.MONGO_DB)
    main.create_indexes()
    yield
    main.client.drop_database(main.MONGO_DB)


@pytest.fixture(autouse=True)
def clean_collections():
    main.db.employees.delete_many({})
    main.db.attendance_logs.delete_many({})


@pytest.fixture
def api():
    with TestClient(main.app) as client:
        yield client


def test_employee_create_duplicate_validation_and_pagination(api):
    assert api.post("/employees", json=employee("EMP1001")).status_code == 201
    assert api.post("/employees", json=employee("EMP1001")).status_code == 409
    invalid = employee("EMP1002", shift_start="09:30", shift_end="09:30")
    assert api.post("/employees", json=invalid).status_code == 422
    assert api.post("/employees", json=employee("bad")).status_code == 422

    api.post("/employees", json=employee("EMP1003", department="Sales"))
    api.post("/employees", json=employee("EMP1002"))
    page = api.get("/employees", params={"department": "Engineering", "page_size": 1})
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["items"][0]["emp_code"] == "EMP1001"
    assert api.get("/employees", params={"page": 0}).status_code == 422


def test_concurrent_employee_creation_has_one_winner(api):
    payload = employee("EMP1100")
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(lambda _: api.post("/employees", json=payload).status_code, range(6)))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 5


def test_punch_in_boundaries_timezone_and_validation(api):
    api.post("/employees", json=employee("EMP1200"))
    inside_grace = epoch_ms("2026-08-03", "09:40:00", milliseconds=900)
    response = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1200", "punched_at": inside_grace},
    )
    assert response.status_code == 201
    assert response.json()["date"] == "2026-08-03"
    assert response.json()["punch_in"] == epoch_ms("2026-08-03", "09:40:00")
    assert response.json()["late_minutes"] == 0
    assert "_id" not in response.json() and "id" not in response.json()

    assert api.post(
        "/attendance/punch-in", json={"emp_code": "UNKNOWN", "punched_at": inside_grace}
    ).status_code == 404
    assert api.post(
        "/attendance/punch-in", json={"emp_code": "EMP1200", "punched_at": 123}
    ).status_code == 422


def test_punched_at_omission_null_and_strict_epoch_validation(api):
    invalid_values = [
        None,
        1_783_312_500,
        "1783312500000",
        1_783_312_500_000.0,
        True,
        main.EPOCH_MIN - 1,
        main.EPOCH_MAX + 1,
    ]
    for path in ("/attendance/punch-in", "/attendance/punch-out"):
        for value in invalid_values:
            response = api.post(
                path,
                json={"emp_code": "UNKNOWN", "punched_at": value},
            )
            assert response.status_code == 422, (path, value, response.text)

    api.post("/employees", json=employee("EMP1202"))
    before = int(datetime.now(timezone.utc).timestamp())
    omitted = api.post("/attendance/punch-in", json={"emp_code": "EMP1202"})
    after = int(datetime.now(timezone.utc).timestamp())
    assert omitted.status_code == 201
    assert before * 1000 <= omitted.json()["punch_in"] <= after * 1000

    api.post("/employees", json=employee("EMP1203"))
    valid_in = epoch_ms("2026-08-03", "09:30:00")
    valid_out = epoch_ms("2026-08-03", "18:30:00")
    assert api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1203", "punched_at": valid_in},
    ).status_code == 201
    assert api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1203", "punched_at": valid_out},
    ).status_code == 200

    api.post("/employees", json=employee("EMP1204"))
    now_seconds = int(datetime.now(timezone.utc).timestamp())
    recent_in = (now_seconds - 60) * 1000
    assert api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1204", "punched_at": recent_in},
    ).status_code == 201
    omitted_out = api.post(
        "/attendance/punch-out", json={"emp_code": "EMP1204"}
    )
    assert omitted_out.status_code == 200
    assert now_seconds * 1000 <= omitted_out.json()["punch_out"] <= (
        int(datetime.now(timezone.utc).timestamp()) * 1000
    )
    assert api.post(
        "/attendance/punch-in", json={"emp_code": "EMP1200", "status": "ABSENT"}
    ).status_code == 422


def test_late_boundary_and_concurrent_punch_in(api):
    api.post("/employees", json=employee("EMP1201"))
    late = epoch_ms("2026-08-04", "09:40:01")
    payload = {"emp_code": "EMP1201", "punched_at": late}
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: api.post("/attendance/punch-in", json=payload), range(4)))
    assert [response.status_code for response in responses].count(201) == 1
    assert [response.status_code for response in responses].count(409) == 3
    winner = next(response for response in responses if response.status_code == 201)
    assert winner.json()["late_minutes"] == 10


def test_overnight_date_and_punch_out_calculations(api):
    api.post(
        "/employees",
        json=employee("EMP1300", shift_start="22:00", shift_end="06:00"),
    )
    punch_in = epoch_ms("2026-08-06", "01:00:00")
    created = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1300", "punched_at": punch_in},
    )
    assert created.status_code == 201
    assert created.json()["date"] == "2026-08-05"

    punch_out = epoch_ms("2026-08-06", "06:30:00")
    closed = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1300", "punched_at": punch_out},
    )
    assert closed.status_code == 200
    assert closed.json()["work_hours"] == 5.5
    assert closed.json()["overtime_minutes"] == 30
    assert closed.json()["half_day"] is False
    assert api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1300", "punched_at": punch_out + 60_000},
    ).status_code == 409


def test_work_hours_half_up_half_day_and_overtime_threshold(api):
    api.post("/employees", json=employee("EMP1301"))
    start = epoch_ms("2026-08-05", "09:30:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP1301", "punched_at": start})
    # 4h29m42s = 4.495h, which must round half-up to 4.50.
    end = start + (4 * 3600 + 29 * 60 + 42) * 1000
    result = api.post(
        "/attendance/punch-out", json={"emp_code": "EMP1301", "punched_at": end}
    )
    assert result.status_code == 200
    assert result.json()["work_hours"] == 4.5
    assert result.json()["half_day"] is False
    assert result.json()["overtime_minutes"] == 0

    api.post("/employees", json=employee("EMP1302"))
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1302", "punched_at": epoch_ms("2026-08-05", "09:30:00")},
    )
    result = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1302", "punched_at": epoch_ms("2026-08-05", "18:59:59")},
    )
    assert result.json()["overtime_minutes"] == 0


def test_punch_out_validation_and_concurrency(api):
    api.post("/employees", json=employee("EMP1400"))
    start = epoch_ms("2026-08-05", "09:30:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP1400", "punched_at": start})
    assert api.post(
        "/attendance/punch-out", json={"emp_code": "EMP1400", "punched_at": start}
    ).status_code == 422
    assert api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1400", "punched_at": start + 24 * 3600 * 1000 + 1000},
    ).status_code == 422

    end = start + 8 * 3600 * 1000
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(
                lambda _: api.post(
                    "/attendance/punch-out",
                    json={"emp_code": "EMP1400", "punched_at": end},
                ).status_code,
                range(2),
            )
        )
    assert sorted(statuses) == [200, 409]


def test_attendance_list_validation_sort_pagination_and_legacy_defaults(api):
    main.db.attendance_logs.insert_many(
        [
            {"emp_code": "EMP2", "date": "2026-08-02", "status": "PRESENT"},
            {"emp_code": "EMP1", "date": "2026-08-02", "status": "PRESENT"},
            {"emp_code": "EMP3", "date": "2026-08-01", "status": "LEAVE"},
        ]
    )
    response = api.get("/attendance", params={"page_size": 2})
    assert [item["emp_code"] for item in response.json()["items"]] == ["EMP1", "EMP2"]
    assert response.json()["items"][0]["history"] == []
    assert response.json()["items"][0]["half_day"] is False
    assert api.get(
        "/attendance", params={"date_from": "2026-08-03", "date_to": "2026-08-01"}
    ).status_code == 422
    assert api.get("/attendance", params={"status": "INVALID"}).status_code == 422
    assert api.get("/attendance", params={"page_size": 101}).status_code == 422


def test_regularization_recalculates_and_serializes_nested_history(api):
    api.post("/employees", json=employee("EMP1500"))
    original_in = epoch_ms("2026-08-05", "10:05:00")
    original_out = epoch_ms("2026-08-05", "18:30:00")
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1500", "punched_at": original_in},
    )
    api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1500", "punched_at": original_out},
    )
    corrected_in = epoch_ms("2026-08-05", "09:28:00")
    response = api.patch(
        "/attendance/EMP1500/2026-08-05",
        json={
            "punch_in": corrected_in,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["late_minutes"] == 0
    assert body["history"][0]["changes"]["punch_in"] == {
        "from": original_in,
        "to": corrected_in,
    }
    assert "late_minutes" in body["history"][0]["changes"]
    assert isinstance(body["history"][0]["at"], int)
    assert api.patch(
        "/attendance/EMP1500/2026-08-05",
        json={
            "punch_in": corrected_in,
            "reason": "same value",
            "regularized_by": "hr.admin",
        },
    ).status_code == 422


def test_regularization_to_leave_clears_times_and_records_only_changes(api):
    api.post("/employees", json=employee("EMP1501"))
    punch_in = epoch_ms("2026-08-06", "09:30:00")
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1501", "punched_at": punch_in},
    )
    response = api.patch(
        "/attendance/EMP1501/2026-08-06",
        json={
            "status": "LEAVE",
            "reason": "approved leave",
            "regularized_by": "hr.admin",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "LEAVE"
    assert body["punch_in"] is None and body["punch_out"] is None
    assert set(body["history"][0]["changes"]) == {"status", "punch_in"}
    assert api.patch(
        "/attendance/EMP1501/2026-08-06",
        json={
            "status": "ABSENT",
            "punch_in": punch_in,
            "reason": "invalid combination",
            "regularized_by": "hr.admin",
        },
    ).status_code == 422


def test_concurrent_regularizations_never_lose_successful_history(api):
    api.post("/employees", json=employee("EMP1502"))
    initial = epoch_ms("2026-08-07", "10:00:00")
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1502", "punched_at": initial},
    )
    payloads = [
        {
            "punch_in": epoch_ms("2026-08-07", "09:31:00"),
            "reason": "first correction",
            "regularized_by": "hr.one",
        },
        {
            "punch_in": epoch_ms("2026-08-07", "09:32:00"),
            "reason": "second correction",
            "regularized_by": "hr.two",
        },
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda body: api.patch(
                    "/attendance/EMP1502/2026-08-07", json=body
                ),
                payloads,
            )
        )
    statuses = [response.status_code for response in responses]
    assert all(status in (200, 409) for status in statuses)
    assert statuses.count(200) >= 1
    stored = main.db.attendance_logs.find_one(
        {"emp_code": "EMP1502", "date": "2026-08-07"}
    )
    assert len(stored["history"]) == statuses.count(200)


def seed_analytics_data():
    now = datetime.now(timezone.utc)
    employees = [
        {**employee("EMP2001", department="Sales"), "created_at": now},
        {**employee("EMP2002", department="Sales"), "created_at": now},
        {**employee("EMP2003", department="Sales", joined_on="2026-08-03"), "created_at": now},
        {**employee("EMP2004", department="NoLogs"), "created_at": now},
    ]
    main.db.employees.insert_many(employees)
    logs = [
        ("EMP2001", "2026-08-03", 30, False, 8.0, "PRESENT"),
        ("EMP2002", "2026-08-03", 20, False, 9.0, "PRESENT"),
        ("EMP2003", "2026-08-03", 20, True, 3.0, "PRESENT"),
        ("EMP2001", "2026-08-08", 5, False, 8.0, "PRESENT"),
        ("EMP2002", "2026-08-04", 0, False, None, "LEAVE"),
    ]
    main.db.attendance_logs.insert_many(
        [
            {
                "emp_code": code,
                "date": day,
                "status": status,
                "punch_in": None,
                "punch_out": None,
                "work_hours": work_hours,
                "late_minutes": late,
                "overtime_minutes": 0,
                "half_day": half_day,
                "history": [],
            }
            for code, day, late, half_day, work_hours, status in logs
        ]
    )


def test_monthly_department_and_leaderboard_analytics(api):
    seed_analytics_data()
    monthly = api.get(
        "/analytics/employees/EMP2003/monthly", params={"month": "2026-08"}
    )
    assert monthly.status_code == 200
    assert monthly.json()["working_days"] == 21
    assert monthly.json()["present_days"] == 0.5

    summary = api.get("/analytics/departments/summary", params={"month": "2026-08"})
    assert summary.status_code == 200
    no_logs = next(item for item in summary.json()["items"] if item["department"] == "NoLogs")
    assert no_logs["headcount"] == 1 and no_logs["avg_work_hours"] is None

    leaderboard = api.get(
        "/analytics/leaderboard/late", params={"month": "2026-08", "limit": 2}
    )
    assert [(item["rank"], item["emp_code"]) for item in leaderboard.json()["items"]] == [
        (1, "EMP2001"),
        (2, "EMP2002"),
        (2, "EMP2003"),
    ]


def test_trend_gap_filling_headcount_and_moving_average(api):
    seed_analytics_data()
    response = api.get(
        "/analytics/departments/Sales/trend",
        params={"from": "2026-08-01", "to": "2026-08-05"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 5
    assert [item["date"] for item in items] == [
        "2026-08-01",
        "2026-08-02",
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
    ]
    assert items[0]["attendance_rate"] is None
    assert items[2]["headcount"] == 3 and items[2]["present_count"] == 2.5
    assert items[4]["attendance_rate"] == 0.0
    assert api.get(
        "/analytics/departments/Unknown/trend",
        params={"from": "2026-08-01", "to": "2026-08-02"},
    ).status_code == 404
    assert api.get(
        "/analytics/departments/Sales/trend",
        params={"from": "2026-01-01", "to": "2026-04-03"},
    ).status_code == 422


def test_explain_uses_indexes_and_validates_required_parameters(api):
    seed_analytics_data()
    assert api.get("/admin/explain/employee_monthly").status_code == 422
    cases = [
        ("attendance_list", {}),
        ("employee_monthly", {"emp_code": "EMP2001", "month": "2026-08"}),
        ("department_summary", {"month": "2026-08"}),
        ("late_leaderboard", {"month": "2026-08"}),
        (
            "department_trend",
            {"department": "Sales", "from": "2026-08-01", "to": "2026-08-05"},
        ),
    ]
    for endpoint, params in cases:
        response = api.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 200
        plan = str(response.json()["explain"])
        assert "IXSCAN" in plan
        assert "COLLSCAN" not in plan


def test_health_reports_database_failure(monkeypatch):
    class BrokenAdmin:
        @staticmethod
        def command(_):
            raise RuntimeError("offline")

    class BrokenClient:
        admin = BrokenAdmin()

    monkeypatch.setattr(main, "client", BrokenClient())
    with pytest.raises(main.HTTPException) as error:
        main.health()
    assert error.value.status_code == 503


def test_startup_remains_available_for_health_when_index_creation_fails(
    monkeypatch,
):
    def unavailable_indexes():
        raise main.ServerSelectionTimeoutError("offline")

    monkeypatch.setattr(main, "create_indexes", unavailable_indexes)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
