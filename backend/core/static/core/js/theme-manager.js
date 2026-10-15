/**
 * Light / dark theme switch.
 *
 * The server is the source of truth. It writes the active theme onto
 * `<html data-theme="...">` from the theme cookie, so the right colours are the
 * first thing painted and a browser with JavaScript switched off still gets the
 * theme it asked for. This file's job is only to keep the page and that cookie in
 * step when the switch is pressed, without reloading anything:
 *
 *   1. Click the switch. The theme is applied to the document immediately, so the
 *      teacher sees the change now rather than after a round trip.
 *   2. The cookie is written with exactly the attributes the server would use -
 *      read off the form, not repeated here - so the next request renders the
 *      same theme and the two never disagree about the name or the lifetime.
 *   3. Every page after that is already dark, whatever page the teacher lands on.
 *
 * Deliberately absent: a reload. The teacher workspace holds half-filled wizard
 * steps in the current form, and a page reload is how that work gets thrown away,
 * so the switch never triggers one.
 *
 * The stored value is validated against the same two-word allowlist the server
 * uses, on the way in and on the way out. Anything else falls back to the
 * default, so a tampered or truncated cookie cannot put an unknown theme on the
 * page.
 */
(function () {
  "use strict";

  var THEMES = { light: true, dark: true };
  var DEFAULT_THEME = "light";
  var ATTRIBUTE = "data-theme";

  function isTheme(value) {
    return typeof value === "string" && THEMES[value] === true;
  }

  function root() {
    return document.documentElement;
  }

  function currentTheme() {
    var value = root().getAttribute(ATTRIBUTE);
    return isTheme(value) ? value : DEFAULT_THEME;
  }

  /**
   * Paint the theme without persisting it.
   *
   * Only the attribute changes: every colour in the site, the teacher workspace
   * and the live dashboard is a `var(--colour-*)` token that flips with it, so
   * there is no stylesheet to swap and nothing to wait for.
   */
  function apply(theme) {
    if (!isTheme(theme)) {
      theme = DEFAULT_THEME;
    }
    root().setAttribute(ATTRIBUTE, theme);
    syncToggles(theme);
    return theme;
  }

  /** Read the theme out of the cookie, or null when nothing valid is stored. */
  function readStored(key) {
    var match = document.cookie.match(new RegExp("(?:^|; )" + key + "=([^;]*)"));
    if (!match) {
      return null;
    }
    var value = decodeURIComponent(match[1]);
    return THEMES[value] === true ? value : null;
  }

  /**
   * Write the cookie with the deployment's own attributes.
   *
   * The name and the lifetime are read off the form rather than repeated here:
   * a second copy in JavaScript would drift from `settings.THEME_COOKIE_*` the
   * first time either was changed, and the symptom - a preference that expires
   * early or is never found - would be invisible until a teacher reported it.
   */
  function writeStored(key, theme, maxAge, secure) {
    var parts = [
      key + "=" + encodeURIComponent(theme),
      "path=/",
      "max-age=" + maxAge,
      "SameSite=Lax"
    ];
    if (secure) {
      parts.push("Secure");
    }
    document.cookie = parts.join("; ");
  }

  /** Mark which of the switcher's buttons reflects the theme now on screen. */
  function syncToggles(theme) {
    var toggles = document.querySelectorAll("[data-theme-toggle]");
    for (var index = 0; index < toggles.length; index += 1) {
      var toggle = toggles[index];
      var isActive = toggle.getAttribute("value") === theme;
      toggle.setAttribute("aria-pressed", isActive ? "true" : "false");
    }
  }

  /**
   * Which theme the pressed control was asking for.
   *
   * `event.submitter` names the button that fired the submit in every current
   * browser; `document.activeElement` is the fallback for the ones that do not
   * have it, because without one the switch would silently do nothing there and
   * the cookie would keep whatever was already stored.
   */
  function requestedTheme(event, form) {
    var submitter = event.submitter || (form.ownerDocument.activeElement === form ? null : form.ownerDocument.activeElement);
    if (submitter && submitter.getAttribute("name") === "theme") {
      var value = submitter.getAttribute("value");
      if (isTheme(value)) {
        return value;
      }
    }
    // A submit with no recognisable control (Enter in a field, or an unknown
    // value) asks for the theme that is not current, which is the one thing a
    // theme switch is allowed to mean.
    return currentTheme() === "dark" ? "light" : "dark";
  }

  function initForm(form) {
    var key = form.getAttribute("data-theme-key") || "almanar_theme";
    var maxAge = parseInt(form.getAttribute("data-theme-max-age"), 10);
    if (!isFinite(maxAge) || maxAge <= 0) {
      maxAge = 31536000;
    }
    var secure = window.location.protocol === "https:";

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var theme = requestedTheme(event, form);
      apply(theme);
      writeStored(key, theme, maxAge, secure);
    });
  }

  function init() {
    var forms = document.querySelectorAll("[data-theme-form]");
    var key = "almanar_theme";
    if (forms.length) {
      key = forms[0].getAttribute("data-theme-key") || key;
    }

    for (var index = 0; index < forms.length; index += 1) {
      initForm(forms[index]);
    }

    // The server rendered the switcher from the cookie, but the head script may
    // have chosen the opposite theme from `prefers-color-scheme` in the meantime
    // (which is exactly the case where no cookie exists). Reconcile the buttons
    // with what is actually on screen, or the switch would claim to be in a theme
    // the page is not.
    syncToggles(currentTheme());

    // With no stored choice, keep following the operating system as it changes.
    // Once something IS stored the server renders it on every request, so this
    // listener stops having any effect - which is the point: an explicit choice
    // outranks whatever the room's lighting is doing.
    if (readStored(key)) {
      return;
    }
    var query = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    if (!query) {
      return;
    }
    var listener = function (event) {
      apply(event.matches ? "dark" : "light");
    };
    if (query.addEventListener) {
      query.addEventListener("change", listener);
    } else if (query.addListener) {
      // Safari before 14 only has the deprecated form.
      query.addListener(listener);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
