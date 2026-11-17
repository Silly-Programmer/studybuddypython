// Shared helpers for every page. No framework, no build step.
// This file is loaded in <head> WITHOUT "defer", so page scripts can use it right away.

const $ = (id) => document.getElementById(id);
const h = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

async function api(method, url, body) {
  let res;
  try {
    res = await fetch(url, {
      method, cache: "no-store",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error("Can't reach the app. Check your connection and try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

// ---------- toasts and dialogs (nicer than alert/confirm/prompt, and they fit a phone) ----------
function toast(message, kind = "error") {
  let box = $("toasts");
  if (!box) { box = h("div"); box.id = "toasts"; document.body.append(box); }
  const t = h("div", "toast " + kind, message);
  box.append(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 3800);
}

// ask({title, text, confirmText, cancelText, danger}) -> true/false
// ask({title, input: "current value"})               -> the new text, or null if cancelled
function ask(opts) {
  return new Promise((resolve) => {
    const d = h("dialog", "sheet");
    if (!d.showModal) return resolve(opts.input !== undefined ? prompt(opts.title, opts.input) : confirm(opts.title));
    const form = h("form", "sheet-body"); form.method = "dialog";
    form.append(h("h3", "sheet-title", opts.title));
    if (opts.text) form.append(h("p", "sheet-text", opts.text));
    let field = null;
    if (opts.input !== undefined) {
      field = h("input", "sheet-input"); field.type = "text"; field.value = opts.input; field.maxLength = 120; field.required = true;
      form.append(field);
    }
    const row = h("div", "sheet-actions");
    const cancel = h("button", "btn btn-ghost", opts.cancelText || "Cancel"); cancel.value = "cancel"; cancel.formNoValidate = true;
    const ok = h("button", "btn " + (opts.danger ? "btn-danger" : "btn-primary"), opts.confirmText || "OK"); ok.value = "ok";
    row.append(cancel, ok); form.append(row); d.append(form); document.body.append(d);
    d.addEventListener("close", () => {
      const accepted = d.returnValue === "ok", value = field ? field.value.trim() : null;
      d.remove(); resolve(field ? (accepted ? value : null) : accepted);
    });
    d.addEventListener("click", (e) => { if (e.target === d) d.close("cancel"); });  // tap outside to cancel
    d.showModal();
    if (field) { field.focus(); field.select(); }
  });
}

async function removeItem(url, name, redirectTo) {
  const yes = await ask({ title: "Delete this?", text: `“${name}” will be removed for good.`, confirmText: "Delete", danger: true });
  if (!yes) return;
  try { await api("DELETE", url); redirectTo ? (location.href = redirectTo) : location.reload(); }
  catch (e) { toast(e.message); }
}

async function renameItem(url, current) {
  const title = await ask({ title: "Rename", input: current, confirmText: "Save" });
  if (title === null || title === current) return;
  try { await api("PATCH", url, { title }); location.reload(); } catch (e) { toast(e.message); }
}

async function toggleSaved(url, saved) {
  try { await api("PATCH", url, { saved: !saved }); location.reload(); } catch (e) { toast(e.message); }
}

async function retryDocument(id, btn) {
  if (btn) btn.disabled = true;
  try { await api("POST", `/api/documents/${id}/retry`); location.reload(); }
  catch (e) { toast(e.message); if (btn) btn.disabled = false; }
}

// ---------- live status ----------
// Anything on the page marked  data-watch="doc:ID" | "sum:ID" | "quiz:ID"  is kept up to date by ONE request every
// moment or two. data-role="panel" is the big progress card (the page reloads when the job ends);
// data-role="badge" is a small status pill in a list (it just changes in place).
function applyStatus(el, s) {
  if (el.dataset.role === "badge") {
    el.dataset.tone = s.tone;
    el.dataset.active = String(s.active);
    const label = el.querySelector(".js-label");
    if (label) label.textContent = s.label + (s.active && s.progress ? ` · ${s.progress}%` : "");
  } else if (el.dataset.role === "panel") {
    const bar = el.querySelector(".js-bar"), pct = el.querySelector(".js-pct"), detail = el.querySelector(".js-detail");
    if (bar) bar.style.width = s.progress + "%";
    if (pct) pct.textContent = s.progress + "%";
    if (detail && s.status_detail) detail.textContent = s.status_detail;
  }
}

function startLive() {
  const els = [...document.querySelectorAll("[data-watch]")];
  if (!els.length) return;
  const finished = new Set();
  let timer = null, delay = 1500, busy = false, leaving = false;
  const activeKeys = () => [...new Set(els.map((e) => e.dataset.watch))].filter((k) => !finished.has(k));

  async function tick() {
    clearTimeout(timer);
    const keys = activeKeys();
    if (!keys.length || leaving || busy || document.hidden) return;  // hidden tabs resume from "visibilitychange"
    busy = true;
    try {
      const params = new URLSearchParams();
      keys.forEach((k) => { const [kind, id] = k.split(":"); params.append(kind, id); });
      const data = await api("GET", "/api/status?" + params);
      delay = 1500;
      for (const el of els) {
        const key = el.dataset.watch, s = data[key];
        if (!s) { finished.add(key); continue; }  // it was deleted
        applyStatus(el, s);
        if (!s.active) {
          finished.add(key);
          if (el.dataset.role === "panel" && !leaving) { leaving = true; location.reload(); }
        }
      }
    } catch (e) {
      delay = Math.min(delay * 1.6, 10000);  // server busy or phone offline: back off, keep trying
    }
    busy = false;
    timer = setTimeout(tick, delay);
  }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  window.addEventListener("online", tick);
  window.addEventListener("pageshow", tick);
  tick();
}

// ---------- upload ----------
function initUpload() {
  const zone = $("dropzone"), input = $("file"), btn = $("upload-btn");
  if (!zone || !input) return;
  const wrap = $("up-wrap"), bar = $("up-bar"), label = $("up-label"), err = $("up-error");
  const maxMb = Number(zone.dataset.maxMb) || 50;
  let busy = false;

  const reset = (message) => {
    busy = false; btn.disabled = false; wrap.classList.add("hidden"); input.value = ""; err.textContent = message || "";
  };

  function send(file) {
    if (busy || !file) return;
    err.textContent = "";
    if (!/\.pdf$/i.test(file.name)) return reset("Please choose a PDF file.");
    if (file.size > maxMb * 1024 * 1024) return reset(`This file is too large. The limit is ${maxMb} MB.`);
    busy = true; btn.disabled = true;
    wrap.classList.remove("hidden"); bar.style.width = "0%"; label.textContent = "Uploading… 0%";

    const form = new FormData(); form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/documents");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const p = Math.round((e.loaded / e.total) * 100);
      bar.style.width = p + "%";
      label.textContent = p < 100 ? `Uploading… ${p}%` : "Saving, one moment…";
    };
    xhr.onload = () => {
      let data = {}; try { data = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
      if (xhr.status === 201) { location.href = `/documents/${data.id}`; return; }
      reset(data.error || "Upload failed. Please try again.");
    };
    xhr.onerror = () => reset("Upload failed. Check your connection and that the app is still running.");
    xhr.send(form);
  }

  btn.addEventListener("click", () => input.click());
  input.addEventListener("change", () => send(input.files[0]));
  // Drag and drop (desktop)
  ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.dataset.drag = "true"; }));
  ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.dataset.drag = "false"; }));
  zone.addEventListener("drop", (e) => send(e.dataTransfer.files[0]));
}

// ---------- the Back button ----------
// Browsers keep a frozen copy of the previous page and show it when you press Back, which is why an old
// progress bar (or a half-finished upload) could appear. When that happens, load the page again.
// A page can opt out with <body data-keep> (the quiz page does, so you never lose your answers).
window.addEventListener("pageshow", (e) => {
  if (e.persisted && !document.body.hasAttribute("data-keep")) location.reload();
});

document.addEventListener("DOMContentLoaded", () => { initUpload(); startLive(); });
