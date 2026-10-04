# Administrator Guide

Setting up and administering the application. No programming needed.

---

## 1. Sign in

Open the application URL and sign in with an administrator account.

Only accounts with the **Administrator** role can reach `/admin/`, create other
accounts, and register classrooms and screens. A **Teacher** sees only their own
competitions and reports — that limit is enforced on the server, not hidden in
the interface.

---

## 2. Create staff accounts

**Admin → Users → Add user**

| Field | Value |
| --- | --- |
| Username | Something a teacher can type without capitals or spaces |
| Role | `teacher` or `administrator` |
| Password | At least 10 characters, unique to this system |

There is no self-service registration. Nobody can create an account except an
administrator, so there is no open door to discover.

**Share passwords privately.** Every account is a real account: it can start
competitions, read the question bank and see results.

### Forgotten passwords

An administrator resets it in `/admin/`. There is no email reset: the school
server has no outbound mail configured, and that is intentional.

Repeated wrong passwords are counted per username and per address. After 10
attempts in 5 minutes the address is refused until the window passes. The
message says the same thing whether the account exists or the password was
wrong, so this cannot be used to discover which accounts exist.

---

## 3. Create classrooms

**Admin → Classrooms → Add classroom**

| Field | Value |
| --- | --- |
| Name | As staff will say it out loud, e.g. `Science Lab A` |
| Grade | e.g. `Grade 5` |
| Building | e.g. `Main` |

The **display name** shown on screens combines these. `Grade 5 - Science Lab A`
sorts near other Grade 5 rooms.

To give a teacher only their own rooms, add them to the classroom's
**teachers**. Administrators see every room regardless.

---

## 4. Register an interactive screen

**Admin → Interactive screens → Add interactive screen**

| Field | Value |
| --- | --- |
| Name | Where it is, e.g. `Front interactive board` |
| Classroom | The room it is in |
| Primary screen | Tick if it is the main display for that room |

Save it and **write down the Screen ID** — it looks like `AM-7KQ4XB`.

> **The Screen ID is the screen's only credential.** Anyone who has it can answer
> as that room. Treat it like a password: do not post it in a public channel, and
> do not let students photograph it. See [SCREEN-SETUP.md](SCREEN-SETUP.md).

The ID is generated once and cannot be edited. If a screen is retired, **untick
Active** rather than deleting it: retiring keeps its competition history and makes
the old ID stop working immediately.

---

## 5. The question bank

**Admin → Questions** — or use the Question Bank page as a signed-in teacher.

A question has a type, an answer key and a duration:

- **Multiple choice** — 2 to 6 options, one correct. Readable from the back of a
  room.
- **True or false** — no options needed.
- **Ordering** — the correct order of every item.
- **Classification** — which items belong in which groups.
- **Open** — free text, compared case-insensitively after trimming.

Duration is per question, and a competition may override it.

### Answer keys are never sent to a screen while answering is open

This is enforced server-side. A screen receives the question without the key, and
the key arrives only in the reveal after answering closes. No configuration
change can expose it.

### Images

JPEG, PNG, GIF or WebP only. Anything else is rejected on upload, and the image
bytes themselves are checked as well as the extension. Uploaded images are served
with `X-Content-Type-Options: nosniff` and are never executed as anything but an
image.

---

## 6. Scoring rules

**Admin → Scoring rules.** Each competition uses its own rule, or the system
default if it has none.

| Setting | Meaning |
| --- | --- |
| Correct points | Points for a correct answer |
| Speed bonus maximum | Most extra points available for answering quickly |
| Speed bonus mode | `linear` pays the bonus in full at the start of a question, decaying to nothing by its deadline; `none` disables it |

Ranking is by **score, then number of correct answers, then lower average
response time**. Genuine ties share a rank, so two classrooms on equal points and
equal timing are genuinely equal.

---

## 7. Championships

**Championships** in the main menu.

1. Create one — a name and a season.
2. Add classrooms. They must be classrooms you are allowed to manage.
3. Add stages: `qualification` advances a set number of classrooms;
   `semi_final`, `third_place`, `final` and `championship` each have their own
   rules. **Final** and **championship** stages advance everyone who reaches them.
4. Attach a finished competition to a stage. A round belongs to exactly one stage,
   so it cannot be counted twice.
5. Complete the stage, then advance: the top classrooms are marked qualified, the
   rest eliminated.
6. Finalise when the last stage completes. The winner is recorded and the
   championship appears in the Hall of Fame.

A concluded championship refuses further changes, and the frozen record keeps the
final table exactly as it stood.

---

## 8. Reports

**Reports** covers competitions, classrooms, tournaments and questions. Each
respects the same access rule as the live pages: a teacher sees their own, an
administrator sees everything. Every report downloads as CSV with the same rows
the screen shows, generated by the same code.

---

## 9. Monitoring screens

**Admin → Interactive screens** lists every screen with:

- **Status** — Online when a heartbeat arrived within the last 90 seconds.
- **Last seen** — when it was last heard from.
- **IP address** — **monitoring only.** It is not the screen's identity.

If a screen shows Offline, check in this order: is the display powered on; is the
browser open at the right URL; does that VLAN reach the server (see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md)); is the Screen ID still active.

---

## 10. Routine care

| How often | Do |
| --- | --- |
| Daily | Glance at the screens list for anything persistently Offline |
| Weekly | Run a backup verification ([BACKUP-RESTORE.md](BACKUP-RESTORE.md)) |
| Weekly | Read the logs for failed sign-ins |
| Each term | Retire screens for rooms that are no longer used |
| Each term | Review which teachers should have accounts |

Check the health endpoint from a browser; it should say `"status": "ok"`. If it
says `unavailable`, the named dependency tells you which one.