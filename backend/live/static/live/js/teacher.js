/*
 * The teacher's live dashboard.
 *
 * One rule drives this file: the buttons do not decide what is possible. The
 * server state does. Each control declares the states in which it is meaningful,
 * and the page enables or disables it from the last state the server reported.
 *
 * That distinction matters operationally. If the browser guessed, a teacher
 * whose laptop had just reconnected would see controls for a state the server
 * was not in, press one, and be told no - during a lesson, in front of a room.
 * So the page only ever *disables* what the server has already said is not
 * available; it never enables anything the server has not offered.
 *
 * A control that the server refuses is reported back and shown, rather than
 * swallowed. A silent failure during a live competition is indistinguishable
 * from a broken dashboard.
 */

(function () {
  "use strict";

  var root = document.querySelector(".live-teacher");
  if (!root) {
    return;
  }

  var path = root.getAttribute("data-websocket-path");
  var socket = null;
  var retryTimer = null;
  var clockSkewSeconds = 0;
  var endsAt = null;
  var countdownTimer = null;

  function role(name) {
    return root.querySelector('[data-role="' + name + '"]');
  }

  /*
   * The states each control is meaningful in. `not_started` is a browser-side
   * description of a competition with no `started_at`, which the server reports
   * as `waiting` once it has started - so the dashboard needs to know the
   * difference and cannot read it from the state name alone.
   */
  function controlStates(control) {
    return (control.getAttribute("data-requires") || "").split(",");
  }

  /*
   * The state the controls are resolved against.
   *
   * `state_sync` carries both `state` and `started_at`, and the pair is what
   * distinguishes a round that has never started from one that has started and
   * is between questions - the model reports both as `waiting`. This mirrors
   * `live.views.control_state_for`, so the server-rendered page and the live
   * socket enable exactly the same controls.
   */
  function controlStateFor(payload) {
    var state = payload && payload.state;
    if (!state) {
      return null;
    }
    if (state === "waiting" && payload.started_at === null) {
      return "not_started";
    }
    return state;
  }

  function setState(state) {
    root.dataset.state = state;

    var name = role("state-name");
    if (name) {
      name.textContent = state ? state.replace(/_/g, " ") : "-";
    }

    var controls = root.querySelectorAll("[data-control]");
    for (var i = 0; i < controls.length; i++) {
      var allowed = controlStates(controls[i]);
      controls[i].disabled = allowed.indexOf(state) === -1;
    }
  }

  function showError(message) {
    var node = role("error");
    if (!node) {
      return;
    }
    if (message) {
      node.textContent = message;
      node.hidden = false;
    } else {
      node.hidden = true;
    }
  }

  function setText(name, value) {
    var node = role(name);
    if (node) {
      node.textContent = value;
    }
  }

  function applyServerTime(isoString) {
    var serverSeconds = Date.parse(isoString) / 1000;
    if (!isNaN(serverSeconds)) {
      clockSkewSeconds = serverSeconds - Date.now() / 1000;
    }
  }

  function applyEnvelope(payload) {
    if (typeof payload.server_time === "string") {
      applyServerTime(payload.server_time);
    }
    if (payload.state) {
      setState(controlStateFor(payload) || payload.state);
    }
    if (typeof payload.question_number === "number" && payload.total_questions) {
      setText("question-number", payload.question_number + " of " + payload.total_questions);
    }
    if (typeof payload.connected_screens === "number") {
      setText("connected-screens", String(payload.connected_screens));
    }
    if (typeof payload.seconds_remaining === "number") {
      setText("seconds-remaining", Math.ceil(payload.seconds_remaining) + "s");
    }

    endsAt =
      typeof payload.ends_at === "string" && payload.ends_at
        ? Date.parse(payload.ends_at) / 1000
        : null;
    startCountdown();
  }

  function startCountdown() {
    stopCountdown();
    if (!endsAt) {
      setText("remaining", "");
      return;
    }
    tickCountdown();
    countdownTimer = window.setInterval(tickCountdown, 500);
  }

  function stopCountdown() {
    if (countdownTimer !== null) {
      window.clearInterval(countdownTimer);
      countdownTimer = null;
    }
  }

  function tickCountdown() {
    var remaining = Math.max(0, endsAt - (Date.now() / 1000 + clockSkewSeconds));
    setText("remaining", " " + Math.ceil(remaining) + "s");
  }

  root.addEventListener("click", function (event) {
    var button = event.target.closest("[data-control]");
    if (!button || button.disabled || !socket) {
      return;
    }
    // The message carries the action name and nothing else. The server reloads
    // the competition, re-checks that this user may drive it, and performs the
    // transition - so a tampered or replayed message from another teacher, or
    // from a browser console, achieves nothing.
    socket.send(JSON.stringify({ type: button.getAttribute("data-control") }));
  });

  function connect() {
    var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(scheme + "//" + window.location.host + path);

    socket.addEventListener("open", function () {
      showError(null);
    });

    socket.addEventListener("message", function (event) {
      var payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      handle(payload);
    });

    socket.addEventListener("close", function () {
      // Every control is disabled on a dropped socket. The dashboard does not
      // know the round's state any more, and a teacher pressing a button into a
      // void is worse than a button that visibly cannot be pressed.
      var controls = root.querySelectorAll("[data-control]");
      for (var i = 0; i < controls.length; i++) {
        controls[i].disabled = true;
      }
      showError("Disconnected from the server. Reconnecting…");
      window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(connect, 3000);
    });
  }

  function handle(payload) {
    if (!payload || !payload.type) {
      return;
    }

    if (payload.type === "error") {
      // A refused action, reported rather than swallowed.
      showError(payload.message || "That action was refused.");
      return;
    }

    if (payload.type === "presence") {
      if (typeof payload.connected_screens === "number") {
        setText("connected-screens", String(payload.connected_screens));
      }
      return;
    }

    if (payload.type === "competition_started") {
      showError(null);
    }

    // Every state-carrying event refreshes the same read-only figures, so there
    // is one place that decides what the dashboard shows.
    applyEnvelope(payload);
  }

  // Until the socket says otherwise, no control is available.
  setState("connecting");
  connect();
})();