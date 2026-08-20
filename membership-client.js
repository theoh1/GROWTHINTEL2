(() => {
  "use strict";

  const api = "/api/v1/membership";
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  async function call(path, options = {}) {
    const response = await fetch(api + path, {
      credentials: "include",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : body.message;
      throw new Error(detail || "Request failed");
    }
    return body;
  }

  function message(text, error = false) {
    const node = $("#message");
    if (!node) return;
    node.textContent = text || "";
    node.className = error ? "message error" : "message";
  }

  function show(selector, visible = true) {
    const node = $(selector);
    if (node) node.hidden = !visible;
  }

  function setText(selector, text) {
    const node = $(selector);
    if (node) node.textContent = text || "";
  }

  function displayDate(value) {
    if (!value) return "";
    return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  function cleanMoney(value) {
    return String(value || "").replace(/\u00c2\u00a3/g, "£");
  }

  function setBusy(form, busy) {
    form?.querySelectorAll("button, input").forEach((node) => {
      node.disabled = Boolean(busy);
    });
  }

  function showAuthMode(mode = "register") {
    show("#auth", true);
    const isRegister = mode !== "login";
    show("#register-card", isRegister);
    show("#login-card", !isRegister);
    $("#tab-register")?.classList.toggle("active", isRegister);
    $("#tab-login")?.classList.toggle("active", !isRegister);
    $("#auth")?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function updateCopyLabel(button, copied = false) {
    if (!button) return;
    button.textContent = copied ? "Copied" : "Copy";
  }

  async function loadMember() {
    try {
      const user = await call("/me");
      show("#auth", false);
      show("#guest-actions", false);
      show("#guest-reference", false);
      show("#member", true);

      const membershipLabel = String(user.membership_state || "NONE").replaceAll("_", " ").toLowerCase();
      setText("#identity", `${user.name} · ${membershipLabel}`);
      setText("#reference", user.gi_reference);

      const stateMessages = {
        NONE: "No payment is awaiting verification yet.",
        PENDING_PAYMENT: "Payment pending manual verification. Paid access is not active yet.",
        ACTIVE: `Membership active until ${displayDate(user.membership_expires_at)}.`,
        EXPIRED: `Membership expired${user.membership_expires_at ? ` on ${displayDate(user.membership_expires_at)}` : ""}. Use your same GI reference to renew.`,
      };
      setText("#membership-state", stateMessages[user.membership_state] || user.membership_state);

      const sentButton = $("#sent-payment");
      if (sentButton) {
        sentButton.disabled = user.membership_state === "PENDING_PAYMENT";
        sentButton.textContent = user.membership_state === "PENDING_PAYMENT" ? "Awaiting verification" : "I've sent the payment";
      }

      const bank = await call("/bank-transfer");
      for (const key of ["bank_name", "account_name", "sort_code", "account_number", "reference", "amount_display"]) {
        const node = document.querySelector(`[data-value="${key}"]`);
        if (node) node.textContent = key === "amount_display" ? cleanMoney(bank[key]) : bank[key];
      }
      show("#standing-order", Boolean(bank.standing_order_available) && user.membership_state === "ACTIVE");
      show("#admin-link", Boolean(user.is_admin));
      message("");
      return true;
    } catch {
      show("#member", false);
      show("#guest-actions", true);
      show("#guest-reference", true);
      return false;
    }
  }

  document.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy]");
    if (!copy) return;
    const value = document.querySelector(`[data-value="${copy.dataset.copy}"]`)?.textContent?.trim() || "";
    if (!value || value.includes("Loading")) return;

    try {
      await navigator.clipboard.writeText(value);
      updateCopyLabel(copy, true);
      setTimeout(() => updateCopyLabel(copy, false), 1200);
    } catch {
      message("Could not copy that value. Please copy it manually.", true);
    }
  });

  $("#show-register")?.addEventListener("click", () => showAuthMode("register"));
  $("#show-login")?.addEventListener("click", () => showAuthMode("login"));
  $("#tab-register")?.addEventListener("click", () => showAuthMode("register"));
  $("#tab-login")?.addEventListener("click", () => showAuthMode("login"));

  $("#register")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    setBusy(form, true);
    try {
      await call("/register", { method: "POST", body: JSON.stringify(values) });
      await call("/login", {
        method: "POST",
        body: JSON.stringify({ email: values.email, password: values.password }),
      });
      await loadMember();
      message("Account created. Your real GI reference and payment details are ready.");
      form.reset();
    } catch (error) {
      message(error.message, true);
    } finally {
      setBusy(form, false);
    }
  });

  $("#login")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    setBusy(form, true);
    try {
      await call("/login", { method: "POST", body: JSON.stringify(values) });
      await loadMember();
      message("Signed in. Your real GI reference and payment details are ready.");
    } catch (error) {
      message(error.message, true);
    } finally {
      setBusy(form, false);
    }
  });

  $("#sent-payment")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    const previousText = button.textContent;
    button.textContent = "Submitting...";
    try {
      const result = await call("/payment-requests", { method: "POST", body: "{}" });
      message(result.message || "Payment request created. Growth Intel will review and match your transfer.");
      button.textContent = "Awaiting verification";
    } catch (error) {
      button.disabled = false;
      button.textContent = previousText;
      message(error.message, true);
    }
  });

  $("#logout")?.addEventListener("click", async () => {
    try {
      await call("/logout", { method: "POST", body: "{}" });
    } finally {
      location.reload();
    }
  });

  $$(".copy-btn").forEach((button) => updateCopyLabel(button, false));

  loadMember().then((loaded) => {
    if (!loaded) {
      const mode = new URLSearchParams(location.search).get("mode");
      if (mode === "login" || mode === "register") showAuthMode(mode);
    }
  });
})();
