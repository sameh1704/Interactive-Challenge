# Teacher Guide

Running a competition. Everything here is done from a browser.

---

## Before the lesson

1. Open the application and sign in.
2. **Create a competition** — give it a name you will recognise in a report later.
3. **Add the classrooms** taking part.
4. **Add questions** from the question bank, and set the order.
5. Check each question: the answer key is right and the duration is sensible for
   the class.

Nothing is visible to students until you press Start.

---

## Starting the round

Press **Start competition**. From then on the live dashboard shows which screens
are attached.

**Check the connected screen count before you start.** It should equal the number
of classrooms you added. If it is lower, open the missing room's URL and confirm
the Screen ID.

You can start questions in any order, skip ahead, or end one early. You cannot
skip a question you have not reached by finishing the round, and you cannot
restart a question that has been revealed.

---

## During a question

The dashboard shows a countdown. That clock is **the server's**, not the
browser's, so every screen in every building counts down together and cannot
drift. A screen that was briefly disconnected rejoins with the correct time
remaining and no manual refresh.

**End Question** closes answering early. Everything downstream — the reveal, the
frozen results — happens the same way whether you pressed it or the clock ran
out, so nothing is lost by ending early.

---

## The reveal

When answering closes, the correct answer and the per-classroom breakdown appear.
Screens show the reveal at the same moment.

The leaderboard is ordered by score, then correct answers, then average response
time. Equal on all three is a genuine tie and shares a rank.

---

## Answering

Students answer on the screen in their room. One answer per classroom per
question — the first one submitted stands, and a second attempt is refused.

Scoring is decided on the server. The screen sends only the choice that was made;
score, correctness and response time are computed by the server from its own
clock and the stored answer key. A message that tries to include its own `score`,
`is_correct` or `response_time` has those fields ignored — they cannot influence
the result.

---

## Finishing

**Finish competition** ends the round. Final standings are frozen: what is
recorded is what happened, and later edits cannot change it.

Then check the **Reports** section. The competition report and its CSV show the
same rows.

---

## If something goes wrong

**A screen says "This screen is not registered."**
The URL is wrong, or the Screen ID is inactive. Unknown and inactive produce the
same message on purpose, so ask an administrator to check.

**A screen says "This classroom is not taking part in this competition."**
The screen is fine; that classroom was not added to this round. Add it, or use a
different screen.

**A screen will not connect but the page loads.**
Usually the network between that building and the server, or something stripping
the WebSocket upgrade. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

**Connected screens is lower than expected.**
A screen that lost its connection retries on its own with a growing delay. The
count is exact across the whole server.

**I pressed something and a red message appeared.**
The round is unchanged and safe. The message says why — for example that a
question is already revealed, or that another round is still running. The server
refuses the action and reports the reason; it never half-applies one.

**The dashboard stopped updating.**
Press F5. The page is rebuilt from the server's state on reload, so it recovers
at the correct point in the round. If sockets are broken the screens will not
recover on their own — check the logs, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Two rounds cannot run at once

Only one competition runs live at a time. If a previous round was not finished,
finish it first. This is a server rule, not a limitation of your dashboard.