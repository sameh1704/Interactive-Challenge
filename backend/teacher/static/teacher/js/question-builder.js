(function () {
  "use strict";

  var form = document.getElementById("question-form");
  var stateElement = document.getElementById("question-builder-data");
  if (!form || !stateElement) return;

  var state;
  try {
    state = JSON.parse(stateElement.textContent);
  } catch (error) {
    console.error("Could not load Question Builder data.", error);
    return;
  }

  var optionsField = form.querySelector('input[name="options"]');
  var correctOptionField = form.querySelector('input[name="correct_option"]');
  var correctAnswerField = form.querySelector('input[name="correct_answer"]');
  var typeConfigField = form.querySelector('input[name="type_config"]');
  var preview = document.getElementById("question-preview");

  function label(name) {
    return form.dataset["label" + name.charAt(0).toUpperCase() + name.slice(1)];
  }

  function selectedType() {
    var selected = form.querySelector('input[name="question_type"]:checked');
    return selected ? selected.value : "multiple_choice";
  }

  function setInitialType() {
    var wanted = state.question_type || "multiple_choice";
    var radio = form.querySelector(
      'input[name="question_type"][value="' + wanted + '"]'
    );
    if (radio) radio.checked = true;
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, action, className) {
    var node = element("button", className || "", label);
    node.type = "button";
    node.dataset.action = action;
    return node;
  }

  function textInput(value, label) {
    var input = element("input");
    input.type = "text";
    input.value = value || "";
    input.setAttribute("aria-label", label);
    return input;
  }

  function currentConfig() {
    return state.type_config && typeof state.type_config === "object"
      ? state.type_config
      : {};
  }

  function readOptions() {
    return Array.prototype.map.call(
      document.querySelectorAll("#multiple-choice-options input[type=text]"),
      function (input) { return input.value.trim(); }
    );
  }

  function addOption(value, checked) {
    var container = document.getElementById("multiple-choice-options");
    var row = element("div", "question-builder__row");
    var radio = element("input");
    radio.type = "radio";
    radio.name = "builder-correct-option";
    radio.value = "correct";
    radio.checked = Boolean(checked);
    radio.setAttribute("aria-label", label("markCorrect"));
    var input = textInput(value, label("answerOption"));
    input.addEventListener("input", sync);
    radio.addEventListener("change", sync);
    row.appendChild(radio);
    row.appendChild(input);
    row.appendChild(button(label("remove"), "remove-option"));
    container.appendChild(row);
  }

  function renderOptions() {
    var container = document.getElementById("multiple-choice-options");
    container.textContent = "";
    var values = Array.isArray(state.options) ? state.options : [];
    if (!values.length) values = [label("optionA"), label("optionB")];
    values.forEach(function (value) {
      addOption(value, value === state.correct_option);
    });
    if (!container.querySelector('input[type=radio]:checked')) {
      var first = container.querySelector('input[type=radio]');
      if (first) first.checked = true;
    }
  }

  /**
   * Ordering: the row order *is* the correct order.
   *
   * A position dropdown per item would be a way of expressing the same thing,
   * but it asks a teacher to think in numbers while they are thinking about
   * content. "Move this item up one" is the instruction they already have in mind,
   * and a list they can read top to bottom is the preview they want - so the
   * rows are the answer, and moving is the only interaction.
   */
  function renderOrdering() {
    var container = document.getElementById("ordering-items");
    container.textContent = "";
    var config = currentConfig();
    var items = Array.isArray(config.items) ? config.items.slice() : [];
    if (!items.length) items = [label("itemOne"), label("itemTwo")];
    var correctOrder = Array.isArray(config.correct_order)
      ? config.correct_order
      : items;
    // The correct order is the order of the rows, so render in that order and a
    // re-opened question looks exactly as it was last left.
    var ordered = correctOrder
      .filter(function (value) { return items.indexOf(value) !== -1; })
      .concat(items.filter(function (value) { return correctOrder.indexOf(value) === -1; }));

    ordered.forEach(function (value) {
      var row = element("li", "question-builder__row");
      row.appendChild(dragHandle());
      var input = textInput(value, label("itemOrder"));
      input.addEventListener("input", sync);
      row.appendChild(input);
      row.appendChild(button(label("moveUp"), "move-ordering-item-up"));
      row.appendChild(button(label("moveDown"), "move-ordering-item-down"));
      row.appendChild(button(label("remove"), "remove-ordering-item"));
      container.appendChild(row);
    });
    renumberOrdering();
  }

  function dragHandle() {
    var handle = element("span", "question-builder__handle", "⋮⋮");
    handle.setAttribute("aria-hidden", "true");
    return handle;
  }

  function renumberOrdering() {
    var rows = document.querySelectorAll("#ordering-items li");
    Array.prototype.forEach.call(rows, function (row, index) {
      var marker = row.querySelector(".question-builder__position");
      if (!marker) {
        marker = element("span", "question-builder__position");
        row.insertBefore(marker, row.firstChild);
      }
      marker.textContent = String(index + 1);
    });
  }

  function addOrderingItem(value) {
    var container = document.getElementById("ordering-items");
    var row = element("li", "question-builder__row");
    row.appendChild(dragHandle());
    var input = textInput(value || "", label("itemOrder"));
    input.addEventListener("input", sync);
    row.appendChild(input);
    row.appendChild(button(label("moveUp"), "move-ordering-item-up"));
    row.appendChild(button(label("moveDown"), "move-ordering-item-down"));
    row.appendChild(button(label("remove"), "remove-ordering-item"));
    container.appendChild(row);
    renumberOrdering();
  }

  /**
   * Swap a row with the one above or below it.
   *
   * The buttons stay in place afterwards - they are attached to the rows, and a
   * row keeps its own controls when it moves - so the list can be walked up and
   * down repeatedly without the controls shifting under the teacher's hand.
   */
  function moveOrderingItem(buttonNode, direction) {
    var row = buttonNode.closest("li");
    var sibling = direction === "up" ? row.previousElementSibling : row.nextElementSibling;
    if (!sibling || sibling.nodeName !== "LI") {
      return;
    }
    if (direction === "up") {
      row.parentNode.insertBefore(row, sibling);
    } else {
      row.parentNode.insertBefore(sibling, row);
    }
    renumberOrdering();
    sync();
  }

  function readOrdering() {
    // Row order is the correct order, so reading it is reading the list.
    var items = Array.prototype.map.call(
      document.querySelectorAll("#ordering-items li input[type=text]"),
      function (input) { return input.value.trim(); }
    );
    return {
      items: items,
      correct_order: items.slice()
    };
  }

  function readCategories() {
    return Array.prototype.map.call(
      document.querySelectorAll("#classification-categories input[type=text]"),
      function (input) { return input.value.trim(); }
    );
  }

  function readAssignments() {
    return Array.prototype.map.call(
      document.querySelectorAll("#classification-assignments .question-builder__row"),
      function (row) {
        return {
          label: row.querySelector('input[type=text]').value.trim(),
          category: row.querySelector("select").value
        };
      }
    );
  }

  function refreshAssignmentCategories() {
    var categories = readCategories();
    Array.prototype.forEach.call(
      document.querySelectorAll("#classification-assignments select"),
      function (select) {
        var previous = select.value;
        select.textContent = "";
        categories.forEach(function (category) {
          var option = element("option", "", category);
          option.value = category;
          select.appendChild(option);
        });
        if (categories.indexOf(previous) !== -1) select.value = previous;
      }
    );
  }

  function addCategory(value) {
    var row = element("div", "question-builder__row");
    var input = textInput(value, label("categoryName"));
    input.addEventListener("input", function () {
      refreshAssignmentCategories();
      sync();
    });
    row.appendChild(input);
    row.appendChild(button(label("remove"), "remove-category"));
    document.getElementById("classification-categories").appendChild(row);
  }

  function addAssignment(assignment) {
    var row = element("div", "question-builder__row question-builder__row--assignment");
    var input = textInput(assignment.label, label("itemClassify"));
    input.addEventListener("input", sync);
    var select = element("select");
    select.setAttribute("aria-label", label("correctCategory"));
    readCategories().forEach(function (category) {
      var option = element("option", "", category);
      option.value = category;
      select.appendChild(option);
    });
    select.value = assignment.category;
    select.addEventListener("change", sync);
    row.appendChild(input);
    row.appendChild(select);
    row.appendChild(button(label("remove"), "remove-assignment"));
    document.getElementById("classification-assignments").appendChild(row);
  }

  function renderClassification() {
    var config = currentConfig();
    var categories = Array.isArray(config.categories) ? config.categories : [];
    var assignments = Array.isArray(config.assignments) ? config.assignments : [];
    if (!categories.length) categories = [label("categoryOne"), label("categoryTwo")];
    if (!assignments.length) {
      assignments = [
        { label: label("itemOne"), category: categories[0] },
        { label: label("itemTwo"), category: categories[1] }
      ];
    }
    document.getElementById("classification-categories").textContent = "";
    document.getElementById("classification-assignments").textContent = "";
    categories.forEach(addCategory);
    assignments.forEach(function (assignment) {
      addAssignment({
        label: assignment && typeof assignment.label === "string" ? assignment.label : "",
        category: assignment && typeof assignment.category === "string"
          ? assignment.category
          : categories[0]
      });
    });
  }

  function renderShortAnswers() {
    var container = document.getElementById("short-answers");
    container.textContent = "";
    var config = currentConfig();
    var answers = Array.isArray(config.accepted_answers)
      ? config.accepted_answers
      : [];
    if (!answers.length) answers = [""];
    answers.forEach(function (answer) {
      var row = element("div", "question-builder__row");
      var input = textInput(answer, label("acceptedAnswer"));
      input.addEventListener("input", sync);
      row.appendChild(input);
      row.appendChild(button(label("remove"), "remove-short-answer"));
      container.appendChild(row);
    });
    document.getElementById("short-answer-case-sensitive").checked =
      Boolean(config.case_sensitive);
  }

  function activeSection() {
    var type = selectedType();
    Array.prototype.forEach.call(
      document.querySelectorAll(".question-type-fields"),
      function (section) {
        section.hidden = section.dataset.fieldsFor !== type;
      }
    );
    Array.prototype.forEach.call(
      document.querySelectorAll(".question-type"),
      function (label) {
        label.classList.toggle(
          "is-selected",
          label.dataset.questionType === type
        );
      }
    );
  }

  function sync() {
    var type = selectedType();
    var text = form.querySelector('[name="text"]').value;
    var duration = form.querySelector('[name="duration_seconds"]').value;
    var active = form.querySelector('[name="active"]').checked;
    var hint = form.querySelector('[name="hint"]').value;
    var explanation = form.querySelector('[name="explanation"]').value;
    var options = [];
    var correctOption = "";
    var correctAnswer = "";
    var config = {};

    if (type === "multiple_choice") {
      var optionRows = document.querySelectorAll("#multiple-choice-options .question-builder__row");
      Array.prototype.forEach.call(optionRows, function (row) {
        var value = row.querySelector('input[type=text]').value.trim();
        options.push(value);
        if (row.querySelector('input[type=radio]').checked) correctOption = value;
      });
    } else if (type === "true_false") {
      var answer = form.querySelector('input[name="builder-correct-answer"]:checked');
      correctAnswer = answer ? answer.value : "";
    } else if (type === "ordering") {
      config = readOrdering();
    } else if (type === "classification") {
      config = {
        categories: readCategories(),
        assignments: readAssignments()
      };
    } else if (type === "short_answer") {
      config = {
        accepted_answers: Array.prototype.map.call(
          document.querySelectorAll("#short-answers input[type=text]"),
          function (input) { return input.value.trim(); }
        ),
        case_sensitive: document.getElementById("short-answer-case-sensitive").checked
      };
    }

    optionsField.value = JSON.stringify(options);
    correctOptionField.value = correctOption;
    correctAnswerField.value = correctAnswer;
    typeConfigField.value = JSON.stringify(config);

    state = {
      question_type: type,
      text: text,
      options: options,
      correct_option: correctOption,
      correct_answer: correctAnswer,
      type_config: config,
      duration_seconds: duration,
      active: active,
      hint: hint,
      explanation: explanation,
      image_url: state.image_url || ""
    };
    activeSection();
    renderPreview();
  }

  function previewNode(tag, className, text) {
    return element(tag, className, text);
  }

  /**
   * Render the preview as the *student* screen will show it.
   *
   * Not as a teacher's debugging view. The correct answer is deliberately
   * dropped here even though the builder knows it: a preview that prints it
   * beside the question is one that can leak it onto a shared display. The same
   * applies to the explanation, which is only revealed once answering has
   * closed. The hint stays, because a hint is shown to the room.
   */
  function renderPreview() {
    if (!preview) return;
    var wrapper = element("div", "question-preview");
    var image = document.getElementById("question-image-preview");
    var selectedImage = image && !image.hidden ? image.src : state.image_url;
    if (selectedImage) {
      var img = previewNode("img", "question-preview__image");
      img.src = selectedImage;
      img.alt = "";
      wrapper.appendChild(img);
    }

    wrapper.appendChild(
      previewNode(
        "p",
        "question-preview__text",
        state.text || label("emptyQuestion")
      )
    );

    if (state.question_type === "multiple_choice") {
      // The options, with no marking of which one is right.
      var optionList = previewNode("ul", "question-preview__options");
      state.options.forEach(function (value) {
        optionList.appendChild(previewNode("li", "", value || label("emptyOption")));
      });
      wrapper.appendChild(optionList);
    } else if (state.question_type === "true_false") {
      // Neither choice is marked, and neither says which is right.
      var choices = previewNode("div", "question-preview__options");
      ["true", "false"].forEach(function (value) {
        var item = previewNode("p", "", value === "true" ? label("true") : label("false"));
        choices.appendChild(item);
      });
      wrapper.appendChild(choices);
    } else if (state.question_type === "ordering") {
      wrapper.appendChild(
        previewNode("p", "question-preview__meta", label("itemsShown"))
      );
      var itemList = previewNode("ul", "question-preview__options");
      (state.type_config.items || []).forEach(function (value) {
        itemList.appendChild(previewNode("li", "", value || label("emptyItem")));
      });
      wrapper.appendChild(itemList);
    } else if (state.question_type === "classification") {
      wrapper.appendChild(
        previewNode("p", "question-preview__meta", label("itemsToSort"))
      );
      var itemsToSort = previewNode("ul", "question-preview__options");
      (state.type_config.assignments || []).forEach(function (assignment) {
        itemsToSort.appendChild(previewNode("li", "", assignment.label));
      });
      wrapper.appendChild(itemsToSort);
      wrapper.appendChild(
        previewNode("p", "question-preview__meta", label("categories"))
      );
      var categoryList = previewNode("ul", "question-preview__options");
      (state.type_config.categories || []).forEach(function (category) {
        categoryList.appendChild(previewNode("li", "", category));
      });
      wrapper.appendChild(categoryList);
    } else if (state.question_type === "short_answer") {
      wrapper.appendChild(
        previewNode("p", "question-preview__answer-input", label("studentAnswer"))
      );
    }

    if (state.hint) {
      var hint = previewNode("p", "question-preview__note");
      hint.appendChild(previewNode("strong", "", label("hint") + " "));
      hint.appendChild(document.createTextNode(state.hint));
      wrapper.appendChild(hint);
    }
    wrapper.appendChild(
      previewNode(
        "p",
        "question-preview__meta",
        (state.duration_seconds || 0) + " " + label("seconds")
      )
    );
    preview.replaceChildren(wrapper);
  }

  function removeRow(buttonNode, selector, minimum) {
    var rows = document.querySelectorAll(selector);
    if (rows.length <= minimum) return;
    buttonNode.closest(".question-builder__row").remove();
    if (selector === "#ordering-items li") renumberOrdering();
    if (selector === "#classification-categories .question-builder__row") {
      refreshAssignmentCategories();
    }
    sync();
  }

  form.addEventListener("click", function (event) {
    var action = event.target.dataset.action;
    if (!action) return;

    if (action === "add-option") {
      if (document.querySelectorAll("#multiple-choice-options .question-builder__row").length >= 6) return;
      addOption("", false);
    } else if (action === "remove-option") {
      removeRow(event.target, "#multiple-choice-options .question-builder__row", 2);
    } else if (action === "add-ordering-item") {
      if (document.querySelectorAll("#ordering-items li").length >= 10) return;
      addOrderingItem("");
    } else if (action === "remove-ordering-item") {
      removeRow(event.target, "#ordering-items li", 2);
    } else if (action === "move-ordering-item-up") {
      moveOrderingItem(event.target, "up");
      return;
    } else if (action === "move-ordering-item-down") {
      moveOrderingItem(event.target, "down");
      return;
    } else if (action === "add-category") {
      if (document.querySelectorAll("#classification-categories .question-builder__row").length >= 6) return;
      addCategory("");
    } else if (action === "remove-category") {
      removeRow(event.target, "#classification-categories .question-builder__row", 2);
    } else if (action === "add-assignment") {
      if (document.querySelectorAll("#classification-assignments .question-builder__row").length >= 12) return;
      addAssignment({ label: "", category: readCategories()[0] || "" });
    } else if (action === "remove-assignment") {
      removeRow(event.target, "#classification-assignments .question-builder__row", 2);
    } else if (action === "add-short-answer") {
      if (document.querySelectorAll("#short-answers .question-builder__row").length >= 10) return;
      var answerRow = element("div", "question-builder__row");
      var answerInput = textInput("", label("acceptedAnswer"));
      answerInput.addEventListener("input", sync);
      answerRow.appendChild(answerInput);
      answerRow.appendChild(button(label("remove"), "remove-short-answer"));
      document.getElementById("short-answers").appendChild(answerRow);
    } else if (action === "remove-short-answer") {
      removeRow(event.target, "#short-answers .question-builder__row", 1);
    } else {
      return;
    }
    sync();
  });

  form.addEventListener("change", function (event) {
    if (event.target.name === "question_type") activeSection();
    if (event.target.name === "builder-correct-answer") {
      correctAnswerField.value = event.target.value;
    }
    if (event.target.id === "short-answer-case-sensitive") sync();
    else sync();
  });

  form.addEventListener("input", function (event) {
    if (!event.target.matches("input[type=radio]")) sync();
  });

  var imageInput = form.querySelector('input[name="image"]');
  var imagePreview = document.getElementById("question-image-preview");
  if (imageInput && imagePreview) {
    imageInput.addEventListener("change", function () {
      var file = imageInput.files && imageInput.files[0];
      if (!file) {
        imagePreview.hidden = true;
        imagePreview.removeAttribute("src");
        sync();
        return;
      }
      var reader = new FileReader();
      reader.addEventListener("load", function () {
        imagePreview.src = reader.result;
        imagePreview.hidden = false;
        sync();
      });
      reader.readAsDataURL(file);
    });
  }

  form.addEventListener("submit", sync);

  setInitialType();
  renderOptions();
  renderOrdering();
  renderClassification();
  renderShortAnswers();
  activeSection();
  sync();
})();
