"use strict";
// model-in-modal chat page. Plain JS, no build step.
//
// Flow: GET /api/status -> Turnstile -> POST /api/session (token kept in
// sessionStorage) -> POST /api/chat, read the SSE stream with fetch().
// EventSource is not used: it cannot send the Authorization header.

(() => {
  const CFG = window.MIM_CONFIG || {};
  const API = String(CFG.apiBase || "").replace(/\/+$/, "");
  const SESSION_KEY = "mim.session";
  const HISTORY_LIMIT = 20; // messages sent per request; the gateway keeps fewer
  const DEFAULT_WAIT_S = 10;

  const $ = (id) => document.getElementById(id);
  const els = {
    chat: $("chat"), empty: $("empty"), input: $("input"), send: $("send"),
    newChat: $("new-chat"), count: $("count"), banner: $("banner"),
    bannerText: $("banner-text"), bannerAction: $("banner-action"),
    turnstile: $("turnstile"), composer: $("composer"),
  };

  const state = {
    available: false, // /api/status said yes
    session: null, // {token, expiresAt}
    history: [], // completed [{role, content}]
    busy: false, // a chat request is in flight
    abort: null, // AbortController for it
    cooldownUntil: 0, // ms epoch, from a 429
    cooldownTimer: null,
    widgetId: null,
    turnstileWaiter: null,
  };

  // ---------------------------------------------------------------- errors
  class ApiError extends Error {
    constructor(status, body) {
      super(body.message || `HTTP ${status}`);
      this.status = status;
      this.code = body.error || `http_${status}`;
      this.retryAfter = Number(body.retry_after) || 0;
      this.resetsAt = body.resets_at || null;
    }
  }
  class OfflineError extends Error {}
  class TurnstileError extends Error {}

  // ---------------------------------------------------------------- helpers
  const pad = (n) => String(n).padStart(2, "0");

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.ceil(seconds));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? `${h}:${pad(m)}:${pad(r)}` : `${m}:${pad(r)}`;
  }

  function fmtResetDate(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "the start of the next billing cycle";
    return d.toLocaleString(undefined, {
      weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  }

  function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(resolve, ms);
      signal?.addEventListener("abort", () => {
        clearTimeout(t);
        reject(new DOMException("Aborted", "AbortError"));
      }, { once: true });
    });
  }

  // Parse complete SSE events out of `buffer`; return them plus the unfinished tail.
  function parseSSE(buffer) {
    const blocks = buffer.split(/\r?\n\r?\n/);
    const rest = blocks.pop();
    const events = [];
    for (const block of blocks) {
      const data = block.split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).replace(/^ /, ""))
        .join("\n");
      if (!data) continue;
      try { events.push(JSON.parse(data)); } catch { /* ignore malformed event */ }
    }
    return { events, rest };
  }

  async function api(path, { method = "GET", body, token, signal } = {}) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (token) headers.Authorization = `Bearer ${token}`;
    let r;
    try {
      r = await fetch(API + path, {
        method, headers, signal, cache: "no-store", credentials: "omit",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      if (e.name === "AbortError") throw e;
      throw new OfflineError("network error"); // also what a CORS failure looks like
    }
    if (r.ok) return r;
    let data = {};
    try { data = await r.json(); } catch { /* non-JSON error page */ }
    throw new ApiError(r.status, data);
  }

  // ---------------------------------------------------------------- banner
  function showBanner(kind, reason, text, action) {
    els.banner.hidden = false;
    els.banner.dataset.kind = kind; // info | warn | error (colour)
    els.banner.dataset.reason = reason; // which condition it describes
    els.bannerText.textContent = text;
    els.bannerAction.hidden = !action;
    if (action) {
      els.bannerAction.textContent = action.label;
      els.bannerAction.onclick = action.onClick;
    }
  }

  function clearBanner(...reasons) {
    if (!reasons.length || reasons.includes(els.banner.dataset.reason)) {
      els.banner.hidden = true;
      els.banner.dataset.reason = "";
    }
  }

  function showOffline() {
    state.available = false;
    showBanner("error", "offline", "The service is offline right now.", { label: "Retry", onClick: boot });
    updateControls();
  }

  function showExhausted(resetsAt) {
    state.available = false;
    showBanner("error", "credits",
      `This month's free GPU credit is used up. The chat is back on ${fmtResetDate(resetsAt)}.`);
    updateControls();
  }

  function showUnavailable() {
    state.available = false;
    showBanner("warn", "unavailable", "The service is temporarily unavailable.", { label: "Retry", onClick: boot });
    updateControls();
  }

  function startCooldown(seconds, message) {
    state.cooldownUntil = Date.now() + Math.max(1, seconds) * 1000;
    clearInterval(state.cooldownTimer);
    const tick = () => {
      const left = (state.cooldownUntil - Date.now()) / 1000;
      if (left <= 0) {
        clearInterval(state.cooldownTimer);
        state.cooldownTimer = null;
        state.cooldownUntil = 0;
        clearBanner("cooldown");
        updateControls();
        return;
      }
      showBanner("warn", "cooldown", `${message || "Rate limit reached."} You can send again in ${fmtDuration(left)}.`);
    };
    tick();
    state.cooldownTimer = setInterval(tick, 1000);
    updateControls();
  }

  // ---------------------------------------------------------------- session + Turnstile
  let markTurnstileLoaded;
  const turnstileLoaded = new Promise((resolve) => { markTurnstileLoaded = resolve; });
  window.mimTurnstileLoaded = () => markTurnstileLoaded();
  if (window.turnstile) markTurnstileLoaded();

  function settleTurnstile(error, token) {
    const waiter = state.turnstileWaiter;
    state.turnstileWaiter = null;
    if (waiter) error ? waiter.reject(error) : waiter.resolve(token);
  }

  async function getTurnstileToken() {
    const loaded = await Promise.race([turnstileLoaded.then(() => true), sleep(20000).then(() => false)]);
    if (!loaded || !window.turnstile) throw new TurnstileError("Turnstile did not load");
    return new Promise((resolve, reject) => {
      state.turnstileWaiter = { resolve, reject };
      if (state.widgetId === null) {
        state.widgetId = window.turnstile.render(els.turnstile, {
          sitekey: CFG.turnstileSiteKey,
          action: "session",
          theme: "auto",
          size: "flexible",
          appearance: "interaction-only", // only visible if a human check is needed
          retry: "never", // failures surface as a banner with a retry button
          callback: (token) => settleTurnstile(null, token),
          "error-callback": () => { settleTurnstile(new TurnstileError("challenge error")); return true; },
          "timeout-callback": () => settleTurnstile(new TurnstileError("challenge timed out")),
        });
      } else {
        window.turnstile.reset(state.widgetId); // runs a fresh challenge
      }
    });
  }

  function loadSession() {
    try {
      const s = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
      if (s && s.token && Date.parse(s.expiresAt) - Date.now() > 60_000) return s;
    } catch { /* storage unavailable or corrupt */ }
    return null;
  }

  function saveSession(s) {
    try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(s)); } catch { /* private mode */ }
  }

  function dropSession() {
    state.session = null;
    try { sessionStorage.removeItem(SESSION_KEY); } catch { /* ignore */ }
  }

  let sessionPromise = null;
  function ensureSession() {
    const s = state.session || loadSession();
    if (s) {
      state.session = s;
      return Promise.resolve(s.token);
    }
    if (!sessionPromise) sessionPromise = createSession().finally(() => { sessionPromise = null; });
    return sessionPromise;
  }

  async function createSession() {
    clearBanner("turnstile");
    let turnstileToken;
    try {
      turnstileToken = await getTurnstileToken();
    } catch (e) {
      showBanner("warn", "turnstile", "We couldn't verify your browser.", { label: "Try again", onClick: retrySession });
      throw e;
    }
    const r = await api("/api/session", { method: "POST", body: { turnstile_token: turnstileToken } });
    const data = await r.json();
    state.session = { token: data.token, expiresAt: data.expires_at };
    saveSession(state.session);
    return state.session.token;
  }

  function retrySession() {
    clearBanner("turnstile", "session");
    ensureSession().catch(handleSessionError);
  }

  function handleSessionError(e) {
    if (e instanceof TurnstileError) return; // banner already shown
    if (e instanceof OfflineError) return showOffline();
    if (e instanceof ApiError) {
      if (e.code === "credits_exhausted") return showExhausted(e.resetsAt);
      if (e.code === "unavailable") return showUnavailable();
      return showBanner("warn", "session", e.code === "turnstile_failed"
        ? "Verification failed." : e.message, { label: "Try again", onClick: retrySession });
    }
    showBanner("warn", "session", "Something went wrong.", { label: "Try again", onClick: retrySession });
  }

  // ---------------------------------------------------------------- transcript
  function addBubble(role, text) {
    const el = document.createElement("div");
    el.className = `msg ${role}`;
    const body = document.createElement("span");
    body.className = "text";
    body.textContent = text; // never innerHTML: model output is untrusted
    const status = document.createElement("span");
    status.className = "status";
    el.append(body, status);
    els.empty.hidden = true;
    els.chat.append(el);
    scrollToEnd(true);
    return el;
  }

  function setStatus(bubble, status, note = "") {
    bubble.dataset.status = status;
    bubble.querySelector(".status").textContent = note;
  }

  function scrollToEnd(force = false) {
    const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
    if (force || nearBottom) window.scrollTo({ top: document.body.scrollHeight });
  }

  function showWaiting(bubble, since) {
    setStatus(bubble, "waiting",
      `Waking up the model (${fmtDuration((Date.now() - since) / 1000)}). ` +
      "Starting a GPU usually takes 1–15 minutes; keep this tab open.");
  }

  // ---------------------------------------------------------------- chat
  // Returns "done" (keep), "undo" (remove the turn, restore the input) or "error".
  async function chat(bubble, signal) {
    let reauthed = false;
    let waitingSince = null;
    let ticker = null;
    try {
      for (;;) {
        try {
          const token = await ensureSession();
          const r = await api("/api/chat", {
            method: "POST", token, signal,
            body: { messages: state.history.slice(-HISTORY_LIMIT) },
          });
          clearBanner("offline", "busy", "unavailable");
          return await readStream(r, bubble, signal);
        } catch (e) {
          if (e.name === "AbortError") return stopped(bubble, "");
          if (e instanceof TurnstileError) return "undo";
          if (e instanceof OfflineError) { showOffline(); return "undo"; }
          if (!(e instanceof ApiError)) throw e;
          switch (e.code) {
            case "unauthorized": // expired or invalid session: run Turnstile again, retry once
              dropSession();
              if (!reauthed) { reauthed = true; continue; }
              showBanner("warn", "session", "Your session could not be renewed.", { label: "Reload", onClick: () => location.reload() });
              return "undo";
            case "warming_up":
              if (waitingSince === null) {
                waitingSince = Date.now();
                ticker = setInterval(() => showWaiting(bubble, waitingSince), 1000);
              }
              showWaiting(bubble, waitingSince);
              try { await sleep((e.retryAfter || DEFAULT_WAIT_S) * 1000, signal); } catch { return stopped(bubble, ""); }
              continue;
            case "rate_limited":
              startCooldown(e.retryAfter, e.message);
              return "undo";
            case "busy":
              showBanner("warn", "busy", "The model is busy right now. Try again shortly.");
              return "undo";
            case "credits_exhausted":
              showExhausted(e.resetsAt);
              return "undo";
            case "unavailable":
              showUnavailable();
              return "undo";
            case "turnstile_failed":
            case "turnstile_unavailable":
              showBanner("warn", "turnstile", "We couldn't verify your browser.", { label: "Try again", onClick: retrySession });
              return "undo";
            default:
              setStatus(bubble, "error", e.message || "Something went wrong.");
              return "error";
          }
        }
      }
    } finally {
      clearInterval(ticker);
    }
  }

  async function readStream(r, bubble, signal) {
    setStatus(bubble, "streaming");
    const textEl = bubble.querySelector(".text");
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", text = "", finished = false, failure = null, note = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parsed = parseSSE(buffer);
        buffer = parsed.rest;
        for (const ev of parsed.events) {
          if (ev.type === "delta" && typeof ev.text === "string") {
            text += ev.text;
            textEl.textContent = text;
            scrollToEnd();
          } else if (ev.type === "done") {
            finished = true;
            if (ev.finish_reason === "length") note = "Reply cut off at the length limit.";
          } else if (ev.type === "error") {
            failure = ev.message || "The reply stopped early.";
          }
        }
      }
    } catch (e) {
      if (signal.aborted) return stopped(bubble, text);
      failure = "The connection dropped.";
    }
    if (text) state.history.push({ role: "assistant", content: text });
    if (failure || !finished) {
      setStatus(bubble, "error", failure || "The connection dropped.");
      return text ? "done" : "error";
    }
    setStatus(bubble, "done", note);
    return "done";
  }

  function stopped(bubble, text) {
    if (!text) return "undo";
    state.history.push({ role: "assistant", content: text });
    setStatus(bubble, "stopped", "Stopped.");
    return "done";
  }

  async function send(text) {
    const userBubble = addBubble("user", text);
    const bot = addBubble("assistant", "");
    setStatus(bot, "pending");
    const userTurn = { role: "user", content: text };
    state.history.push(userTurn);
    state.busy = true;
    state.abort = new AbortController();
    updateControls();

    let outcome;
    try {
      outcome = await chat(bot, state.abort.signal);
    } catch {
      setStatus(bot, "error", "Something went wrong.");
      outcome = "error";
    }
    state.busy = false;
    state.abort = null;

    // Don't resend an unanswered user turn with the next message.
    if (outcome !== "done" && state.history.at(-1) === userTurn) state.history.pop();
    if (outcome === "undo") {
      userBubble.remove();
      bot.remove();
      if (!els.input.value) els.input.value = text;
      if (!els.chat.querySelector(".msg")) els.empty.hidden = false;
      autosize();
    }
    updateControls();
  }

  // ---------------------------------------------------------------- controls
  function canSend() {
    return state.available && !state.busy && Date.now() >= state.cooldownUntil;
  }

  function updateControls() {
    els.input.disabled = !state.available;
    els.send.textContent = state.busy ? "Stop" : "Send";
    els.send.disabled = state.busy ? false : !(canSend() && els.input.value.trim());
    els.newChat.disabled = state.busy || !els.chat.querySelector(".msg");
    els.count.textContent = `${els.input.value.length} / ${els.input.maxLength}`;
  }

  function autosize() {
    els.input.style.height = "auto";
    els.input.style.height = `${els.input.scrollHeight + 2}px`;
    updateControls();
  }

  els.composer.addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.busy) { state.abort?.abort(); return; } // the button reads "Stop"
    const text = els.input.value.trim();
    if (!text || !canSend()) return;
    els.input.value = "";
    autosize();
    send(text);
  });

  els.input.addEventListener("input", autosize);
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      els.composer.requestSubmit();
    }
  });

  els.newChat.addEventListener("click", () => {
    state.history = [];
    els.chat.querySelectorAll(".msg").forEach((m) => m.remove());
    els.empty.hidden = false;
    updateControls();
    els.input.focus();
  });

  // ---------------------------------------------------------------- start
  async function boot() {
    clearBanner("offline", "unavailable");
    if (!API) return showBanner("error", "offline", "The page is missing its API address.");
    try {
      const r = await api("/api/status");
      const s = await r.json();
      if (s.available) {
        state.available = true;
      } else if (s.reason === "credits_exhausted") {
        showExhausted(s.resets_at);
      } else {
        showUnavailable();
      }
    } catch {
      showOffline();
    }
    updateControls();
    // Start Turnstile + session right away: creating a session also wakes a GPU,
    // so the model is warming up while the visitor types.
    if (state.available) ensureSession().catch(handleSessionError);
  }

  boot();
})();
