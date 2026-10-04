/*
 * Screen heartbeat.
 *
 * Reports that the screen is present, so that staff can see whether it is
 * online. It deliberately reports presence and nothing else: no identity,
 * classroom or configuration is sent or changed from the browser.
 *
 * Uses fetch with Django's CSRF token, because the heartbeat endpoint is a POST
 * and must not be reachable through a cross-site request.
 */
(function () {
  "use strict";

  var HEARTBEAT_URL = "/screen/heartbeat/";
  var DEFAULT_INTERVAL_MS = 30000;
  var RETRY_DELAY_MS = 10000;

  var root = document.querySelector("[data-screen-id]");
  if (!root) {
    return;
  }

  var screenId = root.getAttribute("data-screen-id");
  var intervalMs = parseInt(root.getAttribute("data-heartbeat-interval"), 10) * 1000;
  if (isNaN(intervalMs) || intervalMs < 5000) {
    intervalMs = DEFAULT_INTERVAL_MS;
  }

  var csrfToken = readCookie("csrftoken") || "";

  function readCookie(name) {
    var cookies = document.cookie ? document.cookie.split(";") : [];
    for (var i = 0; i < cookies.length; i++) {
      var cookie = cookies[i].trim();
      if (cookie.indexOf(name + "=") === 0) {
        return decodeURIComponent(cookie.substring(name.length + 1));
      }
    }
    return null;
  }

  function setStatus(text, online) {
    var badge = document.getElementById("screen-status");
    if (!badge) {
      return;
    }
    badge.textContent = text;
    badge.classList.toggle("status-online", online);
    badge.classList.toggle("status-offline", !online);
  }

  function send() {
    var body = new URLSearchParams();
    body.set("screen_id", screenId);

    return fetch(HEARTBEAT_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrfToken
      },
      body: body.toString()
    }).then(function (response) {
      // 404 means this screen is no longer registered. Stop polling instead of
      // hammering the server, and let the operator see that it stopped.
      if (!response.ok) {
        throw new Error("heartbeat rejected");
      }
      return response.json();
    });
  }

  function schedule(delay) {
    window.setTimeout(function () {
      send()
        .then(function (payload) {
          var online = payload.status === "online";
          setStatus(online ? "Online" : "Offline", online);
          schedule(intervalMs);
        })
        .catch(function () {
          setStatus("Offline", false);
          schedule(RETRY_DELAY_MS);
        });
    }, delay);
  }

  send()
    .then(function (payload) {
      var online = payload.status === "online";
      setStatus(online ? "Online" : "Offline", online);
      schedule(intervalMs);
    })
    .catch(function () {
      setStatus("Offline", false);
      schedule(RETRY_DELAY_MS);
    });
})();