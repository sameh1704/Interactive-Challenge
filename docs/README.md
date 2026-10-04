# Al Manar Interactive Challenge — Documentation

Production documentation for the Ubuntu server. Read the one you need; they are
written to be used in order during setup and then kept as reference.

## Start here

| Document | Read this when |
| --- | --- |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Installing on the school server for the first time |
| [ADMINISTRATOR.md](ADMINISTRATOR.md) | Setting up accounts, classrooms and screens |
| [TEACHER.md](TEACHER.md) | Running a competition |
| [SCREEN-SETUP.md](SCREEN-SETUP.md) | Installing a screen in a classroom |
| [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md) | Before asking the network team for firewall rules |
| [OPERATIONS.md](OPERATIONS.md) | Day-to-day running, updates, rollback |
| [BACKUP-RESTORE.md](BACKUP-RESTORE.md) | Taking, validating and restoring backups |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Something is wrong and you need a cause |
| [SECURITY.md](SECURITY.md) | Reviewing the security model |
| [PERFORMANCE.md](PERFORMANCE.md) | Sizing, and understanding measured limits |
| [PILOT.md](PILOT.md) | Running the cross-VLAN acceptance test on the real network |

## What this is

A classroom competition system. A teacher drives a live question round from a
browser; students answer from interactive screens; scores are computed on the
server and revealed at the end of each question. Championships run several rounds
together and keep a permanent record.

Everything authoritative happens on the server. Screens are display devices: they
identify themselves with a Screen ID and cannot do anything a browser could not
already do from the same page.

## The short version for an administrator

1. Install on the server: [DEPLOYMENT.md](DEPLOYMENT.md)
2. Create an administrator and classrooms: [ADMINISTRATOR.md](ADMINISTRATOR.md)
3. Register each screen and note its Screen ID: [SCREEN-SETUP.md](SCREEN-SETUP.md)
4. Ask the network team for the rules in [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md)
5. Take the first backup: [BACKUP-RESTORE.md](BACKUP-RESTORE.md)
6. Every night, keep the backup going (§ in BACKUP-RESTORE.md)

## Status

**The application itself is verified** — the full automated suite, the networked
live checks, and load runs at 5, 10 and 20 screens all pass. See
[PERFORMANCE.md](PERFORMANCE.md) for the recorded figures and what they do and do
not cover.

**Two things have not been done**, and both need the school network or the target
server:

1. **Deployment to the Ubuntu server has not been executed.** The deployment is
   documented and its configuration is validated, but it has not been run on the
   target machine.
2. **The cross-VLAN pilot has not been performed.** No screen on a second
   building VLAN has been tested. See [PILOT.md](PILOT.md) for the procedure and
   [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md) § "What still has to be
   tested on the real network" for the checklist.

Until at least two screen VLANs have passed the pilot, do not rely on this in a
live lesson across buildings.
