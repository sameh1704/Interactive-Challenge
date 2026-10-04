# Automated and manual verification

The Django test suite lives with the code it tests, in
`backend/core/tests/`, because that is where the Django test runner discovers it
and where it belongs architecturally. This directory holds the *verification
artifacts*: the executable verification runner and the per-phase acceptance
record.

## Layout

```
tests/
├── README.md
├── scripts/
│   ├── verify.ps1                      # end-to-end verification of a running stack
│   ├── seed_phase7_demo.py             # fixtures for the tournament walkthrough
│   ├── manual_tournament_walkthrough.ps1  # Phase 7 procedure, driven over HTTP
│   └── phase7_state.py                 # read-only state dump the walkthrough asserts on
└── phases/
    └── phase-01/
        ├── acceptance.md     # acceptance criteria and their evidence
        └── manual-test.md    # step-by-step manual verification procedure
```

## Running the automated tests

Requires PostgreSQL and Redis to be up, which `docker compose` provides:

```bash
docker compose exec challenge-web python manage.py test --verbosity 2
```

## Running the full verification

```bash
pwsh -File tests/scripts/verify.ps1
```

This validates the compose file, brings the stack up, asserts every service is
healthy, exercises `GET /` and `GET /health/`, proves PostgreSQL and Redis are
reachable *from inside the project network*, runs the Django test suite, and
checks that no placeholder secret is left in `.env`.

The script only addresses services declared in this project's
`docker-compose.yml`. It never stops, removes or reconfigures any container
that belongs to another project.

## Running the Phase 7 tournament walkthrough

The walkthrough builds a whole championship - four classrooms, a qualification
stage, an attached round, advancement, a final and a frozen record - through the
real pages, then reads the stored rows back to confirm the actions persisted:

```powershell
Get-Content -Raw tests/scripts/seed_phase7_demo.py | docker compose exec -T challenge-web python manage.py shell
pwsh -File tests/scripts/manual_tournament_walkthrough.ps1
```

The seed is repeatable. A round belongs to at most one stage, so a finished
walkthrough leaves the demo rounds attached to it; the seed therefore clears the
championships the walkthrough itself created and leaves the classrooms and the
finished rounds alone.

Every *action* in the walkthrough is a real form post with a real session and a
real CSRF token. `phase7_state.py` only serialises what the database already
holds, so it can be run on its own at any time without changing anything:

```powershell
Get-Content -Raw tests/scripts/phase7_state.py | docker compose exec -T challenge-web python manage.py shell
```