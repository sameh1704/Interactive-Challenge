/*
 * Live leaderboard display.
 *
 * The table is rendered server-side first, so a display opened mid-round shows
 * the current standings immediately rather than an empty board waiting for a
 * socket. This script then keeps it current.
 *
 * The page is a display, not an operator console: it sends nothing except a
 * keepalive ping, and it renders only what the server sends. It never computes a
 * score or reorders a row itself - if the browser disagreed with the server
 * about the standings, the room would see two different answers.
 */

(function () {
  "use strict";

  var RECONNECT_DELAY_MS = 3000;

  var root = document.querySelector("[data-websocket-path]");
  if (!root) {
    return;
  }

  var body = root.querySelector('[data-role="rows"]');
  var connection = root.querySelector('[data-role="connection"]');
  var path = root.getAttribute("data-websocket-path");
  var socket = null;
  var retryTimer = null;

  function setConnection(text, state) {
    if (!connection) {
      return;
    }
    connection.textContent = text;
    connection.dataset.state = state;
  }

  function renderRows(rows) {
    if (!body) {
      return;
    }

    body.textContent = "";

    if (!rows || rows.length === 0) {
      var empty = document.createElement("tr");
      empty.setAttribute("data-role", "empty");
      var cell = document.createElement("td");
      cell.colSpan = 5;
      cell.textContent = "No classrooms are taking part yet.";
      empty.appendChild(cell);
      body.appendChild(empty);
      return;
    }

    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      tr.setAttribute("data-classroom-id", String(row.classroom_id));

      var rank = document.createElement("td");
      rank.className = "leaderboard__rank";
      rank.textContent = String(row.rank);
      tr.appendChild(rank);

      var name = document.createElement("th");
      name.scope = "row";
      name.className = "leaderboard__classroom";
      name.textContent = row.classroom;
      tr.appendChild(name);

      [
        ["score", row.score],
        ["correct_answers", row.correct_answers],
        ["answers_given", row.answers_given],
      ].forEach(function (pair) {
        var td = document.createElement("td");
        td.className = "leaderboard__numeric";
        td.setAttribute("data-field", pair[0]);
        td.textContent = String(pair[1]);
        tr.appendChild(td);
      });

      body.appendChild(tr);
    });
  }

  function apply(payload) {
    if (payload && Array.isArray(payload.rows)) {
      renderRows(payload.rows);
    }
  }

  function connect() {
    var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(scheme + "//" + window.location.host + path);

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

      switch (payload.type) {
        case "welcome":
        case "leaderboard_updated":
          apply(payload);
          break;
        case "result_revealed":
        case "competition_results":
          if (payload.leaderboard) {
            apply(payload.leaderboard);
          } else if (Array.isArray(payload.standings)) {
            renderRows(payload.standings);
          }
          break;
        default:
          break;
      }
    });

    socket.addEventListener("close", function () {
      setConnection("Reconnecting…", "offline");
      window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(connect, RECONNECT_DELAY_MS);
    });
  }

  connect();
})();