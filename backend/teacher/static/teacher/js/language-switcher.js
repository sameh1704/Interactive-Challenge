(function () {
  "use strict";

  var stateKey = "teacher-language-form-state:";
  var pageKey = window.location.pathname + window.location.search;

  function isRestorable(control) {
    return (
      control.name &&
      control.type !== "hidden" &&
      control.type !== "file" &&
      control.type !== "submit" &&
      control.type !== "button" &&
      control.type !== "reset"
    );
  }

  function restoreFormState() {
    var saved;
    try {
      saved = window.sessionStorage.getItem(stateKey + pageKey);
      if (saved) window.sessionStorage.removeItem(stateKey + pageKey);
    } catch (error) {
      console.warn("Wizard form state could not be restored after changing language.", error);
      return;
    }
    if (!saved) return;

    try {
      var forms = document.querySelectorAll("main form:not([data-language-form])");
      JSON.parse(saved).forEach(function (item) {
        var form = forms[item.formIndex];
        var control = form && form.elements[item.elementIndex];
        if (!control || !isRestorable(control) || control.name !== item.name) return;

        if (control.type === "checkbox" || control.type === "radio") {
          control.checked = item.checked;
        } else if (control.tagName === "SELECT" && control.multiple) {
          Array.prototype.forEach.call(control.options, function (option) {
            option.selected = item.values.indexOf(option.value) !== -1;
          });
        } else {
          control.value = item.value;
        }
        control.dispatchEvent(new Event("input", { bubbles: true }));
        control.dispatchEvent(new Event("change", { bubbles: true }));
      });
    } catch (error) {
      console.warn("Wizard form state could not be restored after changing language.", error);
    }
  }

  function saveFormState() {
    var state = [];
    var forms = document.querySelectorAll("main form:not([data-language-form])");
    Array.prototype.forEach.call(forms, function (form, formIndex) {
      Array.prototype.forEach.call(form.elements, function (control, elementIndex) {
        if (!isRestorable(control)) return;
        var item = {
          formIndex: formIndex,
          elementIndex: elementIndex,
          name: control.name,
        };
        if (control.type === "checkbox" || control.type === "radio") {
          item.checked = control.checked;
        } else if (control.tagName === "SELECT" && control.multiple) {
          item.values = Array.prototype.filter
            .call(control.options, function (option) { return option.selected; })
            .map(function (option) { return option.value; });
        } else {
          item.value = control.value;
        }
        state.push(item);
      });
    });

    try {
      window.sessionStorage.setItem(stateKey + pageKey, JSON.stringify(state));
    } catch (error) {
      console.warn("Wizard form state could not be saved before changing language.", error);
    }
  }

  restoreFormState();
  var switcher = document.querySelector("[data-language-form]");
  if (switcher) switcher.addEventListener("submit", saveFormState);
})();
