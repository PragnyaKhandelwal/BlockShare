// Exercise the real page script with controllable XHR events, including late events.
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const script = fs.readFileSync(path.join(__dirname, "../static/js/app.js"), "utf8");

function page({ sendThrows = false } = {}) {
  class Element {
    constructor() {
      this.listeners = {}; this.attrs = {}; this.style = {}; this.dataset = {};
      this.disabled = false; this.textContent = ""; this.files = [];
      this.classList = { add() {}, remove() {}, toggle() {} };
    }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    fire(type, values = {}) {
      for (const fn of this.listeners[type] || []) fn({ preventDefault() {}, ...values });
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] ?? null; }
    removeAttribute(k) { delete this.attrs[k]; }
    querySelector() { return null; }
  }
  const ids = Object.fromEntries(["upload-form", "dropzone", "drop-label", "progress-bar",
    "upload-progress", "upload-metrics", "upload-status"].map(id => [id, new Element()]));
  const input = new Element(), submit = new Element(), browse = new Element(), comment = new Element();
  input.files = [{ name: "report.txt" }]; comment.value = "first draft";
  const form = ids["upload-form"];
  form.action = "/upload/"; form.dataset.mode = "conventional";
  form.querySelector = selector => ({ "input[type=file]": input, "button[type=submit]": submit,
    "input[name=comment]": comment })[selector];
  ids.dropzone.querySelector = () => browse;
  const requests = [], window = { addEventListener() {}, location: "unchanged",
    BSUploadProgress: require("../static/js/upload-progress.js") };
  class XHR {
    constructor() { this.upload = {}; requests.push(this); }
    open() {} setRequestHeader() {}
    send(body) { this.body = body; if (sendThrows) throw Error("send failed"); }
    abort() { this.aborted = true; this.onabort?.(); }
  }
  const document = { documentElement: new Element(), addEventListener() {},
    querySelectorAll: () => [], querySelector: () => null, getElementById: id => ids[id] || null };
  vm.runInNewContext(script, { window, document, navigator: { onLine: true },
    XMLHttpRequest: XHR, performance: { now: () => 1000 },
    FormData: class { constructor() { this.file = input.disabled ? null : input.files[0];
      this.comment = comment.disabled ? null : comment.value; } } });
  return { ...ids, form, input, submit, browse, comment, requests, window,
    status: ids["upload-status"], metrics: ids["upload-metrics"] };
}

test("cancel aborts the transfer, clears estimates and retains the file for retry", () => {
  const p = page();
  p.form.fire("submit");
  const xhr = p.requests[0];
  assert.equal(xhr.body.file.name, "report.txt");
  assert.equal(xhr.body.comment, "first draft");
  assert.equal(p.input.disabled, true);
  assert.equal(p.submit.textContent, "Cancel Upload");
  assert.equal(p.submit.type, "button");
  p.metrics.textContent = "~8s remaining";
  p.submit.fire("click");
  assert.equal(xhr.aborted, true);
  assert.equal(p.submit.disabled, false);
  assert.equal(p.input.disabled, false);
  assert.equal(p.submit.textContent, "Start Upload");
  assert.equal(p.submit.type, "submit");
  assert.equal(p.metrics.textContent, "");
  assert.equal(p["upload-progress"].getAttribute("aria-valuenow"), "0");
  assert.equal(p.input.files[0].name, "report.txt");
  assert.match(p.status.textContent, /Transfer cancelled/);
  assert.match(p.status.textContent, /My Files/);
});

test("late events from a cancelled request cannot change or redirect a retry", () => {
  const p = page();
  p.form.fire("submit");
  const old = p.requests[0];
  p.submit.fire("click");
  p.form.fire("submit");
  old.upload.onprogress({ loaded: 100, total: 100, lengthComputable: true });
  old.upload.onload(); old.onerror(); old.onabort();
  old.status = 200; old.responseText = JSON.stringify({ ok: true, redirect: "/wrong/" }); old.onload();
  assert.equal(p.window.location, "unchanged");
  assert.equal(p.submit.disabled, false);
  assert.equal(p.submit.textContent, "Cancel Upload");
  assert.equal(p.submit.type, "button");
  assert.equal(p.status.textContent, "Sending to server…");
  p.requests[1].status = 200;
  p.requests[1].responseText = JSON.stringify({ ok: true, redirect: "/files/right/" });
  p.requests[1].onload();
  assert.equal(p.window.location, "/files/right/");
});

test("processing disables cancellation for known and unknown upload totals", () => {
  for (const completion of [xhr => xhr.upload.onprogress({ loaded: 100, total: 100, lengthComputable: true }),
                            xhr => xhr.upload.onload()]) {
    const p = page(); p.form.fire("submit");
    const xhr = p.requests[0]; completion(xhr);
    assert.equal(p.submit.textContent, "Processing…");
    assert.equal(p.submit.type, "button");
    p.submit.fire("click");
    assert.equal(xhr.aborted, undefined);
    assert.equal(p.submit.disabled, true);
    assert.match(p.status.textContent, /Chunking/);
    xhr.upload.onprogress({ loaded: 50, total: 100, lengthComputable: true });
    assert.match(p.status.textContent, /Chunking/);
  }
});

test("duplicate submits and file drops cannot replace an active upload", () => {
  const p = page(); p.form.fire("submit"); p.form.fire("submit");
  p.dropzone.fire("drop", { dataTransfer: { files: [{ name: "other.txt" }] } });
  assert.equal(p.requests.length, 1);
  assert.equal(p.input.files[0].name, "report.txt");
});

test("network errors, server errors and unexpected aborts release controls", () => {
  for (const fail of [xhr => xhr.onerror(), xhr => xhr.onabort(), xhr => {
    xhr.status = 400; xhr.responseText = JSON.stringify({ error: "too large" }); xhr.onload();
  }]) {
    const p = page(); p.form.fire("submit"); fail(p.requests[0]);
    assert.equal(p.submit.disabled, false);
    assert.equal(p.submit.textContent, "Start Upload");
    assert.equal(p.submit.type, "submit");
    assert.equal(p.input.disabled, false);
    p.form.fire("submit"); assert.equal(p.requests.length, 2);
  }
});

test("cancel before starting is harmless and send failures allow retry", () => {
  const p = page({ sendThrows: true });
  p.submit.fire("click"); assert.equal(p.requests.length, 0);
  p.form.fire("submit");
  assert.equal(p.submit.disabled, false);
  assert.equal(p.submit.textContent, "Start Upload");
  assert.equal(p.submit.type, "submit");
  assert.match(p.status.textContent, /Could not start/);
});
