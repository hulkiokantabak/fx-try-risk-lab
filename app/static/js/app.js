document.addEventListener("DOMContentLoaded", () => {
  const controls = document.querySelector("[data-view-controls]");
  if (!controls) {
    return;
  }

  const storageKey = "fx-assessment-view-mode";
  const buttons = Array.from(controls.querySelectorAll("[data-view-target]"));
  const defaultMode = "quick";

  const applyMode = (mode) => {
    document.body.dataset.viewMode = mode;
    for (const button of buttons) {
      const active = button.dataset.viewTarget === mode;
      button.setAttribute("aria-pressed", active ? "true" : "false");
    }
  };

  const storedMode = window.localStorage.getItem(storageKey);
  const initialMode =
    storedMode && buttons.some((button) => button.dataset.viewTarget === storedMode)
      ? storedMode
      : defaultMode;
  applyMode(initialMode);

  for (const button of buttons) {
    button.addEventListener("click", () => {
      const nextMode = button.dataset.viewTarget || defaultMode;
      window.localStorage.setItem(storageKey, nextMode);
      applyMode(nextMode);
    });
  }
});
