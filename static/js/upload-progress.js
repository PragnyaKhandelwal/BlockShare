// Upload estimates describe the browser-to-server transfer, not server processing.
(function (root) {
  const bytes = (value) => {
    if (value < 1024) return `${Math.round(value)} B`;
    if (value < 1048576) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / 1048576).toFixed(1)} MiB`;
  };
  const duration = (seconds) => {
    const rounded = Math.ceil(seconds);
    if (rounded < 60) return `${rounded}s`;
    if (rounded < 3600) return `${Math.floor(rounded / 60)}m ${rounded % 60}s`;
    return `${Math.floor(rounded / 3600)}h ${Math.floor((rounded % 3600) / 60)}m`;
  };
  const describe = (loaded, total, elapsedMs) => {
    loaded = Number.isFinite(loaded) ? Math.max(0, loaded) : 0;
    const known = Number.isFinite(total) && total > 0;
    if (known) loaded = Math.min(loaded, total);
    const complete = known && loaded >= total;
    // Never show 100% before all bytes are sent, even after rounding.
    const percent = known ? (complete ? 100 : Math.min(99, Math.floor(loaded / total * 100))) : null;
    const sent = known ? `${bytes(loaded)} / ${bytes(total)}` : `${bytes(loaded)} sent`;
    if (!Number.isFinite(elapsedMs) || elapsedMs < 250 || loaded === 0) {
      return { percent, complete, text: `${sent} · Calculating speed…` };
    }
    const speed = loaded / (elapsedMs / 1000);
    const eta = known && !complete ? ` · ~${duration((total - loaded) / speed)} remaining` : "";
    return { percent, complete, text: `${sent} · ${bytes(speed)}/s${eta}` };
  };
  if (typeof module !== "undefined" && module.exports) module.exports = { describe };
  else root.BSUploadProgress = { describe };
})(typeof window !== "undefined" ? window : globalThis);
