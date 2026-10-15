# Implementation decisions

1. **Indexes.** I created unique indexes on employee code and attendance `(emp_code, date)`. The latter is also the concurrency guard for punch-in. Attendance list indexes cover unfiltered, employee-filtered, status-filtered, and combined status/employee sort patterns. Employee department/join-date indexes support headcount and trend pipelines. I rejected a broad index containing every attendance field because it would increase write cost without matching a real query.

2. **Punch-in race.** Both requests can read the employee and calculate the same attendance date. Both try the insert, but MongoDB's unique index permits exactly one. That request returns 201; the duplicate-key exception from the other becomes 409.

3. **Ties.** The pipeline uses MongoDB's competition `$rank`. If two people tie at the cutoff rank, both are returned because filtering happens on `rank <= limit`; the next rank is skipped.

4. **Headcount.** Department summary starts from eligible employees, then looks up their logs. Therefore employees with an empty lookup array still contribute one to headcount.

5. **At 100x scale.** I would pre-aggregate immutable daily/monthly facts with change-aware refreshes, shard attendance by a deliberate employee/date key, and measure production explain plans before adjusting indexes.
