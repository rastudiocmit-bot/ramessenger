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
  const LIMIT = 30, RATE = 16000, HOLD_MS = 220, CANCEL_PX = 90, EDIT_PX = { button: 34, stream: 60 };
  // Time to read the dictated text before it goes: about 12 characters a second
  // (an unhurried reading pace for a message one is checking), never under 7 s
  // and never over 30 s.
  const sendDelay = text => Math.round(Math.min(30000, Math.max(7000, 2500 + text.length * 85)));
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
      <button type="button" class="voice-hold-clear" aria-label="Удалить текст, ничего не отправлять" title="Удалить"></button>
      <p class="voice-hold-text"></p>
      <p class="voice-hold-note">Отправим автоматически. Нажмите мимо — не отправлять.</p>
      <div class="voice-hold-bar"><i></i></div>
      <div class="voice-hold-actions"><button type="button" class="btn sec voice-hold-edit">Изменить</button>
        <button type="button" class="btn primary voice-hold-send">Отправить</button></div>
    </div>`;
  layer.querySelector(".voice-hold-mic").append(icon("mic"));
  layer.querySelector(".voice-hold-clear").append(icon("close"));
  document.body.append(layer);
  const stage = layer.querySelector(".voice-hold-stage");
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
    // Once running it stays running: Safari only resumes audio inside a tap, and a
    // recording may begin outside one (press-and-hold, after the mic prompt).
    try { const context = audio(); if (context.state !== "running") context.resume().catch(() => {}); }
    catch (_) { /* Not allowed yet: the next gesture tries again. */ }
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

  // Composer: a small ✕ in the field's top-right corner clears the message.
  const clear = $("#chat-clear");
  clear.addEventListener("click", () => {
    if (!input.value) return;
    input.value = ""; input.dispatchEvent(new Event("input", { bubbles: true })); render();
  });
  // chat.js sets the field without an input event (draft restore, after sending):
  // keep the ✕ and the phone's mic/send swap in step with it.
  setInterval(() => {
    if (clear.hidden !== (!input.value || input.disabled) && !document.hidden) render();
  }, 400);
  function render() {
    clear.hidden = !input.value || input.disabled;
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
    void context;                                        // kept running for the next recording (see unlock())
    return { chunks, rate };
  }
  function reset() {
    stopCapture(); run += 1; pending = null; failedAudio = null; closeHold(); render();
  }

  // ── press and hold ────────────────────────────────────────────────────────
  function holdPhase(kind) {
    layer.hidden = false; layer.dataset.phase = kind;
    card.hidden = kind !== "preview";
    holdHint.textContent = kind === "recording" ? "Отпустите — отправим\nВверх — отмена · вниз — в поле"
      : kind === "cancel" ? "Отпустите — запись удалится"
      : kind === "edit" ? "Отпустите — текст в поле, без отправки"
      : kind === "working" ? "Распознаём и улучшаем текст…" : "";
  }
  // While a finger holds (on the list or the microphone) the chat underneath must
  // neither scroll nor select text.
  const scroller = stream.closest(".canvas") || stream;
  function lockList(on) {
    document.documentElement.classList.toggle("voice-holding", on);
    if (on) window.getSelection?.()?.removeAllRanges();
  }
  // The orb follows the finger vertically, a little behind it, with resistance.
  function dragOrb(dy) {
    const eased = Math.sign(dy) * Math.min(170, Math.abs(dy) * 0.85);
    stage.style.transform = dy ? `translateY(${eased.toFixed(1)}px) scale(${(1 - Math.min(Math.abs(eased), 170) / 900).toFixed(3)})` : "";
  }
  function track(event) {
    if (!hold?.armed || event.pointerId !== hold.id || !recording) return;
    const dy = event.clientY - hold.y;
    stage.classList.add("dragging"); dragOrb(dy);
    holdPhase(-dy > CANCEL_PX ? "cancel" : dy > EDIT_PX[hold.source] ? "edit" : "recording");
  }
  function closeHold() {
    lockList(false); stage.classList.remove("dragging"); dragOrb(0);
    if (preview) { clearTimeout(preview.timer); preview = null; }
    hold = null; layer.hidden = true; layer.dataset.phase = ""; card.hidden = true;
  }
  async function beginHold() {
    if (!hold) return;
    hold.armed = true; navigator.vibrate?.(15); lockList(true); hold.scrollTop = scroller.scrollTop;
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
    if (cancelled || layer.dataset.phase === "cancel") {
      // The orb flies off upwards, then the layer closes.
      stopCapture(); run += 1; pending = null; failedAudio = null; hold = null;
      stage.classList.remove("dragging"); stage.classList.add("flying-up");
      setTimeout(() => { stage.classList.remove("flying-up"); closeHold(); render(); toast("Запись отменена."); }, 250);
      return;
    }
    const toField = layer.dataset.phase === "edit";
    hold = null; stage.classList.remove("dragging"); dragOrb(0);
    finish({ auto: !toField, overlay: true });
  }
  function showPreview(text) {
    const mine = run;
    const before = input.value.trim();
    cardText.textContent = before ? `${before} ${text}` : text; holdPhase("preview");
    bar.style.animation = "none"; void bar.offsetWidth; bar.style.animation = "";
    const delay = sendDelay(cardText.textContent);
    bar.style.animationDuration = `${delay}ms`;
    preview = { text, timer: setTimeout(() => { if (mine === run) commit(true); }, delay) };
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
    // Resume now, while still inside the tap; after awaiting the microphone it is too late on iOS.
    const context = audio();
    if (context.state !== "running") context.resume().catch(() => {});
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (error) {
      toast(error?.name === "NotAllowedError" || error?.name === "SecurityError"
        ? "Нет доступа к микрофону. Разрешите микрофон для этого сайта в настройках браузера."
        : "Микрофон недоступен на этом устройстве.", true);
      return;
    }
    if (context.state !== "running") { try { await context.resume(); } catch (_) { /* Checked below. */ } }
    const source = context.createMediaStreamSource(stream), processor = context.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    let frames = 0, peak = 0;
    processor.onaudioprocess = event => {
      const data = event.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(data)); frames += data.length; if (recording) recording.frames = frames;
      let sum = 0; for (let i = 0; i < data.length; i += 1) sum += data[i] * data[i];
      peak = Math.max(peak * 0.85, Math.sqrt(sum / data.length));
      if (frames / context.sampleRate >= LIMIT) { const auto = Boolean(hold); hold = null; finish({ auto }); }
    };
    source.connect(processor); processor.connect(context.destination);
    const started = performance.now();
    recording = { stream, context, source, processor, chunks, rate: context.sampleRate, started,
      frames: 0,
      ticker: setInterval(() => {
        if (!available()) { reset(); return; }
        // No sound at all after 1.5 s: the browser did not start audio. Say so,
        // instead of a misleading «too short» when the operator lets go.
        if (recording && !recording.frames && performance.now() - started > 1500) {
          reset(); toast("Микрофон не передаёт звук. Коснитесь экрана и попробуйте ещё раз.", true); return;
        }
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

  async function send(audio, { auto = false, overlay = auto } = {}) {
    const mine = ++run;
    pending = audio; failedAudio = null; phase("working", "Распознаём речь и улучшаем текст…"); render();
    if (overlay) holdPhase("working");
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
  function finish({ auto = false, overlay = auto } = {}) {
    const captured = stopCapture(); if (!captured) return;
    const audio = encode(captured.chunks, captured.rate);
    if (audio.length < RATE * 2 * 0.3) {
      closeHold(); render();
      toast(overlay ? "Слишком коротко: держите палец, пока говорите." : "Запись слишком короткая.", true); return;
    }
    send(audio, { auto, overlay });
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
    document.documentElement.classList.add("voice-pending");
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
      if (Math.hypot(event.clientX - hold.x, event.clientY - hold.y) > STILL_PX) { clearTimeout(hold.timer); hold = null; pendingOff(); }
      return;
    }
    if (hold.scrollTop != null && scroller.scrollTop !== hold.scrollTop) scroller.scrollTop = hold.scrollTop;
    track(event);
  });
  stream.addEventListener("pointerup", event => { if (hold?.source === "stream" && event.pointerId === hold.id) endHold(false); });
  stream.addEventListener("pointercancel", event => {
    if (hold?.source !== "stream" || event.pointerId !== hold.id) return;
    if (hold.armed) endHold(true); else { clearTimeout(hold.timer); hold = null; }
    pendingOff();
  });
  function pendingOff() { document.documentElement.classList.remove("voice-pending"); }
  document.addEventListener("pointerup", pendingOff, true);
  // Any scroll that slips through while recording is put back.
  scroller.addEventListener("scroll", () => {
    if (hold?.armed && hold.scrollTop != null && scroller.scrollTop !== hold.scrollTop) scroller.scrollTop = hold.scrollTop;
  }, { passive: true });
  // While a hold is pending or recording: no scrolling, text selection or long-press menu.
  scroller.addEventListener("touchmove", event => { if (hold?.armed && event.cancelable) event.preventDefault(); }, { passive: false });
  document.addEventListener("selectstart", event => { if (hold) event.preventDefault(); }, true);
  stream.addEventListener("contextmenu", event => { if (hold?.source === "stream") event.preventDefault(); });
  button.addEventListener("pointermove", track);
  button.addEventListener("pointerup", event => { if (hold && event.pointerId === hold.id) endHold(false); });
  button.addEventListener("pointercancel", event => { if (hold && event.pointerId === hold.id) endHold(true); });
  button.addEventListener("contextmenu", event => event.preventDefault());
  // No long-press callout, text selection or emulated mouse/click on the microphone.
  button.addEventListener("touchstart", event => { if (event.cancelable) event.preventDefault(); }, { passive: false });
  button.addEventListener("touchend", event => { if (event.cancelable) event.preventDefault(); }, { passive: false });
  layer.querySelector(".voice-hold-send").addEventListener("click", () => commit(true));
  layer.querySelector(".voice-hold-clear").addEventListener("click", event => {
    event.stopPropagation(); if (!preview) return;
    closeHold(); render(); toast("Текст удалён, ничего не отправлено.");
  });
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
