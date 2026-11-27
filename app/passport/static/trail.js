// Decision trail: one button per step list opens or closes every step; printing shows every step's details.
(() => {
  "use strict";
  document.querySelectorAll("[data-expand-all]").forEach((button) => {
    const list = document.getElementById(button.getAttribute("aria-controls"));
    const steps = list ? Array.from(list.querySelectorAll("details.step-card")) : [];
    if (!steps.length) return;
    const sync = () => {
      button.textContent = steps.every((step) => step.open) ? "Collapse all steps" : "Expand all steps";
    };
    button.addEventListener("click", () => {
      const open = !steps.every((step) => step.open);
      steps.forEach((step) => { step.open = open; });
      sync();
    });
    steps.forEach((step) => step.addEventListener("toggle", sync));
    button.hidden = false;
    sync();
  });

  let openedForPrint = [];
  window.addEventListener("beforeprint", () => {
    openedForPrint = Array.from(document.querySelectorAll("details.step-card:not([open])"));
    openedForPrint.forEach((step) => { step.open = true; });
  });
  window.addEventListener("afterprint", () => {
    openedForPrint.forEach((step) => { step.open = false; });
    openedForPrint = [];
  });
})();
