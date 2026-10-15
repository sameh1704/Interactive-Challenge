(function () {
  "use strict";

  var notices = document.querySelectorAll("[data-first-use]");
  Array.prototype.forEach.call(notices, function (notice) {
    var key = "teacher-help-dismissed:" + notice.dataset.firstUse;
    var dismiss = notice.querySelector("[data-dismiss-first-use]");

    try {
      if (window.localStorage.getItem(key) === "true") {
        notice.hidden = true;
      }
    } catch (error) {
      console.warn("First-use help preference could not be read.", error);
    }

    if (!dismiss) return;
    dismiss.addEventListener("click", function () {
      notice.hidden = true;
      try {
        window.localStorage.setItem(key, "true");
      } catch (error) {
        console.warn("First-use help preference could not be saved.", error);
      }
    });
  });
})();
