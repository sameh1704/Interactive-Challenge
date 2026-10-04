# Interactive Screen Setup

Installing a classroom display so it can take part in competitions.

---

## What a screen is

A screen is a large display running a web browser. It needs:

- a device with a browser — a board, a TV with a stick PC, a laptop, a tablet
- a permanent network address (or none — a DHCP address is fine)
- a URL with its **Screen ID**

There is nothing to install. The screen has no account, no password and no
software.

---

## 1. Register the screen

An administrator registers it (see [ADMINISTRATOR.md](ADMINISTRATOR.md) § 4) and
gives you the Screen ID, which looks like `AM-7KQ4XB`.

## 2. Open it on the screen device

In the browser on that device, go to:

```
https://<server>/live/screen/?screen_id=AM-7KQ4XB
```

Substitute the real server address. The page should show the classroom name and
`Offline` until the competition starts.

**Enter this URL exactly once and leave it open.** The screen keeps itself
current; do not refresh during a lesson.

> The Screen ID is the screen's only credential. Anyone who has it can answer as
> that room. Do not post it publicly, and keep it off shared screens where
> students can photograph it.

## 3. Make it permanent

| Task | How |
| --- | --- |
| Start on boot | Set the browser to launch on startup and restore the last session, or add the URL to the browser's start pages |
| Full screen | Press F11, or the browser's full-screen control |
| Keep the screen awake | The application's screen page keeps the display awake itself — no screensaver setting is needed |
| Hide the address bar | Full-screen mode hides it, so the Screen ID is not on screen |
| Prevent sleep | A screensaver that blanks the display will interrupt a lesson. Disable it for the browser or the device |

### Recommended browser settings

- **Zoom** to the display's resolution so the question fills the panel.
- **Do not** open developer tools; they overlay the content.
- **Do not** let the browser restore "continue where you left off" onto a *different*
  page. Pin this one URL as a start page.

## 4. Check it is connected

The teacher's dashboard shows a **connected screens** count. It should rise to
include this room as soon as a competition starts.

The screen also reports presence on its own every 30 seconds. An administrator
sees it as **Online** in `/admin/` → Interactive screens.

## 5. What the screen shows

- While waiting: the classroom name and a waiting message
- On question start: the question, its options, and the server's countdown
- As classrooms answer: a **count** only
- On reveal: the correct answer and the per-classroom breakdown
- During a result: the leaderboard
- At the end: the final standings

It never shows the answer key while a question is open. That is enforced by the
server, not by the page.

---

## Recovery

| Situation | What to do |
| --- | --- |
| Page shows "not registered" | The URL or the Screen ID is wrong, or the screen was deactivated. Unknown and inactive give the same message. |
| Page loads, never connects | Network problem between that building and the server. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md). |
| Screen froze mid-lesson | Refresh. It rejoins with the correct state and remaining time; nothing is lost. |
| Screen shows a stale question | Its WebSocket dropped. Refresh. |
| Screen shows a whole competition it should not | Wrong Screen ID. Re-open the correct URL. |

The screen reconnects on its own with a growing delay — 2 seconds up to 15 — so a
brief network interruption heals without anyone touching the screen.

---

## Moving or retiring a screen

- **Moving rooms:** create a new screen for the new room, and retire the old one by
  unticking **Active**. Do not reuse an ID across rooms.
- **Retiring:** untick **Active** rather than deleting. The history is kept, and
  the old ID stops working immediately.

## Identity and IP addresses

A screen's identity is its Screen ID, and nothing else. Its IP address is recorded
purely so an administrator can see what is happening on the network.

So: DHCP re-addressing a screen changes nothing; two screens behind one NAT
address remain separate; and a screen moved to another building keeps working. If
identity came from the IP address, a DHCP lease change during a lesson could
silently move a room's answers to a different device.

Screen identity works across VLANs because it is never derived from the network.
See [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md).