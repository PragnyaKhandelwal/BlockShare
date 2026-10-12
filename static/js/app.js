// BlockShare front-end helpers: navigation, drag & drop upload with progress, confirmations.
(function () {
  const sidebar = document.querySelector(".sidebar");
  document.querySelectorAll("[data-menu]").forEach((b) =>
    b.addEventListener("click", () => sidebar && sidebar.classList.toggle("open"))
  );

  document.querySelectorAll("form[data-confirm]").forEach((f) =>
    f.addEventListener("submit", (e) => {
      if (!confirm(f.dataset.confirm)) e.preventDefault();
    })
  );

  // copy-to-clipboard for hashes / addresses
  document.querySelectorAll("[data-copy]").forEach((el) => {
    el.style.cursor = "copy";
    el.title = "Click to copy";
    el.addEventListener("click", () => navigator.clipboard && navigator.clipboard.writeText(el.dataset.copy));
  });

  // ---------------------------------------------------------------- upload
  const form = document.getElementById("upload-form");
  if (!form) return;
  const zone = document.getElementById("dropzone");
  const input = form.querySelector("input[type=file]");
  const label = document.getElementById("drop-label");
  const bar = document.getElementById("progress-bar");
  const status = document.getElementById("upload-status");
  const button = form.querySelector("button[type=submit]");

  const show = () => {
    if (input.files.length) {
      const f = input.files[0];
      label.textContent = `${f.name} — ${(f.size / 1048576).toFixed(2)} MB`;
    }
  };
  zone.addEventListener("click", () => input.click());
  input.addEventListener("change", show);
  ["dragenter", "dragover"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); })
  );
  zone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      show();
    }
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!input.files.length) {
      status.textContent = "Choose a file first.";
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        const pct = Math.round((ev.loaded / ev.total) * 100);
        bar.style.width = pct + "%";
        status.textContent = pct < 100
          ? `Sending to coordinator… ${pct}%`
          : "Chunking, hashing, replicating to storage nodes" + (form.dataset.mode === "blockchain" ? " and registering on-chain…" : "…");
      }
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (err) { /* not json */ }
      if (xhr.status === 200 && data.ok) {
        window.location = data.redirect;
      } else {
        button.disabled = false;
        bar.style.width = "0";
        status.textContent = "Upload failed: " + (data.error || xhr.statusText);
      }
    };
    xhr.onerror = () => { button.disabled = false; status.textContent = "Network error during upload."; };
    button.disabled = true;
    xhr.send(new FormData(form));
  });
})();
