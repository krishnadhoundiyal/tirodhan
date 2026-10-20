# Geographical rolling QA — generated rider-route reports

These static reports are derived from the **recorded primary geographical QA evidence** at commit `10c4655c8db0117ed9fe51c710d5b4c3f728ee0f`. The reports cover West, North, South and East, R1–R4. Open [index.html](index.html).

## Reusable generator (QA tooling, outside the API/runtime)

```powershell
# Run the QA first (the wrapper prints its unique output directory):
./tests/routing/run-geographical.ps1 -SkipBuild

# Then generate from that run's evidence (substitute the printed UUID):
python tests/routing/qa_reports/render_geographical_route_reports.py --evidence-root .tools/geographical-qa/runs/<run-uuid>

# Or generate from a single existing case directory:
python tests/routing/qa_reports/render_geographical_route_reports.py --evidence-root .tools/geographical-qa/runs/<run-uuid>/primary

# Offline unit tests:
python -m unittest discover -s tests/routing/qa_reports -p 'test_*.py' -v
```

The generator reads each case's `QA_BOOKINGS.csv`, `QA_PLAN_SNAPSHOTS.json`, `QA_EXECUTIONS.json`, `QA_METRICS.json`, plus `QA_REGION_GEOGRAPHY.json` if present. For older runs it recovers the authoritative receiving-point geometry from the already-recorded `QA_TEAM_REPORT.html`. It performs **no database queries, OSRM calls or planning**, and uses **no external JavaScript, map tiles or paid services**.

Each run writes to its own `<case>/route-reports/` directory. The generator opens report files with exclusive creation (`x`) and refuses to overwrite prior reports or their original evidence. The existing QA wrapper independently rejects reuse of a nonempty run-output directory. Selected successful evidence can be retained in versioned review folders, as done here; there is no implicit deletion or replacement.

## Interpretation

Reports show actual persisted route stop ordering and recorded travel/distance/cycle timings. The maps connect successive recorded stop locations with **straight schematic segments**; they do **not** display turn-by-turn OSRM road geometry. Synthetic receiving points are taken from the QA operating-region geometry, not real authorized facilities. The booking key includes paid requests outside the rolling horizon. Some test flows record physical pickup events; an already collected booking may remain assigned while its pickup stop is absent from the active route. Such ownership comes from the authoritative assignment ledger, not just the current stop list.

## Retention and changes

Do not overwrite this folder when running QA again. Use the fresh UUID-based runtime directory for each run and a new versioned review folder if promoting later results. Source and template live solely under `tests/routing/qa_reports/`; no production API or database schema is changed.

Validation: the Python report generator has offline tests for complete report output, missing geometry, inconsistent booking counts and no-overwrite behavior. Historical primary static pages were reconstructed from the retained QA evidence and checked against its booked/assigned counts. This does **not** represent a fresh native QA execution or full-suite pass.
