// BlockShare front-end helpers: theme, navigation, page filter, tabs, charts,
// drag & drop upload with progress, confirmations.
(function () {
  const root = document.documentElement;

  // ---------------------------------------------------------------- theme
  const BS = (window.BS = window.BS || {});
  BS.palette = ["#6366f1", "#14b8a6", "#f59e0b", "#a855f7", "#ec4899", "#0ea5e9", "#64748b"];
  BS.isDark = () => root.getAttribute("data-theme") === "dark";
  BS.css = (name) => getComputedStyle(root).getPropertyValue(name).trim();

  const syncThemeColor = () => {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", BS.isDark() ? "#0d1324" : "#ffffff");
  };
  document.addEventListener("bs:theme", syncThemeColor);
  const setTheme = (t) => {
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("bs-theme", t); } catch (e) { /* storage blocked */ }
    document.dispatchEvent(new CustomEvent("bs:theme", { detail: t }));
  };
  document.querySelectorAll("[data-theme-toggle]").forEach((b) =>
    b.addEventListener("click", () => setTheme(BS.isDark() ? "light" : "dark"))
  );
  // follow the OS setting until the user picks a theme explicitly
  if (window.matchMedia) {
    matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", (e) => {
      let saved = null;
      try { saved = localStorage.getItem("bs-theme"); } catch (err) { /* ignore */ }
      if (!saved) {
        root.setAttribute("data-theme", e.matches ? "dark" : "light");
        document.dispatchEvent(new CustomEvent("bs:theme"));
      }
    });
  }

  // Charts are (re)built from a factory so they pick up theme colours on toggle.
  const charts = [];
  const applyChartDefaults = () => {
    if (!window.Chart) return;
    const d = window.Chart.defaults;
    d.color = BS.css("--muted");
    d.borderColor = BS.css("--line");
    d.font.family = "Inter, system-ui, sans-serif";
    d.font.size = 12;
    d.plugins.legend.labels.usePointStyle = true;
    d.plugins.legend.labels.boxWidth = 8;
    d.plugins.legend.labels.boxHeight = 8;
    d.plugins.legend.labels.padding = 14;
    d.plugins.tooltip.backgroundColor = BS.isDark() ? "#e2e8f0" : "#0f172a";
    d.plugins.tooltip.titleColor = BS.isDark() ? "#0f172a" : "#fff";
    d.plugins.tooltip.bodyColor = BS.isDark() ? "#1e293b" : "#e2e8f0";
    d.plugins.tooltip.padding = 10;
    d.plugins.tooltip.cornerRadius = 8;
    d.plugins.tooltip.boxPadding = 4;
  };
  BS.chart = (canvas, factory) => {
    if (!canvas || !window.Chart) return null;
    applyChartDefaults();
    const entry = { canvas, factory, chart: new window.Chart(canvas, factory()) };
    charts.push(entry);
    return entry.chart;
  };
  document.addEventListener("bs:theme", () => {
    applyChartDefaults();
    charts.forEach((e) => { e.chart.destroy(); e.chart = new window.Chart(e.canvas, e.factory()); });
  });

  // ---------------------------------------------------------------- toast
  let toastEl, toastTimer;
  BS.toast = (msg) => {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "toast";
      toastEl.setAttribute("role", "status");
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 1600);
  };

  // ---------------------------------------------------------------- PWA
  const swMeta = document.querySelector('meta[name="bs-sw"]');
  if ("serviceWorker" in navigator && swMeta && window.isSecureContext) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register(swMeta.content, { scope: "/" }).catch((err) => console.warn("Service worker:", err));
    });
  }
  let installEvent = null;
  const installButtons = document.querySelectorAll("[data-install]");
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    installEvent = e;
    installButtons.forEach((b) => (b.hidden = false));
  });
  installButtons.forEach((b) =>
    b.addEventListener("click", async () => {
      if (!installEvent) return;
      installEvent.prompt();
      await installEvent.userChoice;
      installEvent = null;
      installButtons.forEach((x) => (x.hidden = true));
    })
  );
  window.addEventListener("appinstalled", () => {
    installButtons.forEach((b) => (b.hidden = true));
    BS.toast("BlockShare installed");
  });
  const syncOnline = (announce) => {
    root.classList.toggle("is-offline", !navigator.onLine);
    if (announce) BS.toast(navigator.onLine ? "Back online" : "You're offline");
  };
  window.addEventListener("online", () => syncOnline(true));
  window.addEventListener("offline", () => syncOnline(true));
  syncOnline(false);

  // ---------------------------------------------------------------- sidebar (mobile)
  const sidebar = document.querySelector(".sidebar");
  const scrim = document.querySelector(".scrim");
  const toggleMenu = (open) => {
    if (!sidebar) return;
    const on = open === undefined ? !sidebar.classList.contains("open") : open;
    sidebar.classList.toggle("open", on);
    scrim && scrim.classList.toggle("show", on);
  };
  document.querySelectorAll("[data-menu]").forEach((b) => b.addEventListener("click", () => toggleMenu()));
  scrim && scrim.addEventListener("click", () => toggleMenu(false));

  document.querySelectorAll("form[data-confirm]").forEach((f) =>
    f.addEventListener("submit", (e) => {
      if (!confirm(f.dataset.confirm)) e.preventDefault();
    })
  );

  // copy-to-clipboard for hashes / addresses
  document.querySelectorAll("[data-copy]").forEach((el) => {
    el.classList.add("copyable");
    el.title = "Click to copy";
    el.addEventListener("click", () => {
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(el.dataset.copy).then(() => BS.toast("Copied to clipboard"));
    });
  });

  // ---------------------------------------------------------------- page filter + tabs
  const dataRows = () =>
    Array.from(document.querySelectorAll(".content table tbody tr")).filter(
      (tr) => !(tr.children.length === 1 && tr.children[0].hasAttribute("colspan"))
    );
  const search = document.getElementById("page-search");
  if (search) {
    const apply = () => {
      const q = search.value.trim().toLowerCase();
      dataRows().forEach((tr) =>
        tr.classList.toggle("is-search-hidden", !!q && !tr.textContent.toLowerCase().includes(q))
      );
    };
    search.addEventListener("input", apply);
    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (e.key === "/" && !typing) { e.preventDefault(); search.focus(); }
      if (e.key === "Escape" && document.activeElement === search) { search.value = ""; apply(); search.blur(); }
    });
  }

  document.querySelectorAll("[data-tabs]").forEach((tabs) => {
    const target = document.getElementById(tabs.dataset.tabs);
    if (!target) return;
    const rows = Array.from(target.querySelectorAll("tbody tr[data-group]"));
    const buttons = Array.from(tabs.querySelectorAll("button[data-filter]"));
    buttons.forEach((b) => {
      const f = b.dataset.filter;
      const n = f === "all" ? rows.length : rows.filter((r) => r.dataset.group === f).length;
      const c = b.querySelector(".count");
      if (c) c.textContent = n;
      if (f !== "all" && n === 0 && b.hasAttribute("data-hide-empty")) b.hidden = true;
      b.addEventListener("click", () => {
        buttons.forEach((x) => x.classList.toggle("active", x === b));
        rows.forEach((r) => r.classList.toggle("is-tab-hidden", f !== "all" && r.dataset.group !== f));
      });
    });
  });

  // ---------------------------------------------------------------- upload
  const form = document.getElementById("upload-form");
  if (!form) return;
  const zone = document.getElementById("dropzone");
  const input = form.querySelector("input[type=file]");
  const label = document.getElementById("drop-label");
  const bar = document.getElementById("progress-bar");
  const progress = document.getElementById("upload-progress");
  const metrics = document.getElementById("upload-metrics");
  const status = document.getElementById("upload-status");
  const button = form.querySelector("button[type=submit]");
  const cancel = document.getElementById("cancel-upload");
  const browse = zone.querySelector("button");
  const comment = form.querySelector("input[name=comment]");
  let activeUpload = null;
  cancel.hidden = false;

  const setProgress = (pct) => {
    bar.style.width = (pct === null ? 0 : pct) + "%";
    if (pct === null) progress.removeAttribute("aria-valuenow");
    else progress.setAttribute("aria-valuenow", pct);
  };
  const setBusy = (busy) => {
    button.disabled = busy;
    input.disabled = busy;
    browse.disabled = busy;
    comment.disabled = busy;
    zone.setAttribute("aria-disabled", String(busy));
    form.setAttribute("aria-busy", String(busy));
    cancel.disabled = !busy;
  };
  const finish = (message) => {
    activeUpload = null;
    setBusy(false);
    setProgress(0);
    metrics.textContent = "";
    status.textContent = message;
  };
  cancel.addEventListener("click", () => {
    if (!activeUpload || activeUpload.phase !== "sending") return;
    const xhr = activeUpload.xhr;
    // Invalidate this request first: abort and queued events must not change a retry.
    finish("Transfer cancelled. You can upload the selected file again. If the server already received it, it may still appear in My Files; check there before retrying.");
    xhr.abort();
  });

  const show = () => {
    if (input.files.length) {
      const f = input.files[0];
      label.innerHTML = "";
      const name = document.createElement("strong");
      name.textContent = f.name;
      const size = document.createElement("span");
      size.textContent = `${(f.size / 1048576).toFixed(2)} MB · ready to upload`;
      label.append(name, size);
      zone.classList.add("has-file");
    }
  };
  zone.addEventListener("click", () => { if (!activeUpload) input.click(); });
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (!activeUpload) input.click(); }
  });
  input.addEventListener("change", show);
  ["dragenter", "dragover"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); if (!activeUpload) zone.classList.add("drag"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); })
  );
  zone.addEventListener("drop", (e) => {
    if (!activeUpload && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      show();
    }
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (activeUpload || button.disabled) return;
    if (!input.files.length) {
      status.textContent = "Choose a file first.";
      return;
    }
    // Build the body before disabling fields: disabled inputs are omitted by FormData.
    const body = new FormData(form);
    const xhr = new XMLHttpRequest();
    const request = { xhr, phase: "sending" };
    activeUpload = request;
    const current = () => activeUpload === request;
    const started = performance.now();
    const processing = () => {
      if (!current()) return;
      request.phase = "processing";
      cancel.disabled = true;
      setProgress(100);
      metrics.textContent = "";
      status.textContent = "Chunking, hashing, replicating to storage nodes" + (form.dataset.mode === "blockchain" ? " and registering on-chain…" : "…");
    };
    const failed = (message) => {
      if (current()) finish(message);
    };
    setProgress(0);
    metrics.textContent = "Calculating speed…";
    status.textContent = "Sending to server…";
    xhr.open("POST", form.action);
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    xhr.upload.onprogress = (ev) => {
      if (!current() || request.phase !== "sending") return;
      const info = window.BSUploadProgress.describe(ev.loaded, ev.lengthComputable ? ev.total : null,
                                                    performance.now() - started);
      setProgress(info.percent);
      metrics.textContent = info.text;
      status.textContent = info.percent === null ? "Sending to server…" : `Sending to server… ${info.percent}%`;
      if (info.complete) processing();
    };
    xhr.upload.onload = processing;
    xhr.onload = () => {
      if (!current()) return;
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (err) { /* not json */ }
      if (xhr.status === 200 && data.ok) {
        activeUpload = null;
        cancel.disabled = true;
        window.location = data.redirect;
      } else {
        failed("Upload failed: " + (data.error || xhr.statusText));
      }
    };
    xhr.onerror = () => failed("Network error during upload.");
    xhr.onabort = () => failed("Transfer stopped. Check My Files before retrying if the server already received the file.");
    setBusy(true);
    try {
      xhr.send(body);
    } catch (err) {
      failed("Could not start the upload. Please try again.");
    }
  });
})();
