// Small helpers shared by every page. No framework, no build step.

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

// Ask the server for a status every 2 seconds until onData returns true.
function poll(url, onData, ms = 2000) {
  const timer = setInterval(async () => {
    try {
      if (onData(await api("GET", url)) === true) clearInterval(timer);
    } catch (e) { /* ignore a failed check, try again next time */ }
  }, ms);
}

// Shows the progress bar for a running job and reloads the page when it finishes (or fails).
function watchJob(url) {
  poll(url, (d) => {
    const bar = document.getElementById("bar"), pct = document.getElementById("pct"), detail = document.getElementById("detail");
    if (bar) bar.style.width = (d.progress || 0) + "%";
    if (pct) pct.textContent = (d.progress || 0) + "%";
    if (detail && d.status_detail) detail.textContent = d.status_detail;
    if (["ready", "done", "failed"].includes(d.status)) { location.reload(); return true; }
  });
}

async function removeItem(url, name, redirectTo) {
  if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
  try { await api("DELETE", url); redirectTo ? (location.href = redirectTo) : location.reload(); }
  catch (e) { alert(e.message); }
}

async function renameItem(url, current) {
  const title = prompt("New name:", current);
  if (title === null) return;
  try { await api("PATCH", url, { title }); location.reload(); } catch (e) { alert(e.message); }
}

async function toggleSaved(url, saved) {
  try { await api("PATCH", url, { saved: !saved }); location.reload(); } catch (e) { alert(e.message); }
}

async function retryDocument(id) {
  try { await api("POST", `/api/documents/${id}/retry`); location.reload(); } catch (e) { alert(e.message); }
}

// Upload with a percentage bar, then open the new document.
function initUpload() {
  const input = document.getElementById("file"), btn = document.getElementById("upload-btn");
  const bar = document.getElementById("up-bar"), wrap = document.getElementById("up-wrap");
  const label = document.getElementById("up-label"), err = document.getElementById("up-error");
  if (!input) return;
  btn.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    const file = input.files[0];
    if (!file) return;
    err.textContent = "";
    if (!/\.pdf$/i.test(file.name)) { err.textContent = "Please choose a PDF file."; input.value = ""; return; }
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/documents");
    btn.disabled = true; wrap.classList.remove("hidden");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const p = Math.round((e.loaded / e.total) * 100);
      bar.style.width = p + "%"; label.textContent = p < 100 ? `Uploading… ${p}%` : "Saving…";
    };
    xhr.onload = () => {
      let data = {}; try { data = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status === 201) { location.href = `/documents/${data.id}`; return; }
      err.textContent = data.error || "Upload failed. Please try again.";
      btn.disabled = false; wrap.classList.add("hidden"); input.value = "";
    };
    xhr.onerror = () => { err.textContent = "Upload failed. Is the app still running?"; btn.disabled = false; wrap.classList.add("hidden"); };
    xhr.send(form);
  });
}
document.addEventListener("DOMContentLoaded", initUpload);
