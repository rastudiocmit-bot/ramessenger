"use strict";
// Voice input in the chat composer. The browser takes raw sound from the
// microphone and turns it into 16 kHz, 16-bit, mono LPCM — the format Yandex
// SpeechKit accepts as is — so no converter is needed on the server. Audio
// stays in memory until the text arrives or the operator cancels; nothing is
// sent to a client: the text lands in the message field for the operator to send.
//
// The same press-and-hold works with a finger anywhere on the message list:
// hold still for half a second (a moving finger scrolls as usual).
//
// Two gestures on the microphone:
// • tap — the recording panel in the composer; the text goes into the field;
// • press and hold — a large centred microphone; release to finish, slide up to
//   cancel. The text is shown big with a short countdown and then sent by itself,
//   unless the operator taps «Изменить». Anything doubtful (agent fallback, a
//   warning, nothing recognised) is never sent automatically: it goes to the field.
window.CenterVoice = ({ $, state, api, toast }) => {
  const LIMIT = 30, RATE = 16000, HOLD_MS = 220, CANCEL_PX = 90, SEND_DELAY = 3000;
  const STREAM_HOLD_MS = 450, STILL_PX = 10;
  const stream = $("#chat-stream");
  const compose = $("#chat-compose"), input = $("#chat-input"), button = $("#chat-voice");
  const panel = $("#chat-voice-panel"), label = $("#chat-voice-label"), time = $("#chat-voice-time");
  const level = $("#chat-voice-level"), done = $("#chat-voice-done"), cancel = $("#chat-voice-cancel"), retry = $("#chat-voice-retry");
  let recording = null, pending = null, failedAudio = null, run = 0;
  let hold = null, preview = null, suppressClick = false;

  // The full-screen layer for press-and-hold; built here so markup stays in one place.
  const icon = name => window.CenterIcon ? window.CenterIcon(name) : document.createElement("span");
  const layer = document.createElement("div");
  layer.className = "voice-hold"; layer.id = "voice-hold"; layer.hidden = true;
  layer.setAttribute("role", "dialog"); layer.setAttribute("aria-modal", "true"); layer.setAttribute("aria-label", "Голосовое сообщение");
  layer.innerHTML = `<div class="voice-hold-stage">
      <div class="voice-hold-orb"><span class="voice-hold-ring"></span><span class="voice-hold-mic"></span></div>
      <div class="voice-hold-time">0:00</div>
      <div class="voice-hold-hint"></div>
    </div>
    <div class="voice-hold-card" hidden>
      <p class="voice-hold-text"></p>
      <p class="voice-hold-note">Отправим автоматически. Нажмите мимо — не отправлять.</p>
      <div class="voice-hold-bar"><i></i></div>
      <div class="voice-hold-actions"><button type="button" class="btn sec voice-hold-edit">Изменить</button>
        <button type="button" class="btn primary voice-hold-send">Отправить</button></div>
    </div>`;
  layer.querySelector(".voice-hold-mic").append(icon("mic"));
  document.body.append(layer);
  const orb = layer.querySelector(".voice-hold-orb"), holdTime = layer.querySelector(".voice-hold-time");
  const holdHint = layer.querySelector(".voice-hold-hint"), card = layer.querySelector(".voice-hold-card");
  const cardText = layer.querySelector(".voice-hold-text"), bar = layer.querySelector(".voice-hold-bar i");

  const phone = () => matchMedia("(max-width: 719px)").matches;

  // One AudioContext for the page, created and resumed inside a real user gesture.
  // Phones (iOS Safari above all) only let audio start from a tap's end, and the
  // press-and-hold recording begins from a timer, outside any gesture. Unlocking
  // on earlier taps (opening the chat, tapping the field) makes it ready in time.
  let shared = null;
  function audio() {
    if (!shared) { const Context = window.AudioContext || window.webkitAudioContext; shared = new Context(); }
    return shared;
  }
  function unlock() {
    if (!supported() || !state.voiceInput) return;
    try { const context = audio(); if (context.state !== "running") context.resume().then(() => {
      if (!recording) context.suspend().catch(() => {});
    }).catch(() => {}); } catch (_) { /* Not allowed yet: the next gesture tries again. */ }
  }
  for (const type of ["pointerup", "touchend", "keydown"]) document.addEventListener(type, unlock, { capture: true, passive: true });

  // After a hold ends, the phone sends a click to whatever is under the finger
  // (the panel's «Отмена», the message field). Swallow it.
  let swallowUntil = 0;
  document.addEventListener("click", event => {
    if (performance.now() < swallowUntil && !layer.contains(event.target)) { event.preventDefault(); event.stopPropagation(); }
  }, true);
  const supported = () => Boolean(navigator.mediaDevices?.getUserMedia && (window.AudioContext || window.webkitAudioContext));
  const available = () => Boolean(state.voiceInput) && state.workspace && !input.disabled && !compose.hidden && supported();

  function render() {
    const can = available(), busy = Boolean(recording || pending || failedAudio);
    // On a phone one round button: the microphone while the field is empty, "send" once there is text.
    const empty = !input.value.trim();
    // While a finger holds the microphone it must stay in place: it owns the pointer.
    button.hidden = !hold && (!can || busy || (phone() && !empty));
    compose.classList.toggle("voice-swap", can && phone() && empty && !busy);
    compose.classList.toggle("voice-active", busy);
    panel.hidden = !busy || !layer.hidden;
  }
  function phase(kind, text) {
    panel.dataset.phase = kind; label.textContent = text;
    done.hidden = kind !== "recording"; retry.hidden = kind !== "error";
    time.hidden = level.parentElement.hidden = kind !== "recording";
  }
  const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

  function stopCapture() {
    if (!recording) return null;
    const { stream, context, source, processor, ticker, chunks, rate } = recording;
    recording = null; clearInterval(ticker);
    try { processor.onaudioprocess = null; source.disconnect(); processor.disconnect(); } catch (_) { /* Already detached. */ }
    stream.getTracks().forEach(track => track.stop());
    context.suspend().catch(() => {});                  // kept for the next recording (see audio())
    return { chunks, rate };
  }
  function reset() {
    stopCapture(); run += 1; pending = null; failedAudio = null; closeHold(); render();
  }

  // ── press and hold ────────────────────────────────────────────────────────
  function holdPhase(kind) {
    layer.hidden = false; layer.dataset.phase = kind;
    card.hidden = kind !== "preview";
    holdHint.textContent = kind === "recording" ? "Отпустите — отправим\nПроведите вверх — отмена"
      : kind === "cancel" ? "Отпустите — запись удалится" : kind === "working" ? "Распознаём и улучшаем текст…" : "";
  }
  function closeHold() {
    if (preview) { clearTimeout(preview.timer); preview = null; }
    hold = null; layer.hidden = true; layer.dataset.phase = ""; card.hidden = true;
  }
  async function beginHold() {
    if (!hold) return;
    hold.armed = true; navigator.vibrate?.(15);
    holdTime.textContent = `0:00 / ${clock(LIMIT)}`; orb.style.setProperty("--level", "0");
    holdPhase("recording");
    const mine = hold;
    await start();
    if (hold !== mine) return;
    if (!recording) { closeHold(); return; }            // no microphone: start() has explained why
    if (mine.released) {                                 // released while the permission prompt was open
      reset(); toast("Держите палец всё время, пока говорите.", true);
    }
  }
  function endHold(cancelled) {
    const mine = hold; if (!mine) return;
    clearTimeout(mine.timer);
    if (mine.armed) swallowUntil = performance.now() + 600;
    if (!mine.armed) {
      hold = null;
      if (mine.source === "button") { suppressClick = true; start(); }   // a short tap on the microphone
      return;                                                          // a tap or scroll on the list
    }
    mine.released = true;
    if (!recording) return;                              // still waiting for the microphone
    if (cancelled || layer.dataset.phase === "cancel") { reset(); toast("Запись отменена."); return; }
    hold = null;
    finish({ auto: true });
  }
  function showPreview(text) {
    const mine = run;
    const before = input.value.trim();
    cardText.textContent = before ? `${before} ${text}` : text; holdPhase("preview");
    bar.style.animation = "none"; void bar.offsetWidth; bar.style.animation = "";
    bar.style.animationDuration = `${SEND_DELAY}ms`;
    preview = { text, timer: setTimeout(() => { if (mine === run) commit(true); }, SEND_DELAY) };
    layer.querySelector(".voice-hold-send").focus({ preventScroll: true });
  }
  function commit(sendNow) {
    if (!preview) return;
    const text = preview.text; closeHold();
    if (!available()) { insert(text); render(); return; }
    insert(text); render();
    if (sendNow) compose.requestSubmit ? compose.requestSubmit() : compose.dispatchEvent(new Event("submit", { cancelable: true }));
  }

  async function start() {
    if (!available() || recording || pending) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (error) {
      toast(error?.name === "NotAllowedError" || error?.name === "SecurityError"
        ? "Нет доступа к микрофону. Разрешите микрофон для этого сайта в настройках браузера."
        : "Микрофон недоступен на этом устройстве.", true);
      return;
    }
    const context = audio();
    try { await context.resume(); } catch (_) { /* Resumed on the next gesture. */ }
    const source = context.createMediaStreamSource(stream), processor = context.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    let frames = 0, peak = 0;
    processor.onaudioprocess = event => {
      const data = event.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(data)); frames += data.length;
      let sum = 0; for (let i = 0; i < data.length; i += 1) sum += data[i] * data[i];
      peak = Math.max(peak * 0.85, Math.sqrt(sum / data.length));
      if (frames / context.sampleRate >= LIMIT) { const auto = Boolean(hold); hold = null; finish({ auto }); }
    };
    source.connect(processor); processor.connect(context.destination);
    const started = performance.now();
    recording = { stream, context, source, processor, chunks, rate: context.sampleRate, started,
      ticker: setInterval(() => {
        if (!available()) { reset(); return; }
        time.textContent = `${clock((performance.now() - started) / 1000)} / ${clock(LIMIT)}`;
        level.style.transform = `scaleX(${Math.min(1, peak * 6).toFixed(3)})`;
        if (!layer.hidden) {
          holdTime.textContent = time.textContent;
          orb.style.setProperty("--level", Math.min(1, peak * 6).toFixed(3));
        }
      }, 150) };
    time.textContent = `0:00 / ${clock(LIMIT)}`; level.style.transform = "scaleX(0)";
    phase("recording", "Идёт запись"); render();
  }

  // 16 kHz Int16 little-endian, averaging each output sample's source span.
  function encode(chunks, rate) {
    const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0), samples = new Float32Array(total);
    let offset = 0; for (const chunk of chunks) { samples.set(chunk, offset); offset += chunk.length; }
    const ratio = rate / RATE, length = Math.min(Math.floor(total / ratio), LIMIT * RATE), out = new Int16Array(length);
    for (let i = 0; i < length; i += 1) {
      const from = Math.floor(i * ratio), to = Math.max(from + 1, Math.floor((i + 1) * ratio));
      let sum = 0; for (let j = from; j < to && j < total; j += 1) sum += samples[j];
      const value = Math.max(-1, Math.min(1, sum / (to - from)));
      out[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
    }
    return new Uint8Array(out.buffer);
  }
  function base64(bytes) {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  }

  function insert(text) {
    const value = text.trim(); if (!value) return;
    const before = input.value, joined = before && !/\s$/.test(before) ? ` ${value}` : value;
    input.focus(); input.setSelectionRange(before.length, before.length);
    // insertText keeps the browser's undo history (Ctrl+Z removes the dictated text).
    let inserted = false;
    try { inserted = document.execCommand("insertText", false, joined); } catch (_) { inserted = false; }
    if (!inserted || input.value === before) { input.value = before + joined; input.dispatchEvent(new Event("input", { bubbles: true })); }
  }

  async function send(audio, { auto = false } = {}) {
    const mine = ++run;
    pending = audio; failedAudio = null; phase("working", "Распознаём речь и улучшаем текст…"); render();
    if (auto) holdPhase("working");
    try {
      const result = await api(`/workspaces/${state.workspace.id}/voice/transcribe`, { method: "POST", data: { audio: base64(audio) }, timeoutMs: 35000 });
      if (mine !== run) return;
      pending = null; render();
      if (auto && result.text && !result.warning && !result.fallback) { showPreview(result.text); return; }
      closeHold();
      if (result.text) insert(result.text);
      if (result.warning) toast(auto && result.text ? `${result.warning} Проверьте текст и отправьте сами.` : result.warning, !result.text);
    } catch (error) {
      if (mine !== run || error.name === "AbortError") return;
      closeHold(); pending = null; failedAudio = audio;
      phase("error", error.message || "Не удалось распознать запись."); render();
    }
  }
  function finish({ auto = false } = {}) {
    const captured = stopCapture(); if (!captured) return;
    const audio = encode(captured.chunks, captured.rate);
    if (audio.length < RATE * 2 * 0.3) {
      closeHold(); render();
      toast(auto ? "Слишком коротко: удерживайте микрофон, пока говорите." : "Запись слишком короткая.", true); return;
    }
    send(audio, { auto });
  }

  // Keyboard and assistive "click" (no pointer) keep the tap behaviour.
  button.addEventListener("click", event => {
    if (suppressClick || hold) { suppressClick = false; event.preventDefault(); return; }
    if (event.detail === 0) start();
  });
  button.addEventListener("pointerdown", event => {
    suppressClick = false;
    if (event.button !== 0 || !available() || recording || pending || hold || preview) return;
    event.preventDefault();
    try { button.setPointerCapture(event.pointerId); } catch (_) { /* Older browsers: events still reach the button. */ }
    hold = { id: event.pointerId, x: event.clientX, y: event.clientY, armed: false, released: false, source: "button" };
    hold.timer = setTimeout(beginHold, HOLD_MS);
  });

  // Hold a finger still on the message list. Scrolling, taps, links and buttons
  // keep working: only a still finger for STREAM_HOLD_MS starts the recording.
  const interactive = "button, a, input, textarea, select, label, summary, [contenteditable], [role=button]";
  stream.addEventListener("pointerdown", event => {
    if (event.pointerType !== "touch" || !event.isPrimary || !available() || recording || pending || hold || preview) return;
    if (event.target.closest(interactive)) return;
    hold = { id: event.pointerId, x: event.clientX, y: event.clientY, armed: false, released: false, source: "stream" };
    hold.timer = setTimeout(() => {
      if (hold?.source !== "stream") return;
      try { stream.setPointerCapture(hold.id); } catch (_) { /* The pointer may already be gone. */ }
      window.getSelection?.()?.removeAllRanges();
      beginHold();
    }, STREAM_HOLD_MS);
  });
  stream.addEventListener("pointermove", event => {
    if (hold?.source !== "stream" || event.pointerId !== hold.id) return;
    if (!hold.armed) {
      if (Math.hypot(event.clientX - hold.x, event.clientY - hold.y) > STILL_PX) { clearTimeout(hold.timer); hold = null; }
      return;
    }
    if (recording) holdPhase(hold.y - event.clientY > CANCEL_PX ? "cancel" : "recording");
  });
  stream.addEventListener("pointerup", event => { if (hold?.source === "stream" && event.pointerId === hold.id) endHold(false); });
  stream.addEventListener("pointercancel", event => {
    if (hold?.source !== "stream" || event.pointerId !== hold.id) return;
    if (hold.armed) endHold(true); else { clearTimeout(hold.timer); hold = null; }
  });
  // While a hold is pending or recording: no scrolling, text selection or long-press menu.
  stream.addEventListener("touchmove", event => { if (hold?.source === "stream" && hold.armed && event.cancelable) event.preventDefault(); }, { passive: false });
  stream.addEventListener("selectstart", event => { if (hold?.source === "stream") event.preventDefault(); });
  stream.addEventListener("contextmenu", event => { if (hold?.source === "stream") event.preventDefault(); });
  button.addEventListener("pointermove", event => {
    if (!hold?.armed || event.pointerId !== hold.id || !recording) return;
    holdPhase(hold.y - event.clientY > CANCEL_PX ? "cancel" : "recording");
  });
  button.addEventListener("pointerup", event => { if (hold && event.pointerId === hold.id) endHold(false); });
  button.addEventListener("pointercancel", event => { if (hold && event.pointerId === hold.id) endHold(true); });
  button.addEventListener("contextmenu", event => event.preventDefault());
  // No long-press callout, text selection or emulated mouse/click on the microphone.
  button.addEventListener("touchstart", event => { if (event.cancelable) event.preventDefault(); }, { passive: false });
  button.addEventListener("touchend", event => { if (event.cancelable) event.preventDefault(); }, { passive: false });
  layer.querySelector(".voice-hold-send").addEventListener("click", () => commit(true));
  layer.querySelector(".voice-hold-edit").addEventListener("click", () => { commit(false); input.focus(); });
  // A tap on the free space around the card means "no": stop the countdown and
  // leave the text in the field, unsent.
  layer.addEventListener("click", event => {
    if (preview && !card.contains(event.target)) { commit(false); toast("Не отправлено: текст в поле сообщения."); }
  });
  layer.addEventListener("keydown", event => { if (event.key === "Escape" && preview) { commit(false); input.focus(); } });
  done.addEventListener("click", () => finish());
  retry.addEventListener("click", () => { if (failedAudio) send(failedAudio); });
  cancel.addEventListener("click", reset);
  input.addEventListener("input", render);
  document.addEventListener("cm:voice-availability", render);
  document.addEventListener("cm:chat-selected", reset);
  document.addEventListener("visibilitychange", () => { if (document.hidden && recording) reset(); });
  window.addEventListener("resize", render);
  new MutationObserver(render).observe(input, { attributes: true, attributeFilter: ["disabled"] });
  new MutationObserver(render).observe(compose, { attributes: true, attributeFilter: ["hidden"] });
  render();
  return { reset, render };
};
