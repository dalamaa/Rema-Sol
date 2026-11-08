const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class FakeElement {
  constructor(tagName = "div", className = "") {
    this.tagName = tagName.toLowerCase();
    this.className = className;
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.attributes = {};
    this.style = {};
    this.disabled = false;
    this.hidden = false;
    this._textContent = "";
    if (this.tagName === "form") this.elements = {};
  }

  set textContent(value) {
    this._textContent = String(value ?? "");
    this.children = [];
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent).join("");
  }

  set innerHTML(value) {
    this.textContent = value;
  }

  get innerHTML() {
    return this._textContent;
  }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    if (this.tagName === "form" && child.name) this.elements[child.name] = child;
    return child;
  }

  replaceChildren(...children) {
    this.children = [];
    children.forEach((child) => this.appendChild(child));
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  matches(selector) {
    return selector.split(",").some((part) => matchesSelector(this, part.trim()));
  }

  closest(selector) {
    let current = this;
    while (current) {
      if (current.matches(selector)) return current;
      current = current.parentElement;
    }
    return null;
  }

  querySelector(selector) {
    return findDescendant(this, selector, false);
  }

  querySelectorAll(selector) {
    return findDescendant(this, selector, true);
  }

  addEventListener() {}
  focus() {}
  scrollIntoView() {}
  getBoundingClientRect() {
    return { top: 0, bottom: 300 };
  }
}

function matchesSelector(element, selector) {
  if (!selector) return false;
  const tag = selector.match(/^[a-z][a-z0-9-]*/i)?.[0];
  if (tag && element.tagName !== tag.toLowerCase()) return false;
  for (const className of selector.matchAll(/\.([a-z0-9_-]+)/gi)) {
    if (!element.className.split(/\s+/).includes(className[1])) return false;
  }
  for (const attr of selector.matchAll(/\[([^=\]]+)(?:=["']?([^\]"']+)["']?)?\]/g)) {
    const [, name, expected] = attr;
    const actual = name.startsWith("data-")
      ? element.dataset[name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase())]
      : element.getAttribute(name) ?? element[name];
    if (actual === undefined || actual === null) return false;
    if (expected !== undefined && String(actual) !== expected) return false;
  }
  return true;
}

function findDescendant(root, selector, all) {
  const matches = [];
  const visit = (element) => {
    for (const child of element.children) {
      if (child.matches(selector)) {
        matches.push(child);
        if (!all) return;
      }
      visit(child);
      if (!all && matches.length) return;
    }
  };
  visit(root);
  return all ? matches : matches[0] || null;
}

class FakeDocument {
  constructor(walletForm, resultPanel, errorRegion) {
    this.walletForm = walletForm;
    this.resultPanel = resultPanel;
    this.errorRegion = errorRegion;
    this.body = new FakeElement("body");
  }

  querySelector(selector) {
    if (selector === ".wallet-send-form[data-wallet-payment]") return this.walletForm;
    if (selector === "[data-wallet-result]") return this.resultPanel;
    if (selector === "[data-wallet-error]") return this.errorRegion;
    return null;
  }

  querySelectorAll() {
    return [];
  }

  addEventListener() {}
  createElement(tagName) {
    return new FakeElement(tagName);
  }
  getElementById() {
    return null;
  }
}

class FakeFormData {
  constructor(form) {
    this.form = form;
  }
}

function buildFixture() {
  const form = new FakeElement("form", "wallet-send-form");
  form.dataset.walletPayment = "";
  form.dataset.walletStatus = "created";
  form.action = "/wallet/send";
  const csrf = new FakeElement("input");
  csrf.type = "hidden";
  csrf.name = "csrf_token";
  csrf.value = "csrf-token";
  const session = new FakeElement("input");
  session.type = "hidden";
  session.name = "session_id";
  session.value = "wallet-1";
  const recipient = new FakeElement("input");
  recipient.type = "text";
  recipient.value = "recipient";
  const button = new FakeElement("button");
  button.type = "submit";
  button.innerHTML = "Send Test SOL";
  form.appendChild(csrf);
  form.appendChild(session);
  form.appendChild(recipient);
  form.appendChild(button);
  form.elements = { csrf_token: csrf, session_id: session };
  form.submitButton = button;

  const resultPanel = new FakeElement("div", "panel wallet-result-card");
  resultPanel.dataset.walletResult = "";
  resultPanel.dataset.csrfToken = "csrf-token";
  resultPanel.dataset.recoveryAction = "/recover";
  resultPanel.dataset.recheckAction = "/wallet/recheck";
  resultPanel.dataset.newTestAction = "/wallet/create";
  const errorRegion = new FakeElement("div");
  errorRegion.dataset.walletError = "";
  errorRegion.hidden = true;
  errorRegion.appendChild(new FakeElement("span"));
  return { form, resultPanel, errorRegion };
}

async function main() {
  const fixture = buildFixture();
  const document = new FakeDocument(fixture.form, fixture.resultPanel, fixture.errorRegion);
  const window = {
    __REMA_SOL_TEST__: {},
    innerHeight: 800,
    setTimeout,
    clearTimeout,
    requestAnimationFrame: (callback) => callback(),
    matchMedia: () => ({ matches: false }),
  };
  const context = {
    window,
    document,
    navigator: { clipboard: { writeText: async () => {} } },
    FormData: FakeFormData,
    AbortController,
    Event,
    fetch: async () => ({
      ok: true,
      json: async () => ({
        wallet: {
          id: "wallet-1",
          status: "complete",
          sender_address: "sender-wallet",
          recipient_address: "recipient-wallet",
          selected_sol: "0.002",
          payment_signature: "confirmed-transaction-id",
        },
        explorer_url: "https://explorer.solana.com/tx/confirmed-transaction-id?cluster=devnet",
      }),
    }),
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "..", "remasol", "static", "js", "app.js"), "utf8"),
    context,
    { filename: "remasol/static/js/app.js" },
  );

  await window.__REMA_SOL_TEST__.wallet.requestWalletState(fixture.form);

  assert.match(fixture.resultPanel.textContent, /Payment confirmed/);
  assert.match(fixture.resultPanel.textContent, /sender-wallet/);
  assert.match(fixture.resultPanel.textContent, /recipient-wallet/);
  assert.match(fixture.resultPanel.textContent, /0\.002 SOL/);
  assert.match(fixture.resultPanel.textContent, /confirmed-transaction-id/);
  assert.equal(
    fixture.resultPanel.querySelector("a").href,
    "https://explorer.solana.com/tx/confirmed-transaction-id?cluster=devnet",
  );
  assert.equal(fixture.resultPanel.querySelector(".wallet-processing-spinner"), null);
  assert.equal(fixture.resultPanel.querySelector('[aria-busy="true"]'), null);
  assert.equal(fixture.resultPanel.hasAttribute("aria-busy"), false);
  assert.equal(fixture.form.submitButton.hasAttribute("aria-busy"), false);
  assert.equal(fixture.form.submitButton.hidden, true);
  assert.equal(fixture.form.dataset.walletRequestBusy, "false");
  assert.doesNotMatch(fixture.resultPanel.textContent, /Payment in progress|Processing/);
  assert.doesNotMatch(fixture.resultPanel.textContent, /Check overpayment|request refund/);
  console.log("frontend wallet success regression: passed");
}

async function pollingTest() {
  const fixture = buildFixture();
  const document = new FakeDocument(fixture.form, fixture.resultPanel, fixture.errorRegion);
  let timerId = 0;
  const timers = new Map();
  const setTimeout = (callback, delay) => {
    timerId += 1;
    timers.set(timerId, { callback, delay });
    return timerId;
  };
  const clearTimeout = (id) => timers.delete(id);
  const runNextTimer = async () => {
    const next = [...timers.entries()].sort((left, right) => left[1].delay - right[1].delay)[0];
    assert.ok(next, "expected a scheduled wallet poll");
    timers.delete(next[0]);
    await next[1].callback();
  };
  const window = {
    __REMA_SOL_TEST__: {},
    innerHeight: 800,
    setTimeout,
    clearTimeout,
    requestAnimationFrame: (callback) => callback(),
    matchMedia: () => ({ matches: false }),
  };
  const responses = [
    { wallet: { id: "wallet-1", status: "funding_pending" } },
    {
      wallet: {
        id: "wallet-1",
        status: "payment_pending",
        sender_address: "sender-wallet",
        recipient_address: "recipient-wallet",
        selected_sol: "0.002",
        payment_signature: "saved-payment",
      },
    },
    {
      wallet: {
        id: "wallet-1",
        status: "complete",
        sender_address: "sender-wallet",
        recipient_address: "recipient-wallet",
        selected_sol: "0.002",
        payment_signature: "saved-payment",
      },
      explorer_url: "https://explorer.solana.com/tx/saved-payment?cluster=devnet",
    },
  ];
  const requestedPaths = [];
  const context = {
    window,
    document,
    navigator: { clipboard: { writeText: async () => {} } },
    FormData: FakeFormData,
    AbortController,
    Event,
    fetch: async (path) => {
      requestedPaths.push(path);
      const payload = responses.shift();
      return { ok: true, json: async () => payload };
    },
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "..", "remasol", "static", "js", "app.js"), "utf8"),
    context,
    { filename: "remasol/static/js/app.js" },
  );

  await window.__REMA_SOL_TEST__.wallet.requestWalletState(fixture.form);
  assert.match(fixture.resultPanel.textContent, /Payment in progress/);
  assert.equal(fixture.form.submitButton.hidden, true);
  await runNextTimer();
  assert.match(fixture.resultPanel.textContent, /awaiting finalization/);
  await runNextTimer();
  assert.match(fixture.resultPanel.textContent, /Payment confirmed/);
  assert.deepEqual(requestedPaths, ["/wallet/send", "/wallet/recheck", "/wallet/recheck"]);
  assert.equal(timers.size, 0);
  console.log("frontend wallet bounded polling regression: passed");
}

main().then(pollingTest).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
