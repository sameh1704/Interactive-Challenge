# Backup and Restore

PostgreSQL is the only permanent store. If it is lost, competitions,
classrooms, question banks and championship history are lost with it. The
uploaded question images live in a separate volume and need backing up too.

---

## Taking a backup

From the project directory on the server:

```bash
./deploy/backup.sh
```

It:

1. dumps the database in PostgreSQL's **custom format** (compressed, and
   selectively restorable),
2. writes a SHA-256 checksum beside the dump,
3. **verifies the dump is readable** before reporting success,
4. deletes dumps older than `KEEP_DAYS` (default 30).

The checksum file records the **file name only**, never an absolute path, so a
dump copied from another machine still passes `sha256sum --check` on the server.
An integrity check that fails because of a path recorded on a different computer
is worse than no check, because it looks like one.

Output looks like:

```
[backup] database : challenge (container almanar-challenge-db)
[backup] writing  : /opt/almanar/challenge/backups/challenge-20261004-091338.dump
[backup] checksum : 9f2c1e0b7a...
[backup] size     : 1.2M
[backup] verifying the archive is readable
[backup] verified : 27 tables present in the archive
[backup] retention: keeping 30 days
```

`backups/` is git-ignored. A custom-format dump contains the whole database,
**including password hashes**, so `git add -A` must never pick one up.

A backup in a different place:

```bash
BACKUP_DIR=/mnt/usb/backups ./deploy/backup.sh
```

---

## Validating a backup

A backup you have never restored is a hypothesis. Restore it into a scratch
database and compare every table with the live one:

```bash
./deploy/restore.sh backups/challenge-20261004-091338.dump --verify
```

This:

- checks the checksum,
- restores into a **separate scratch database**, leaving the live one untouched,
- compares the row count of **every** table against the live database,
- refuses to report success unless it compared at least as many tables as the
  dump contains,
- drops the scratch database.

Success:

```
[restore] checksum OK
[restore] restoring into scratch database 'challenge_restore_check' - the live database is untouched
[restore] comparing row counts
[restore] scratch database dropped
[restore] VERIFY PASSED: all 27 tables match the live database.
```

Anything reported as `MISMATCH` is a real problem: that table did not come back.

**Run this on a schedule** — weekly is reasonable. It is the only way to learn
that backups are broken while there is still time to fix them.

---

## Restoring over the live database

This **destroys every row** in the application database.

```bash
# 1. Back up what you are about to overwrite. Always.
./deploy/backup.sh

# 2. Restore, with the explicit confirmation flag.
./deploy/restore.sh backups/challenge-20261004-091338.dump --live --confirm
```

Without `--confirm` the script refuses and explains why. That refusal is
intentional — this command has no undo.

The live restore:

1. stops the proxy **and** web containers, so nothing writes mid-restore,
2. drops and recreates the database,
3. restores the dump,
4. starts **both** containers again, and
5. polls `/health/` through the proxy for up to 60 s before reporting success.

Step 4 matters: the proxy is the only published port in production, so a restore
that left it stopped would look like a successful restore with a dead site. Step 5
is why you can trust the "done" it prints. The web container's entrypoint
re-applies migrations on start, so a dump taken before a schema change is
brought forward this way.

If step 5 times out, the script exits non-zero and points you at both container
logs. The restore itself has already happened; what failed is the check.

Check afterwards:

```bash
curl -fsS http://<server>/health/
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Open the application and confirm the expected classrooms and competitions are
present.

---

## Migrating to a schema change

Migrations run automatically when the `challenge-web` container starts, and the
`challenge-prepare` service runs them explicitly during a deployment. To apply
them by hand:

```bash
docker exec -it almanar-challenge-web python manage.py migrate --noinput
```

Take a backup first. A migration that fails half way leaves the schema
part-updated; the restore procedure is then the way back.

---

## Scheduling

Scheduled with a **systemd timer** rather than cron, so a backup missed while
the server was off runs as soon as it comes back instead of being skipped, and
so a failure is visible in `systemctl status` rather than only in a log file
nobody reads.

Install and enable it:

```bash
cd /opt/almanar/challenge
./deploy/install-backup-timer.sh
```

That installs and enables two timers:

| Timer | Default schedule | What it runs |
| --- | --- | --- |
| `almanar-challenge-backup.timer` | daily 02:30 | `deploy/backup.sh` |
| `almanar-challenge-backup-verify.timer` | Sunday 03:15 | `deploy/verify-latest-backup.sh` |

Both carry `Persistent=true`, so a missed run is caught up on the next boot
rather than lost. The backup timer also carries `RandomizedDelaySec=15m` so this
server does not dump its database at the same instant as everything else
scheduled for the small hours.

### Where backups go

| Installed as | Backup directory |
| --- | --- |
| `root` (recommended on the server) | `/var/backups/almanar-interactive-challenge/` |
| a normal user | `/opt/almanar/backups/almanar-interactive-challenge/` |

Both are **outside the source tree**, so a dump can never be picked up by
`git add -A` and never sits inside a directory a `git checkout` can touch. The
script writes a config file with these values and both `backup.sh` and the
verification read it, so a scheduled run and a manual run always agree on where
dumps live.

Override either before installing:

```bash
BACKUP_DIR=/mnt/usb/backups KEEP_DAYS=14 ./deploy/install-backup-timer.sh
```

### Retention, checksums and verification

`KEEP_DAYS` defaults to **30** and is enforced by `backup.sh`, which also writes
a SHA-256 beside every dump and checks the archive is readable before reporting
success. The weekly timer goes further: it restores the newest dump into a
**scratch** database, compares every table against the live one, and drops the
scratch database. The live database is never modified.

### Logs and failure visibility

```bash
systemctl status almanar-challenge-backup.timer
journalctl -u almanar-challenge-backup.service -n 50
journalctl -u almanar-challenge-backup-verify.service -n 50
```

A failed backup exits non-zero, so the timer records the failure. There is
deliberately no notification destination configured — no mail relay, no remote
host — because an unattended notification to an address nobody set up is not
monitoring. To be told, set `OnFailure=` on the unit to something that reaches
you.

For a non-root install, use `systemctl --user` and `journalctl --user`. The
installer runs `loginctl enable-linger` so the timer survives logout and
reboots; confirm with `loginctl show-user "$USER" -p Linger`.

#### A user-level install needs Docker group membership

`backup.sh` takes the dump with `docker exec ... pg_dump`, so whoever runs the
timer has to be allowed to talk to the Docker socket — in practice a member of
the `docker` group.

The subtlety: a systemd **user** manager started *before* you were added to that
group keeps its old group set, and every service it runs fails with

```
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
```

even though `docker ps` works in your shell. Log out and back in, or, without
disrupting your session:

```bash
systemctl --user daemon-reexec
pgrep -u "$USER" -x systemd | head -1 | \
  xargs -I{} grep -E '^Groups' /proc/{}/status     # 983 must be in the list
```

Check that before trusting a user-level schedule. A system-level install run as
root has no such problem, which is the other reason to prefer it.

### Checking it works

Do not wait for the first scheduled run to find out:

```bash
systemctl start almanar-challenge-backup.service
journalctl -u almanar-challenge-backup.service -n 30
```

### Removing the schedule

```bash
./deploy/install-backup-timer.sh --uninstall   # keeps existing dumps
./deploy/install-backup-timer.sh --status      # show what is installed
```

### Restoring from a scheduled backup

```bash
ls -t /var/backups/almanar-interactive-challenge/*.dump | head -1
./deploy/restore.sh /var/backups/almanar-interactive-challenge/challenge-<stamp>.dump --verify
./deploy/restore.sh /var/backups/almanar-interactive-challenge/challenge-<stamp>.dump --live --confirm
```

### cron, if you prefer it

```cron
# Nightly backup at 02:15
15 2 * * * cd /opt/almanar/challenge && BACKUP_DIR=/var/backups/almanar-interactive-challenge ./deploy/backup.sh >> /var/log/challenge-backup.log 2>&1

# Weekly restore verification, Sunday 03:40
40 3 * * 0 cd /opt/almanar/challenge && BACKUP_DIR=/var/backups/almanar-interactive-challenge ./deploy/verify-latest-backup.sh >> /var/log/challenge-restore-check.log 2>&1
```

Unlike the timers, neither cron entry runs a backup that was missed while the
machine was off.

---

## The uploaded question images

`backup.sh` covers the database only. Question images live in the
`challenge-media` Docker volume.

```bash
docker run --rm \
  -v almanar-interactive-challenge_challenge-media:/data:ro \
  -v "$PWD/backups":/backup \
  alpine tar czf /backup/media-$(date +%Y%m%d-%H%M%S).tar.gz -C /data .
```

Check the volume name first with `docker volume ls | grep media`. Restore with
`tar xzf` into the same volume.

Worth keeping in step with the database dump: an image whose question row is
missing is a broken question.

---

## Keeping a copy off the server

A backup on the same disk as the database is not a backup — one disk failure
loses both. Copy the `backups` directory to a USB drive, an external disk or a
network share on a schedule, and **keep at least one copy somewhere the server
cannot reach**. Test a restore from that copy, not from the working directory.