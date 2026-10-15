# Starter code review

The review below covers the supplied `app/main.py`, including defects that only appear at boundaries, under concurrency, or with larger datasets.

| # | Where | What was wrong | How to reproduce or notice it | Correction |
|---|---|---|---|---|
| 1 | Module-level database setup | Only the employee index was created, and it was created during import rather than application startup. The attendance uniqueness guarantee and query indexes were absent. | Inspect `attendance_logs.getIndexes()`, or send concurrent same-day punch-ins. | Moved idempotent index creation into the FastAPI lifespan and added unique natural-key and query-specific indexes. |
| 2 | `EmployeeIn` | Employee code, strings, email, date, and shift time formats were not validated. | Submit `emp_code="x"`, an invalid email/date, or `shift_start="25:90"`; the starter accepted them. | Added contract-sized Pydantic fields, patterns, strict date validation, and the unequal-shift rule. |
| 3 | `compute_late_minutes` | It combined a possibly UTC punch with a same-date naive shift time and did not anchor the shift to the IST attendance date. | Run outside IST or use an overnight shift; subtraction is wrong or mixes aware and naive datetimes. | Build the shift instant in IST from the attendance date, convert to UTC, and compare whole seconds. |
| 4 | `compute_late_minutes` | Integer truncation was applied before the grace comparison. `09:40:01` became 10 minutes and was treated as on time. | Punch at 09:40:01 for a 09:30 shift; R2 requires 10 late minutes. | Compare elapsed seconds to 600 first, then floor the full elapsed minutes. |
| 5 | `compute_work_hours` | Python `round(float, 2)` uses binary floats and ties-to-even, not decimal half-up. | A 4.495-hour interval can round incorrectly and change `half_day`. | Use `Decimal.quantize(..., ROUND_HALF_UP)` on the exact whole-second duration. |
| 6 | `compute_overtime` | It ignored overnight shift ends and returned any positive overtime, although R3 has a 30-minute minimum. | A 22:00-06:00 shift uses the wrong day; 18:35 incorrectly returns 5. | Anchor shift end in IST, advance it one day for overnight shifts, floor minutes, and return zero below 30 minutes. |
| 7 | `list_employees` | Pagination skipped `page * page_size`, so page 1 omitted the first page. | Request the default first page; it begins at employee 21. | Use `(page - 1) * page_size` and validate page bounds. |
| 8 | `list_employees` | `total` counted the whole collection, ignoring the department filter. | Filter a two-person department in a larger company; total still showed company size. | Count with the same filter used by the page query. |
| 9 | `list_employees` | Results had no deterministic sort and BSON `created_at` was not converted to epoch milliseconds. | Repeated pages can shift order; FastAPI emits an ISO datetime rather than the contracted integer. | Sort by `emp_code`, project out `_id`, and serialize all instants at the API boundary. |
| 10 | `punch_in` | An unknown employee was dereferenced and produced a 500. | Punch in with an unrecognised `emp_code`. | Return 404 before using employee shift fields. |
| 11 | `PunchInIn` and `punch_in` | Epoch values were coercible/unbounded, seconds-looking values were accepted, and server/request times became naive local datetimes. | Send a string, float, or `123`; or run the server outside UTC. | Require strict integer milliseconds in the documented range, store aware UTC BSON datetimes, and truncate punches to seconds. |
| 12 | `punch_in` | The record date used the server's local calendar date, not R1's IST date and overnight rule. | Punch an overnight employee at 01:00 IST; the starter stores the new calendar day. | Convert to IST and move pre-shift-end overnight punches to the prior attendance date. |
| 13 | `punch_in` | Any status string was accepted. | Send `ABSENT` or an arbitrary string to punch-in. | Restrict the request to `PRESENT`, `WFH`, or `ON_DUTY`. |
| 14 | `punch_in` | Duplicate prevention used check-then-insert, so concurrent requests could both succeed. | Send parallel identical requests before either insert is visible. | Rely on a unique `(emp_code, date)` index and translate `DuplicateKeyError` to 409. |
| 15 | `punch_in` | The response added an internal `id` derived from MongoDB `_id` and returned BSON datetimes without contract conversion. | Inspect any successful punch-in response. | Return only the attendance schema and recursively serialize datetime values to epoch milliseconds. |
| 16 | `list_attendance` | Dates, status, page, page size, and reversed ranges were not validated. | Use `date_from > date_to`, an impossible date, invalid status, page 0, or page size 101. | Added FastAPI and semantic validation with contracted 422 response bodies. |
| 17 | `list_attendance` | It loaded all matching records into Python, sorted only by date, paginated in memory, and exposed an `id`. | At about 100,000 records memory and latency grow with the full result; equal dates are nondeterministic. | Perform the exact sort, skip, and limit in MongoDB, count separately, and remove internal IDs. |
| 18 | Attendance serialization | Nested datetimes in `history[].changes` were not converted, and legacy missing `history`/`half_day` fields were not normalised. | Read the supplied regularized and legacy sample records. | Added recursive datetime conversion and API defaults for optional legacy stored fields. |

## Code examined and found correct

- `load_dotenv()` uses its default `override=False`, so real environment variables already win over a local `.env` file.
- The MongoDB health handler already performed an actual `ping` and translated failures to 503.
- Employee creation already used the server clock in UTC, removed `_id`, caught `DuplicateKeyError`, and returned 409. Those parts were retained.
- Treating `shift_end < shift_start` as valid was correct because the contract uses it to represent overnight shifts; only equal endpoints are invalid.
- The starter's initial punch-in derived defaults (`punch_out`, `work_hours`, overtime, half-day, and history) matched the required stored shape.

The seven endpoints marked TODO were missing functionality rather than defects in an existing implementation. They are implemented in the completed file and tested separately.
