(() => {
  const copy = async (value, button) => {
    const original = button ? button.innerHTML : "";
    if (button) {
      button.setAttribute("aria-live", "polite");
      button.textContent = "Copied";
    }
    try {
      await navigator.clipboard.writeText(value);
    } catch (_error) {
      const fallback = document.createElement("textarea");
      fallback.value = value;
      fallback.style.position = "fixed";
      fallback.style.opacity = "0";
      document.body.appendChild(fallback);
      fallback.select();
      document.execCommand("copy");
      fallback.remove();
    }
    if (button) window.setTimeout(() => { button.innerHTML = original; }, 1400);
  };

  document.querySelectorAll("[data-copy]").forEach((element) => {
    element.addEventListener("click", (event) => {
      const button = event.target.closest("button") || (element.tagName === "BUTTON" ? element : null);
      copy(element.dataset.copy, button);
    });
  });

  document.querySelectorAll("form[data-loading-text]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit']");
      if (!button || button.disabled) return;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = form.dataset.loadingText;
    });
  });
})();
