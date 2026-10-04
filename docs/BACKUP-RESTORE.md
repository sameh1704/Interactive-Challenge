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

A nightly backup with a weekly verification, as two entries in the server's
crontab. Adjust the schedule to suit:

```cron
# Nightly backup at 02:15
15 2 * * * cd /opt/almanar/challenge && ./deploy/backup.sh >> /var/log/challenge-backup.log 2>&1

# Weekly restore verification, Sunday 03:40
40 3 * * 0 cd /opt/almanar/challenge && ./deploy/restore.sh "$(ls -t backups/*.dump | head -1)" --verify >> /var/log/challenge-restore-check.log 2>&1
```

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