# Stage Deputy / وكيل المرحلة

Stage Deputies monitor explicitly assigned school stages at `/deputy/`. They are
read-only and do not receive Django staff, superuser, competition-control, screen
registration, user-management, question-bank, or school-wide reporting access.

| Role | Classroom scope | Competitions / results | Administration / controls |
| --- | --- | --- | --- |
| Administrator | Whole school | Existing whole-school access | Existing administrator rights |
| Teacher | Existing teacher assignments | Existing ownership rules | Existing teacher controls; no administrator rights |
| Stage Deputy | Active stages explicitly assigned by an administrator | Only rounds with at least one target and **all** targets in the assigned scope | Read-only monitoring; no controls or administration |

## Stage classification and assignments

`Classroom.grade` remains unchanged as descriptive legacy data. The existing
values `G1` and `1` do not establish that two rooms belong to the same school
stage. `SchoolStage` supplies explicit, stable, administrator-managed stage codes;
`Classroom.stage` is nullable and initially null for every existing classroom.
No existing user, teacher assignment, or classroom is reclassified automatically.
Tournament stages describe championship rounds and are not school stages.

An administrator signs in through the normal login and:

1. Opens **Administration → Classrooms → School stages**, creates a named stage
   with a unique code, and leaves it active.
2. Opens each appropriate **Classroom**, explicitly selects its school stage,
   reviews its existing teacher assignments, and saves.
3. Opens **Users**, creates or selects a user, chooses **Stage Deputy**, selects
   one or more **Assigned stages**, and saves. The role cannot be combined with
   superuser status. Stage selection rejects nonexistent IDs and non-deputy users.
4. Reviews the user's assignments. Removing a stage immediately removes it from
   monitoring; removing every stage gives a clear empty dashboard. Deactivating
   a stage also removes monitoring access without deleting its history.

Use this classification workflow only after the school confirms the real scope.
The deployment and tests do not assign stages to real school classrooms.

## Isolation

The server derives classroom membership from stage assignments on every request.
Screen visibility follows classroom relationships and exposes names, presence,
last-seen and numeric monitoring links, never bearer Screen IDs or registration
URLs. Legacy teacher/admin/report/API paths are denied to Stage Deputies by an
explicit HTTP boundary as well as role-specific authorization helpers.

Mixed-stage competitions are withheld unless **every** target classroom is in the
viewer's assigned stages. Targetless competitions are withheld. This prevents
opponent names, scores and indirect totals from leaking across stages. Dashboard
counts, search, pagination, details and CSV use the same scoped queries. Frozen
results are preferred, and snapshot room IDs are checked against authorized rooms;
raw snapshot names and global winner fields are not rendered. New CSV exports
neutralize spreadsheet formulas in textual cells.

Deputies cannot connect to teacher, leaderboard or device WebSockets. Teacher and
leaderboard sockets recheck current role and ownership before inbound controls
and outbound messages, so a teacher converted to a deputy cannot retain control
through an already open socket. Device sockets with an authenticated staff
session also refresh its role before receiving or sending; anonymous school
devices retain the existing Screen ID authentication flow.

## Routes

- `/deputy/` — dashboard and stage/status/classroom-search filters.
- `/deputy/classrooms/<id>/` — classroom monitoring.
- `/deputy/screens/<id>/` — screen status without registration credentials.
- `/deputy/competitions/<id>/` — scoped round and results.
- `/deputy/results/` and `/deputy/results/export.csv` — scoped results and export.

All monitoring endpoints accept GET/HEAD/OPTIONS only. Forged or out-of-scope
stage/object IDs return 404; state-changing monitor requests return 405 (or 403
if CSRF is missing). Pagination retains filters. `/dashboard/` redirects deputies
to their workspace. Arabic/English and light/dark settings use existing cookies
and CSRF-protected preference endpoints.

## Deployment

Use the canonical production overlay and build from `/opt/almanar/challenge`.
Apply `accounts.0003` (role validation and deputy/non-superuser constraint) and
`classrooms.0002` (stage table, assignment relation, nullable classroom FK) after
checking the plan. PostgreSQL migrations are atomic and do not assign real data.
Existing users retain their roles and scopes. No database or Redis restart or
volume deletion is required.
