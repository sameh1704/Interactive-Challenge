/*
 * The classroom screen.
 *
 * This is a display, not an application. Its job is to show what the server says
 * and to send back exactly one thing - the answer a pupil chose. Three rules are
 * enforced here in the browser as well as on the server, because a screen that
 * looked interactive after the teacher closed the question would be a lie the
 * room can see:
 *
 *   1. Nothing is shown or hidden on the browser's own judgement. The visible
 *      state follows the last server event, never a local countdown.
 *   2. No answer key exists in this file, and none is read from any event before
 *      `result_revealed`. There is no client-side scoring anywhere.
 *   3. Once an answer is sent, the controls are disabled and stay disabled. The
 *      server would refuse a second answer anyway; refusing here means the
 *      screen does not pretend otherwise.
 *
 * The countdown is drawn locally for smoothness, but derived only from the
 * server's `ends_at` and the offset between the server clock and this browser's,
 * both of which the server supplies. If the two ever disagree the countdown is
 * wrong only by the disagreement, never by this browser's clock drifting.
 */

(function () {
  "use strict";

  var RECONNECT_DELAY_MS = 3000;
  var URGENT_SECONDS = 5;

  var root = document.body;
  var path = root.getAttribute("data-websocket-path");
  var screenId = root.getAttribute("data-screen-id");
  if (!path || !screenId) {
    return;
  }

  var socket = null;
  var retryTimer = null;

  /*
   * Everything about the question currently on screen. Replaced wholesale by
   * `question_started` rather than mutated in place, so a message belonging to a
   * previous question can never be half-applied to the next one.
   */
  var question = null;
  var selection = null;
  var answered = false;
  var awaitingAck = false;
  var classroomId = null;

  var clockSkewSeconds = 0;
  var endsAt = null;
  var countdownTimer = null;

  function role(name) {
    return root.querySelector('[data-role="' + name + '"]');
  }

  function setConnection(text, state) {
    var badge = role("connection");
    if (badge) {
      badge.textContent = text;
      badge.dataset.state = state;
    }
  }

  function show(name) {
    var panels = root.querySelectorAll("[data-panel]");
    for (var i = 0; i < panels.length; i++) {
      panels[i].hidden = panels[i].getAttribute("data-panel") !== name;
    }
    root.dataset.state = name;
  }

  // -- the countdown ------------------------------------------------------

  function startCountdown() {
    stopCountdown();
    if (!endsAt) {
      return;
    }
    tickCountdown();
    countdownTimer = window.setInterval(tickCountdown, 250);
  }

  function stopCountdown() {
    if (countdownTimer !== null) {
      window.clearInterval(countdownTimer);
      countdownTimer = null;
    }
  }

  function tickCountdown() {
    var timer = role("timer");
    if (!timer) {
      return;
    }
    var remaining = Math.max(0, endsAt - (Date.now() / 1000 + clockSkewSeconds));
    timer.textContent = String(Math.ceil(remaining));
    // Urgency is a visual warning only. A countdown reaching zero does not close
    // answering; the server does that, and announces it with an event.
    timer.dataset.urgent = remaining <= URGENT_SECONDS ? "true" : "false";
  }

  function showTimer(label) {
    var timer = role("timer");
    var progress = role("progress");
    if (timer) {
      timer.hidden = !label;
      if (!label) {
        timer.textContent = "--";
        timer.dataset.urgent = "false";
      }
    }
    if (progress) {
      progress.hidden = !label;
      if (label) {
        progress.textContent = label;
      }
    }
  }

  // -- answers ------------------------------------------------------------

  function showLocked(text) {
    var node = role("locked-text");
    if (node) {
      node.textContent = text;
    }
    show("locked");
  }

  function markAnswered() {
    answered = true;
    awaitingAck = false;
    stopCountdown();
    showLocked("Answer sent. Waiting for the teacher.");
  }

  function disableControls() {
    var controls = root.querySelectorAll(
      ".screen__option, .screen__order-move, .screen__chip, .screen__place, .screen__submit, .screen__input"
    );
    for (var i = 0; i < controls.length; i++) {
      controls[i].disabled = true;
    }
  }

  /*
   * Send the answer.
   *
   * Deliberately the only outbound message after the handshake. It carries the
   * choice and nothing else - no classroom, no question, no score, no time. The
   * server derives every other value, and `scoring.services.extract_selection`
   * reads nothing but this field.
   */
  function submitAnswer() {
    if (answered || awaitingAck || selection === null || !socket) {
      return;
    }
    awaitingAck = true;
    disableControls();
    socket.send(JSON.stringify({ type: "submit_answer", answer: selection }));
  }

  // -- rendering each question type ---------------------------------------

  function currentPresentation() {
    return (question && question.presentation) || {};
  }

  function clearTypeAreas() {
    [
      "options",
      "ordering",
      "classification",
      "item-pool",
      "shortanswer"
    ].forEach(function (name) {
      var node = role(name);
      if (node) {
        node.hidden = true;
        node.textContent = "";
      }
    });
    var submit = role("submit");
    if (submit) {
      submit.hidden = true;
    }
  }

  function renderQuestion() {
    var text = role("question-text");
    var image = role("question-image");

    if (text) {
      text.textContent = (question && question.text) || "";
    }

    if (image) {
      if (question && question.image_url) {
        image.src = question.image_url;
        image.hidden = false;
      } else {
        image.removeAttribute("src");
        image.hidden = true;
      }
    }

    clearTypeAreas();

    switch (question.question_type) {
      case "ordering":
        renderOrdering();
        break;
      case "classification":
        renderClassification();
        break;
      case "short_answer":
        renderShortAnswer();
        break;
      default:
        renderOptions();
        break;
    }

    show("question");
  }

  function renderOptions() {
    var container = role("options");
    var submit = role("submit");
    if (!container) {
      return;
    }

    var choices =
      question.question_type === "true_false" ? ["True", "False"] : question.options || [];

    choices.forEach(function (choice, index) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "screen__option";
      button.setAttribute("data-selected", "false");
      button.addEventListener("click", function () {
        selection = choice;
        var buttons = container.querySelectorAll(".screen__option");
        for (var i = 0; i < buttons.length; i++) {
          buttons[i].setAttribute(
            "data-selected",
            i === index ? "true" : "false"
          );
        }
        if (submit) {
          submit.hidden = false;
        }
      });

      var key = document.createElement("span");
      key.className = "screen__option-key";
      key.textContent = String.fromCharCode(65 + index);
      button.appendChild(key);

      var label = document.createElement("span");
      label.className = "screen__option-label";
      label.textContent = choice;
      button.appendChild(label);

      container.appendChild(button);
    });

    container.hidden = false;
  }

  /*
   * Ordering uses explicit up/down buttons rather than drag and drop. Drag is
   * unreliable on the touch hardware these boards actually use, and a pupil who
   * has just missed a drop should be able to see that the item moved anyway.
   */
  function renderOrdering() {
    var container = role("ordering");
    var submit = role("submit");
    if (!container) {
      return;
    }

    // The working order starts as the order it was sent in, and is the answer.
    selection = currentPresentation().items ? currentPresentation().items.slice() : [];

    function paint() {
      container.textContent = "";

      selection.forEach(function (item, index) {
        var row = document.createElement("li");
        row.className = "screen__order-item";

        var label = document.createElement("span");
        label.className = "screen__order-label";
        label.textContent = item;
        row.appendChild(label);

        var up = document.createElement("button");
        up.type = "button";
        up.className = "screen__order-move";
        up.textContent = "▲";
        up.setAttribute("aria-label", "Move " + item + " up");
        up.disabled = index === 0;
        up.addEventListener("click", function () {
          move(index, index - 1);
        });
        row.appendChild(up);

        var down = document.createElement("button");
        down.type = "button";
        down.className = "screen__order-move";
        down.textContent = "▼";
        down.setAttribute("aria-label", "Move " + item + " down");
        down.disabled = index === selection.length - 1;
        down.addEventListener("click", function () {
          move(index, index + 1);
        });
        row.appendChild(down);

        container.appendChild(row);
      });

      if (submit) {
        submit.hidden = selection.length === 0;
      }
    }

    function move(from, to) {
      if (to < 0 || to >= selection.length) {
        return;
      }
      var moved = selection.splice(from, 1)[0];
      selection.splice(to, 0, moved);
      paint();
    }

    paint();
    container.hidden = false;
  }

  /*
   * Classification is two taps: choose an item, then choose where it goes.
   * `placements` is the live answer object - it is mutated in place and referenced
   * by `selection`, so the button always sends the current state.
   *
   * The item pool only holds unplaced items, so a placed item leaves it and
   * reappears nowhere; tapping a placed chip removes it again, which is how a
   * pupil undoes a mistake without needing a reset control.
   */
  function renderClassification() {
    var container = role("classification");
    var pool = role("item-pool");
    var submit = role("submit");
    if (!container || !pool) {
      return;
    }

    var categories = currentPresentation().categories || [];
    var items = currentPresentation().items || [];

    var placements = {};
    var pending = null;
    selection = placements;

    function placePending(category) {
      if (pending === null) {
        return;
      }
      placements[pending] = category;
      pending = null;
      paint();
    }

    function chip(label, placedIn) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "screen__chip";
      button.textContent = label;
      button.setAttribute("data-placed", placedIn ? "true" : "false");
      button.setAttribute("data-pending", pending === label ? "true" : "false");
      button.addEventListener("click", function () {
        if (placedIn) {
          // Tapping a placed item takes it back out.
          delete placements[label];
          pending = null;
        } else {
          pending = pending === label ? null : label;
        }
        paint();
      });
      return button;
    }

    function paint() {
      container.textContent = "";
      pool.textContent = "";

      categories.forEach(function (category) {
        var group = document.createElement("div");
        group.className = "screen__category";

        var name = document.createElement("p");
        name.className = "screen__category-name";
        name.textContent = category;
        group.appendChild(name);

        var held = document.createElement("div");
        held.className = "screen__category-items";
        items.forEach(function (item) {
          if (placements[item] === category) {
            held.appendChild(chip(item, category));
          }
        });
        group.appendChild(held);

        // The drop target appears only when there is something to place, so the
        // screen does not present six identical buttons most of the time.
        if (pending !== null) {
          var target = document.createElement("button");
          target.type = "button";
          target.className = "screen__place";
          target.textContent = "Place “" + pending + "” in " + category;
          target.addEventListener("click", function () {
            placePending(category);
          });
          group.appendChild(target);
        }

        container.appendChild(group);
      });

      items.forEach(function (item) {
        if (!placements[item]) {
          pool.appendChild(chip(item, null));
        }
      });

      if (submit) {
        submit.hidden = Object.keys(placements).length !== items.length;
      }
    }

    paint();
    container.hidden = false;
    pool.hidden = false;
  }

  function renderShortAnswer() {
    var form = role("shortanswer");
    var input = role("shortanswer-input");
    if (!form || !input) {
      return;
    }
    var presentation = currentPresentation();
    // Generous headroom over the longest accepted answer: a pupil should not be
    // stopped mid-word by a limit derived from the key.
    input.maxLength = Math.max(80, (presentation.max_length || 0) + 60);
    form.hidden = false;

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!input.value.trim()) {
        return;
      }
      selection = input.value;
      submitAnswer();
    });
  }

  // The send button, for every type except short answer (which uses its form).
  root.addEventListener("click", function (event) {
    var button = event.target.closest('[data-role="submit"]');
    if (button) {
      submitAnswer();
    }
  });

  // -- server events ------------------------------------------------------

  function applyServerTime(isoString) {
    var serverSeconds = Date.parse(isoString) / 1000;
    if (!isNaN(serverSeconds)) {
      clockSkewSeconds = serverSeconds - Date.now() / 1000;
    }
  }

  function applyQuestion(payload) {
    question = payload;
    answered = false;
    awaitingAck = false;
    selection = null;

    endsAt = payload && typeof payload.ends_at === "string" && payload.ends_at
      ? Date.parse(payload.ends_at) / 1000
      : null;

    showTimer(
      payload && payload.total_questions
        ? "Question " +
            payload.question_number +
            " of " +
            payload.total_questions
        : null
    );

    renderQuestion();
    startCountdown();
  }

  function showClosed() {
    stopCountdown();
    showTimer(null);
    if (answered) {
      showLocked("Answer sent. Waiting for the result.");
    } else {
      show("closed");
    }
  }

  function showFinished(payload) {
    stopCountdown();
    showTimer(null);
    var summary = role("final-summary");
    if (summary) {
      summary.textContent =
        payload && payload.competition_title
          ? "That is the end of " + payload.competition_title + "."
          : "That is the end of the competition.";
    }
    show("finished");
  }

  function renderBoard(rows) {
    var board = role("board");
    if (!board) {
      return;
    }
    board.textContent = "";

    (rows || []).forEach(function (row) {
      var item = document.createElement("li");
      item.className = "screen__board-row";
      if (row.classroom_id === classroomId) {
        item.dataset.self = "true";
      }

      var rank = document.createElement("span");
      rank.className = "screen__board-rank";
      rank.textContent = String(row.rank);
      item.appendChild(rank);

      var name = document.createElement("span");
      name.className = "screen__board-name";
      name.textContent = row.classroom;
      item.appendChild(name);

      var score = document.createElement("span");
      score.className = "screen__board-score";
      score.textContent = String(row.score);
      item.appendChild(score);

      board.appendChild(item);
    });

    show("board");
  }

  function applyResult(payload) {
    if (!payload) {
      return;
    }
    stopCountdown();
    showTimer(null);

    var mine = null;
    (payload.answers || []).forEach(function (row) {
      if (row.classroom_id === classroomId) {
        mine = row;
      }
    });

    var verdict = role("verdict");
    var key = role("correct-answer");
    var explanation = role("explanation");

    if (verdict) {
      if (!mine) {
        verdict.textContent = "This classroom did not answer";
        verdict.dataset.correct = "false";
      } else if (mine.is_correct) {
        verdict.textContent = "Correct";
        verdict.dataset.correct = "true";
      } else {
        verdict.textContent = "Answer sent: " + (mine.selected_answer || "");
        verdict.dataset.correct = "false";
      }
      verdict.hidden = false;
    }

    // The key is only ever read from `result_revealed`. There is no other event
    // this function is called from, so there is no earlier moment at which the
    // browser could have a key in hand.
    if (key) {
      key.textContent = "Correct answer: " + (payload.correct_answer || "");
      key.hidden = !payload.correct_answer;
    }

    if (explanation) {
      explanation.textContent = payload.explanation || "";
      explanation.hidden = !payload.explanation;
    }

    show("result");
  }

  function applyStateSync(payload) {
    if (!payload) {
      return;
    }
    if (payload.screen) {
      classroomId = payload.screen.classroom;
    }
    if (typeof payload.server_time === "string") {
      applyServerTime(payload.server_time);
    }

    switch (payload.state) {
      case "question_active":
        if (payload.question) {
          applyQuestion(payload.question);
        }
        break;
      case "answering_closed":
        showClosed();
        break;
      case "finished":
        showFinished(payload);
        break;
      default:
        show(answered ? "locked" : "waiting");
        break;
    }
  }

  function handle(payload) {
    if (!payload || !payload.type) {
      return;
    }

    switch (payload.type) {
      case "welcome":
        show(answered ? "locked" : "waiting");
        break;

      case "state_sync":
        applyStateSync(payload);
        break;

      case "question_started":
        if (typeof payload.server_time === "string") {
          applyServerTime(payload.server_time);
        }
        applyQuestion(payload);
        break;

      case "answering_closed":
        showClosed();
        break;

      case "answer_accepted":
        markAnswered();
        break;

      case "answer_progress":
        // A count only. Deliberately not rendered as anything that hints at
        // whether an answer was right - the server sends nothing that would.
        break;

      case "result_revealed":
        applyResult(payload);
        break;

      case "leaderboard_updated":
        renderBoard(payload.rows);
        break;

      case "competition_results":
      case "competition_finished":
        showFinished(payload);
        break;

      case "competition_started":
      case "next_question":
        show(answered ? "locked" : "waiting");
        break;

      case "tick":
        if (typeof payload.server_time === "string") {
          applyServerTime(payload.server_time);
        }
        break;

      case "rejected":
        setConnection("Refused", "offline");
        show("offline");
        break;

      default:
        break;
    }
  }

  function connect() {
    var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    var url = scheme + "//" + window.location.host + path;
    // The Screen ID is this screen's credential, so it travels in the query
    // string exactly as the Phase 2 endpoints expect. It is deliberately absent
    // from the path: a screen must not be able to nominate a competition.
    url += "?screen_id=" + encodeURIComponent(screenId);

    socket = new WebSocket(url);

    socket.addEventListener("open", function () {
      setConnection("Live", "live");
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
      setConnection("Reconnecting…", "offline");
      if (!answered) {
        show("offline");
      }
      window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(connect, RECONNECT_DELAY_MS);
    });
  }

  show("connecting");
  connect();
})();