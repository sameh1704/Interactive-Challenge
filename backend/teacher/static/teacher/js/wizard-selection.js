"use strict";
document.querySelectorAll("[data-selection-group]").forEach(group => {
  const boxes = () => [...group.querySelectorAll('input.selection-checkbox')];
  const update = () => {
    const checked = boxes().filter(b => b.checked);
    group.querySelector("[data-selection-count]").textContent = checked.length;
    boxes().forEach(b => b.closest("label").classList.toggle("is-selected", b.checked));
  };
  group.addEventListener("change", update);
  group.querySelector("[data-select-all]").addEventListener("click", () => {boxes().forEach(b => b.checked = true); update();});
  group.querySelector("[data-select-none]").addEventListener("click", () => {boxes().forEach(b => b.checked = false); update();});
  update();
});
