# Phase 5 production acceptance

Use the existing canonical production Compose files. Back up uncommitted source
and original data first. Never start or broadcast a test round on production:
the current publisher also nudges the global waiting-screen group.

`browser_acceptance.py` runs normal login, role navigation, Arabic/English and
RTL/LTR, all five question builders, draft persistence and schedule persistence.
It consumes a private `/tmp/phase5_spec.json` with explicitly owned temporary
users, subject, stage/classroom/screen IDs and test password; never use original
school accounts or records as its mutation fixtures.

Set `P5_BASE` and `P5_OUT` for the target URL and private evidence prefix. A unique
`P5_RUN` avoids collisions when a failed diagnostic run is repeated. With
`P5_ISOLATED=1`, the URL is restricted to the isolated validation container. That
mode also starts a round, checks real WebSocket frame delivery to the target
screen, withholds the question from the non-target screen, submits a translated
true/false answer, rejects repeated start and finishes the test round.

The screen regression must assert that exactly one `[data-panel]` is visible;
checking the HTML `hidden` attribute alone misses CSS overriding that attribute.
Dynamic Arabic labels must remain translated after actual WebSocket events and
heartbeat updates. Wire values for true/false must remain protocol values.

Use a separate PostgreSQL and Redis deployment for isolated starts. Collect
static assets and compile the Arabic catalog from the current source before
browser execution. Run automated test databases separately from active browser
fixtures. Clean only the records, sessions and credentials created for this run.
Record each check as PASS / FAIL / NOT TESTED, preserve failed diagnostic logs,
and verify original row fingerprints and source/image/static parity afterward.

Physical classroom devices, prolonged load and cross-VLAN delivery require a
separate acceptance window; browser WebSocket delivery to temporary screens is
not proof of those environments.
