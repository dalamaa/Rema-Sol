(() => {
  const PAYMENT_TIMEOUT_MS = 120000;
  const PROCESSING_STATUSES = new Set(["funding", "funding_pending", "funded", "payment_pending"]);
  const TERMINAL_STATUSES = new Set(["complete", "failed", "needs_review"]);

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

  const make = (tag, className, content) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = content;
    return element;
  };

  const walletForm = document.querySelector(".wallet-send-form[data-wallet-payment]");
  const resultPanel = document.querySelector("[data-wallet-result]");
  const errorRegion = document.querySelector("[data-wallet-error]");

  const setWalletError = (message) => {
    if (!errorRegion) return;
    const normalized = typeof message === "string" ? message.trim() : "";
    const messageElement = errorRegion.querySelector("span");
    if (messageElement) messageElement.textContent = normalized;
    errorRegion.hidden = !normalized;
  };

  const focusWalletResult = () => {
    if (!resultPanel) return;
    window.requestAnimationFrame(() => {
      const bounds = resultPanel.getBoundingClientRect();
      const visible = bounds.top >= 0 && bounds.bottom <= window.innerHeight;
      resultPanel.focus({ preventScroll: true });
      if (!visible) {
        const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
        resultPanel.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "nearest" });
      }
    });
  };

  const focusWalletError = () => {
    if (!errorRegion) return;
    window.requestAnimationFrame(() => errorRegion.focus({ preventScroll: false }));
  };

  const setFormBusy = (form, busy, label = "Payment in progress") => {
    const walletOperation = form?.matches?.("[data-wallet-payment], [data-wallet-recheck]");
    const button = form?.querySelector("button[type='submit']");
    if (walletOperation && !busy) resultPanel?.removeAttribute("aria-busy");
    if (!button) return;
    if (!button.dataset.idleHtml) button.dataset.idleHtml = button.innerHTML;
    button.disabled = busy;
    if (walletOperation) {
      // The result card owns the single wallet progress indicator. Keeping a
      // second button spinner here made terminal responses look stuck and left
      // a stale busy state behind on dynamically-created recheck forms.
      if (resultPanel) {
        if (busy) resultPanel.setAttribute("aria-busy", "true");
        else resultPanel.removeAttribute("aria-busy");
      }
      button.removeAttribute("aria-busy");
    } else if (busy) {
      button.setAttribute("aria-busy", "true");
    } else {
      button.removeAttribute("aria-busy");
    }
    if (busy) button.textContent = label;
    else button.innerHTML = button.dataset.idleHtml;
    form.querySelectorAll("input, select, textarea").forEach((control) => {
      if (control.type !== "hidden") control.disabled = busy;
    });
  };

  const setPaymentFormState = (wallet, localNeedsReview = false) => {
    if (!walletForm) return;
    const button = walletForm.querySelector("button[type='submit']");
    const status = wallet?.status || "created";
    const stopSending = localNeedsReview || TERMINAL_STATUSES.has(status);
    walletForm.querySelectorAll("input, select, textarea").forEach((control) => {
      if (control.type !== "hidden") control.disabled = stopSending;
    });
    if (!button) return;
    button.hidden = stopSending;
    button.disabled = stopSending;
    button.removeAttribute("aria-busy");
    if (!stopSending) {
      button.innerHTML = button.dataset.idleHtml || button.innerHTML;
      button.textContent = wallet?.payment_signature ? "Check payment status" : "Send Test SOL";
    }
  };

  const addCopyButton = (parent, value, label) => {
    if (!value) return;
    const button = make("button", "copy-button", "Copy");
    button.type = "button";
    button.dataset.copy = value;
    button.setAttribute("aria-label", `Copy ${label}`);
    parent.appendChild(button);
  };

  const addDetail = (details, label, value, copyLabel, strong = false) => {
    if (!value) return;
    const row = make("div");
    row.appendChild(make("span", "", label));
    row.appendChild(make(strong ? "strong" : "code", "", String(value)));
    if (!strong) addCopyButton(row, String(value), copyLabel);
    details.appendChild(row);
  };

  const actionForm = (action, csrfToken, sessionId, label, isRecheck = false) => {
    const form = make("form");
    form.method = "post";
    form.action = action;
    if (isRecheck) form.dataset.walletRecheck = "";
    else form.dataset.loadingText = "Creating New Test…";
    const csrf = make("input");
    csrf.type = "hidden";
    csrf.name = "csrf_token";
    csrf.value = csrfToken || "";
    form.appendChild(csrf);
    if (sessionId) {
      const session = make("input");
      session.type = "hidden";
      session.name = "session_id";
      session.value = sessionId;
      form.appendChild(session);
    }
    const button = make("button", "button button-primary button-full", label);
    button.type = "submit";
    form.appendChild(button);
    return form;
  };

  const renderWalletResult = (payload, { localNeedsReview = false, focus = false } = {}) => {
    if (!resultPanel) return;
    const wallet = payload?.wallet || null;
    const rawStatus = localNeedsReview ? "needs_review" : (wallet?.status || payload?.status || "created");
    const knownStatus = new Set(["created", ...PROCESSING_STATUSES, ...TERMINAL_STATUSES]);
    let status = knownStatus.has(rawStatus) ? rawStatus : "needs_review";
    if (status === "complete" && !wallet?.payment_signature) status = "needs_review";
    const processing = !localNeedsReview && PROCESSING_STATUSES.has(status);
    const confirmed = status === "complete";
    const failed = status === "failed";
    const needsReview = status === "needs_review";
    const sessionId = wallet?.id || walletForm?.elements.session_id?.value || "";
    const csrfToken = resultPanel.dataset.csrfToken || walletForm?.elements.csrf_token?.value || "";

    resultPanel.dataset.walletStatus = status;
    if (TERMINAL_STATUSES.has(status)) resultPanel.removeAttribute("aria-busy");
    resultPanel.replaceChildren(make("div", "panel-kicker", "Payment result"));

    if (status === "created" && !wallet?.payment_signature) {
      const placeholder = make("div", "demo-result-placeholder");
      placeholder.appendChild(make("div", "placeholder-orbit", "◎"));
      placeholder.appendChild(make("h2", "", "Your transaction will appear here."));
      placeholder.appendChild(make("p", "", "Enter an active checkout address and choose an amount to send."));
      resultPanel.appendChild(placeholder);
      if (focus) focusWalletResult();
      return;
    }

    const iconClass = confirmed ? "success" : failed ? "failed" : needsReview ? "review" : "pending processing";
    const icon = make("div", `demo-result-icon ${iconClass}`);
    icon.setAttribute("aria-hidden", "true");
    if (processing) icon.appendChild(make("span", "wallet-processing-spinner"));
    else icon.textContent = confirmed ? "✓" : "!";
    resultPanel.appendChild(icon);

    const heading = confirmed ? "Payment confirmed" : failed ? "Payment failed" : needsReview ? "Payment needs review" : "Payment in progress";
    resultPanel.appendChild(make("h2", "", heading));
    const description = confirmed
      ? "The verified Devnet transfer is recorded below."
      : failed
        ? "The payment did not reach the checkout. No second transfer was created."
        : needsReview
          ? "The saved payment is uncertain. Recheck its status without sending again."
          : "The transfer is being verified before it is confirmed.";
    resultPanel.appendChild(make("p", "", description));

    if (wallet?.payment_signature) {
      const details = make("div", "wallet-result-details");
      addDetail(details, "Actual sender", wallet.sender_address, "actual sender address");
      addDetail(details, "Recipient", wallet.recipient_address, "recipient address");
      addDetail(details, "Amount", wallet.selected_sol ? `${wallet.selected_sol} SOL` : "", "", true);
      addDetail(details, "Transaction ID", wallet.payment_signature, "transaction ID");
      resultPanel.appendChild(details);

      if (payload?.explorer_url) {
        const explorer = make("a", "button button-quiet button-full", "Open on Solana Explorer");
        explorer.href = payload.explorer_url;
        explorer.target = "_blank";
        explorer.rel = "noreferrer";
        resultPanel.appendChild(explorer);
      }
    } else if (wallet?.last_error || payload?.error) {
      resultPanel.appendChild(make("div", "wallet-result-message", wallet?.last_error || payload.error));
    }

    if (confirmed) {
      const recovery = make("a", "button button-primary button-full", "Open Recovery");
      recovery.href = resultPanel.dataset.recoveryAction;
      resultPanel.appendChild(recovery);
    } else if (failed) {
      resultPanel.appendChild(actionForm(resultPanel.dataset.newTestAction, csrfToken, "", "New Test"));
    }
    if (needsReview && (wallet?.payment_signature || localNeedsReview)) {
      resultPanel.appendChild(actionForm(resultPanel.dataset.recheckAction, csrfToken, sessionId, "Recheck Payment Status", true));
    }
    if (focus) focusWalletResult();
  };

  const requestWalletState = async (form, { isRecheck = false } = {}) => {
    if (form.dataset.walletRequestBusy === "true") return;
    form.dataset.walletRequestBusy = "true";
    const requestBody = new FormData(form);
    const controller = new AbortController();
    let timedOut = false;
    let finalWallet = null;
    let localNeedsReview = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, PAYMENT_TIMEOUT_MS);
    setFormBusy(form, true, isRecheck ? "Checking payment status" : "Payment in progress");
    setWalletError("");
    renderWalletResult(
      { wallet: { id: form.elements.session_id?.value || "", status: "payment_pending" } },
      { focus: true },
    );

    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: requestBody,
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "X-Requested-With": "fetch",
        },
        signal: controller.signal,
      });
      let payload;
      try {
        payload = await response.json();
      } catch (_error) {
        throw new Error("The payment result was not returned in a readable format.");
      }
      finalWallet = payload.wallet || null;
      if (!response.ok) {
        if (finalWallet) renderWalletResult(payload, { focus: true });
        setWalletError(payload.error || "The payment request could not be processed.");
        if (!finalWallet) focusWalletError();
        return;
      }
      if (!finalWallet) throw new Error("The payment result did not include a saved wallet state.");
      renderWalletResult(payload, { focus: true });
      setWalletError("");
    } catch (error) {
      const message = timedOut
        ? "The request timed out before the result could be confirmed. Recheck the saved payment status; no new transfer will be created."
        : "The result could not be confirmed. Recheck the saved payment status; no new transfer will be created.";
      localNeedsReview = true;
      finalWallet = null;
      renderWalletResult({ status: "needs_review", error: message }, { localNeedsReview: true });
      focusWalletResult();
      setWalletError(message);
    } finally {
      window.clearTimeout(timeoutId);
      form.dataset.walletRequestBusy = "false";
      setFormBusy(form, false);
      setPaymentFormState(finalWallet, localNeedsReview);
    }
  };

  document.addEventListener("click", (event) => {
    const element = event.target.closest?.("[data-copy]");
    if (!element) return;
    const button = element.closest("button");
    copy(element.dataset.copy, button);
  });

  document.querySelectorAll(".preset-button[data-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.target);
      if (!input) return;
      input.value = button.dataset.amount;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
    });
  });

  if (walletForm) {
    const initialStatus = walletForm.dataset.walletStatus || "created";
    if (TERMINAL_STATUSES.has(initialStatus)) setPaymentFormState({ status: initialStatus });
    walletForm.addEventListener("submit", (event) => {
      event.preventDefault();
      requestWalletState(walletForm);
    });
  }

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.matches("[data-wallet-payment]")) return;
    if (form.matches("[data-wallet-recheck]")) {
      event.preventDefault();
      requestWalletState(form, { isRecheck: true });
      return;
    }
    const button = form.querySelector("button[type='submit']");
    if (!button || button.disabled || !form.dataset.loadingText) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = form.dataset.loadingText;
  });

  if (window.__REMA_SOL_TEST__) {
    window.__REMA_SOL_TEST__.wallet = { requestWalletState, renderWalletResult, resultPanel, walletForm };
  }
})();
