"use strict";
// Audio and retry clips live only in memory. Permission/startup/recognition belong
// to one attempt and one chat; cancelling fences even promises browsers cannot abort.
window.CenterVoice = ({ $, state, api }) => {
  const LIMIT = 30, RATE = 16000, HOLD_MS = 220, STREAM_HOLD_MS = 450;
  const CANCEL_PX = 90, EDIT_PX = { button: 34, stream: 60 }, STILL_PX = 10;
  const compose = $("#chat-compose"), input = $("#chat-input"), button = $("#chat-voice"), stream = $("#chat-stream");
  const panel = $("#chat-voice-panel"), label = $("#chat-voice-label"), time = $("#chat-voice-time");
  const level = $("#chat-voice-level"), done = $("#chat-voice-done"), cancel = $("#chat-voice-cancel"), retry = $("#chat-voice-retry");
  const clear = $("#chat-clear"), scroller = stream.closest(".canvas") || stream;
  let attempt = null, hold = null, preview = null, shared = null, generation = 0;
  let status = "idle", suppressClick = false, swallowUntil = 0;
  const supported = () => Boolean(navigator.mediaDevices?.getUserMedia && (window.AudioContext || window.webkitAudioContext));
  const available = () => Boolean(state.voiceInput && state.workspace && !input.disabled && !compose.hidden && supported()
    && (state.pane === undefined || state.pane === "messages") && !document.hidden);
  const sameScope = mine => attempt === mine && mine.generation === generation && mine.workspace === state.workspace?.id
    && mine.epoch === state.epoch;
  const current = mine => sameScope(mine) && available();
  const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  const icon = name => window.CenterIcon ? window.CenterIcon(name) : document.createElement("span");
  const layer = document.createElement("div");
  layer.className = "voice-hold"; layer.id = "voice-hold"; layer.hidden = true;
  layer.setAttribute("role", "region"); layer.setAttribute("aria-label", "Голосовое сообщение");
  layer.innerHTML = `<div class="voice-hold-stage">
    <div class="voice-hold-orb"><span class="voice-hold-ring"></span><span class="voice-hold-mic"></span></div>
    <div class="voice-hold-time">0:00</div><div class="voice-hold-hint"></div></div>
    <div class="voice-hold-card" hidden>
    <button type="button" class="voice-hold-clear" aria-label="Удалить текст, ничего не отправлять" title="Удалить"></button>
    <p class="voice-hold-text"></p><p class="voice-hold-note">Проверка перед отправкой. Касание чата отменит отправку и сохранит текст в поле.</p>
    <div class="voice-hold-bar"><i></i></div><div class="voice-hold-actions">
    <button type="button" class="btn sec voice-hold-edit">Изменить</button>
    <button type="button" class="btn primary voice-hold-send">Отправить</button></div></div>`;
  layer.querySelector(".voice-hold-mic").append(icon("mic")); layer.querySelector(".voice-hold-clear").append(icon("close"));
  document.body.append(layer);
  const stage = layer.querySelector(".voice-hold-stage"), orb = layer.querySelector(".voice-hold-orb");
  const holdTime = layer.querySelector(".voice-hold-time"), holdHint = layer.querySelector(".voice-hold-hint");
  const card = layer.querySelector(".voice-hold-card"), cardText = layer.querySelector(".voice-hold-text"), bar = layer.querySelector(".voice-hold-bar i");

  function failure(code, message) { return Object.assign(new Error(message || code), { code }); }
  function bounded(promise, ms, signal, code) {
    return new Promise((resolve, reject) => {
      let timer;
      const end = (fn, value) => { clearTimeout(timer); signal.removeEventListener("abort", abort); fn(value); };
      const abort = () => end(reject, new DOMException("Отменено", "AbortError"));
      if (signal.aborted) { abort(); return; }
      signal.addEventListener("abort", abort, { once: true });
      timer = setTimeout(() => end(reject, failure(code)), ms);
      Promise.resolve(promise).then(value => end(resolve, value), error => end(reject, error));
    });
  }
  // Called synchronously from the actual pointer/key gesture, never from a hold
  // timer. A scroll may prepare audio but never asks for microphone permission.
  function prepare() {
    try {
      if (!shared || shared.state === "closed") shared = new (window.AudioContext || window.webkitAudioContext)();
      const context = shared;
      const ready = context.state === "running" ? Promise.resolve() : Promise.resolve(context.resume());
      ready.catch(() => {});
      return { context, ready };
    } catch (error) { return { error }; }
  }
  // Touch activation is delivered on pointerup/touchend, unlike mouse down.
  // Warm audio when opening a chat too (its composer may still be hidden).
  // This never requests microphone permission or attaches a capture graph.
  function activateAudio(event) {
    if (event.key === "Escape" || !state.voiceInput || !state.workspace || document.hidden || !supported()
      || (state.pane !== undefined && state.pane !== "messages")) return;
    const prepared = prepare();
    if (hold && !attempt) hold.prepared = prepared;
  }
  for (const type of ["pointerup", "touchend", "keydown"]) document.addEventListener(type, activateAudio, { capture: true, passive: true });
  function discardContext(context) {
    if (!context) return;
    if (shared === context) shared = null;
    try { Promise.resolve(context.close()).catch(() => {}); } catch (_) { /* Already closed. */ }
  }
  function phase(kind, text = "") {
    status = kind; panel.dataset.phase = kind; label.textContent = text;
    done.hidden = kind !== "recording"; retry.hidden = kind !== "error";
    retry.textContent = attempt?.clip ? "Повторить" : "Записать";
    time.hidden = level.parentElement.hidden = kind !== "recording";
    render();
  }
  function render() {
    if (attempt && !current(attempt)) { reset(); return; }
    clear.hidden = !input.value || input.disabled;
    const can = available(), capture = status === "starting" || status === "recording", busy = Boolean(attempt);
    const empty = !input.value.trim(), phone = matchMedia("(max-width: 719px)").matches;
    button.hidden = !hold && (!can || busy || (phone && !empty));
    compose.classList.toggle("voice-swap", can && phone && empty && !busy);
    compose.classList.toggle("voice-active", capture);
    compose.classList.toggle("voice-inline", ["working", "error", "notice"].includes(status));
    compose.classList.toggle("voice-preview", Boolean(preview));
    panel.hidden = status === "idle" || status === "preview" || !layer.hidden;
  }
  function lockList(on) {
    document.documentElement.classList.toggle("voice-holding", on);
    if (on) window.getSelection?.()?.removeAllRanges();
  }
  function releaseGesture() {
    const mine = hold; hold = null;
    if (mine) {
      clearTimeout(mine.timer);
      const target = mine.source === "button" ? button : stream;
      try { if (target.hasPointerCapture?.(mine.id)) target.releasePointerCapture(mine.id); } catch (_) { /* Pointer ended. */ }
    }
    document.documentElement.classList.remove("voice-pending"); lockList(false);
    stage.classList.remove("dragging"); stage.style.transform = "";
  }
  function closeLayer() {
    if (preview) clearTimeout(preview.timer);
    preview = null; layer.hidden = true; layer.dataset.phase = ""; card.hidden = true;
    if (layer.parentNode !== document.body) document.body.append(layer);
  }
  function stopTracks(media) { media?.getTracks().forEach(track => { track.onended = null; track.onmute = null; track.stop(); }); }
  function stopCapture(mine) {
    const capture = mine?.capture; if (!capture) return null;
    mine.capture = null; clearInterval(capture.ticker);
    capture.processor.onaudioprocess = null; capture.context.onstatechange = null;
    for (const node of [capture.source, capture.processor, capture.gain]) { try { node?.disconnect(); } catch (_) { /* Detached. */ } }
    stopTracks(capture.media);
    return capture;
  }
  function report(mine, outcome, code = "none") {
    if (!mine || mine.reported || mine.workspace !== state.workspace?.id || mine.epoch !== state.epoch) return;
    mine.reported = true;
    // Strict enum/numeric metadata only; no audio, transcripts, device names or keys.
    const data = { attempt_id: mine.id, outcome, code, stage: status, audio_state: ["running", "suspended", "interrupted", "closed"].includes(mine.context?.state) ? mine.context.state : "unknown",
      elapsed_ms: Math.min(300000, Math.round(performance.now() - mine.started)),
      first_frame_ms: mine.firstFrame == null ? null : Math.min(120000, Math.round(mine.firstFrame - mine.started)),
      captured_ms: Math.min(30000, Math.round((mine.frames || 0) / (mine.rate || RATE) * 1000)) };
    api(`/workspaces/${mine.workspace}/voice/diagnostic`, { method: "POST", data, timeoutMs: 5000 }).catch(() => {});
  }
  function reset() {
    const mine = attempt; attempt = null; generation += 1;
    if (mine) {
      stopCapture(mine); mine.controller.abort();
      if (status === "starting") discardContext(mine.context);
    }
    releaseGesture(); closeLayer(); phase("idle");
  }
  function errorMessage(error) {
    const code = error?.code || error?.name;
    if (["NotAllowedError", "SecurityError"].includes(code)) return "Разрешите микрофон для этого сайта в настройках браузера.";
    if (["NotReadableError", "AbortError"].includes(code)) return "Микрофон занят или прерван. Закройте другую запись и попробуйте снова.";
    if (code === "NotFoundError") return "Микрофон не найден. Проверьте подключение.";
    if (code === "permission_timeout") return "Микрофон не включён. Проверьте разрешение и нажмите «Записать».";
    if (code === "resume_timeout" || code === "audio_interrupted") return "Браузер приостановил микрофон. Нажмите «Записать», чтобы включить его снова.";
    if (code === "no_frames") return "Звук от микрофона не поступает. Нажмите «Записать».";
    if (code === "capture_stalled") return "Запись прервалась. Запишите сообщение ещё раз.";
    if (code === "audio_short") return "Запись короче 0,3 секунды. Запишите сообщение ещё раз.";
    if (code === "timeout" || error?.name === "TimeoutError") return "Распознавание не ответило вовремя. Запись сохранена — можно повторить.";
    if (code === "network") return "Нет связи с сервисом. Запись сохранена — повторите после восстановления связи.";
    return error?.message && error.message !== code ? error.message : "Не удалось включить микрофон. Попробуйте снова.";
  }
  function fail(mine, error) {
    if (!current(mine)) return;
    stopCapture(mine); releaseGesture(); closeLayer(); mine.auto = false;
    mine.controller.abort(); report(mine, "error", diagnosticCode(error));
    if (!mine.clip) discardContext(mine.context);
    phase("error", errorMessage(error));
  }
  function diagnosticCode(error) {
    const code = error?.code || error?.name;
    if (["permission_timeout", "resume_timeout", "audio_interrupted", "no_frames", "capture_stalled", "audio_short", "network", "timeout"].includes(code)) return code;
    if (["NotAllowedError", "SecurityError"].includes(code)) return "permission_denied";
    if (code === "NotReadableError") return "microphone_busy";
    if (code === "NotFoundError") return "microphone_missing";
    if (error?.status) return "http_error";
    return "browser_error";
  }
  function holdPhase(kind) {
    layer.hidden = false; layer.dataset.phase = kind; card.hidden = kind !== "preview";
    holdHint.textContent = kind === "starting" ? "Включаем микрофон…"
      : kind === "recording" ? "Отпустите — отправим\nВверх — отмена · вниз — в поле"
      : kind === "cancel" ? "Отпустите — запись удалится" : kind === "edit" ? "Отпустите — текст в поле, без отправки" : "";
  }
  async function start(prepared = prepare()) {
    if (!available() || attempt) return;
    const mine = { id: crypto.randomUUID(), generation, workspace: state.workspace.id, epoch: state.epoch,
      controller: new AbortController(), started: performance.now(), frames: 0, rate: RATE, auto: true };
    attempt = mine; phase("starting", "Включаем микрофон…");
    if (hold?.armed) { holdTime.textContent = `0:00 / ${clock(LIMIT)}`; holdPhase("starting"); render(); }
    let media, source, processor, gain;
    try {
      if (prepared.error) throw prepared.error;
      const { context, ready } = prepared; mine.context = context;
      // Install late-stream cleanup before awaiting: getUserMedia itself cannot be aborted.
      const requested = navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      Promise.resolve(requested).then(value => { if (!current(mine) || mine.controller.signal.aborted) stopTracks(value); }, () => {});
      media = await bounded(requested, 20000, mine.controller.signal, "permission_timeout");
      if (!current(mine)) { stopTracks(media); return; }
      if (context.state !== "running") await bounded(ready, 5000, mine.controller.signal, "resume_timeout");
      if (!current(mine)) { stopTracks(media); return; }
      if (context.state !== "running") throw failure("audio_interrupted");
      source = context.createMediaStreamSource(media); processor = context.createScriptProcessor(4096, 1, 1);
      gain = context.createGain(); gain.gain.value = 0; // Keep callbacks alive without playing the microphone back.
      const capture = { media, context, source, processor, gain, chunks: [], rate: context.sampleRate, lastFrame: performance.now(), peak: 0 };
      mine.capture = capture; mine.rate = capture.rate;
      processor.onaudioprocess = event => {
        if (!current(mine) || mine.capture !== capture) return;
        const data = event.inputBuffer.getChannelData(0); if (!data.length) return;
        const now = performance.now(); capture.lastFrame = now;
        if (mine.firstFrame == null) { mine.firstFrame = now; phase("recording", "Идёт запись"); if (hold?.armed) holdPhase("recording"); render(); }
        const remaining = Math.max(0, Math.floor(LIMIT * capture.rate - mine.frames));
        const block = new Float32Array(data.subarray(0, remaining)); capture.chunks.push(block); mine.frames += block.length;
        let sum = 0; for (const value of block) sum += value * value;
        capture.peak = Math.max(capture.peak * 0.85, Math.sqrt(sum / (block.length || 1)));
        time.textContent = holdTime.textContent = `${clock(mine.frames / capture.rate)} / ${clock(LIMIT)}`;
        level.style.transform = `scaleX(${Math.min(1, capture.peak * 6).toFixed(3)})`;
        orb.style.setProperty("--level", Math.min(1, capture.peak * 6).toFixed(3));
        if (mine.frames / capture.rate >= LIMIT) finish({ auto: Boolean(hold?.armed) });
      };
      context.onstatechange = () => { if (current(mine) && mine.capture && context.state !== "running") fail(mine, failure("audio_interrupted")); };
      for (const track of media.getTracks()) track.onended = () => { if (current(mine) && mine.capture) fail(mine, failure("capture_stalled")); };
      source.connect(processor); processor.connect(gain); gain.connect(context.destination);
      capture.ticker = setInterval(() => {
        if (!current(mine)) { reset(); return; }
        if (performance.now() - capture.lastFrame > (mine.firstFrame == null ? 5000 : 2500)) fail(mine, failure(mine.firstFrame == null ? "no_frames" : "capture_stalled"));
      }, 150);
    } catch (error) {
      if (!mine.capture) {
        for (const node of [source, processor, gain]) { try { node?.disconnect(); } catch (_) { /* Detached. */ } }
        stopTracks(media);
      }
      if (current(mine) && !mine.controller.signal.aborted) fail(mine, error);
    }
  }
  function beginHold() {
    if (!hold || attempt || !available()) return;
    hold.armed = true; hold.scrollTop = scroller.scrollTop;
    navigator.vibrate?.(15); lockList(true); start(hold.prepared);
  }
  function endHold(cancelled = false) {
    const mine = hold; if (!mine) return;
    if (mine.armed) swallowUntil = performance.now() + 600;
    const toField = layer.dataset.phase === "edit", toCancel = cancelled || layer.dataset.phase === "cancel";
    releaseGesture();
    if (!mine.armed) {
      suppressClick = !cancelled && mine.source === "button";
      if (!cancelled && mine.source === "button") start(mine.prepared);
      return;
    }
    if (toCancel || attempt?.firstFrame == null) {
      const wasPreparing = !toCancel && status === "starting";
      if (attempt) report(attempt, "cancelled"); reset();
      if (wasPreparing) phase("notice", "Микрофон ещё включался. Коснитесь кнопки микрофона для записи.");
      return;
    }
    finish({ auto: !toField });
  }
  function track(event) {
    if (!hold?.armed || event.pointerId !== hold.id || status !== "recording") return;
    const dy = event.clientY - hold.y, eased = Math.sign(dy) * Math.min(170, Math.abs(dy) * 0.85);
    stage.classList.add("dragging"); stage.style.transform = `translateY(${eased.toFixed(1)}px)`;
    holdPhase(-dy > CANCEL_PX ? "cancel" : dy > EDIT_PX[hold.source] ? "edit" : "recording");
  }
  // 16 kHz, 16-bit little-endian LPCM accepted directly by SpeechKit.
  function encode(chunks, rate) {
    const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0), samples = new Float32Array(total);
    let offset = 0; for (const chunk of chunks) { samples.set(chunk, offset); offset += chunk.length; }
    const ratio = rate / RATE, length = Math.min(Math.floor(total / ratio), LIMIT * RATE), out = new Int16Array(length);
    for (let i = 0; i < length; i += 1) {
      const from = Math.floor(i * ratio), to = Math.max(from + 1, Math.floor((i + 1) * ratio));
      let sum = 0; for (let j = from; j < to && j < total; j += 1) sum += samples[j];
      const value = Math.max(-1, Math.min(1, sum / (to - from))); out[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
    }
    return new Uint8Array(out.buffer);
  }
  function base64(bytes) {
    let binary = ""; for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  }
  function insert(text, focus = false) {
    const value = text.trim(); if (!value) return;
    const before = input.value, joined = before && !/\s$/.test(before) ? ` ${value}` : value;
    const top = scroller.scrollTop;
    if (focus) input.focus({ preventScroll: true });
    let inserted = false;
    if (document.activeElement === input) {
      input.setSelectionRange(before.length, before.length);
      try { inserted = document.execCommand("insertText", false, joined); } catch (_) { /* Fallback below. */ }
    }
    if (!inserted || input.value === before) { input.value = before + joined; input.dispatchEvent(new Event("input", { bubbles: true })); }
    scroller.scrollTop = top;
  }
  function finish({ auto = false } = {}) {
    const mine = attempt; if (!mine || !current(mine) || !mine.capture) return;
    const capture = stopCapture(mine); releaseGesture(); closeLayer();
    if (mine.frames / capture.rate < 0.3) {
      if (mine.frames / capture.rate < 0.15) { report(mine, "cancelled"); reset(); }
      else fail(mine, failure("audio_short"));
      return;
    }
    mine.clip = encode(capture.chunks, capture.rate); send(mine, auto);
  }
  async function send(mine, auto = false) {
    if (!current(mine) || !mine.clip || status === "working") return;
    mine.controller = new AbortController(); mine.auto = auto;
    const draft = input.value;
    phase("working", "Распознаём запись…");
    try {
      const result = await api(`/workspaces/${mine.workspace}/voice/transcribe`, { method: "POST",
        data: { audio: base64(mine.clip), attempt_id: mine.id }, timeoutMs: 60000, signal: mine.controller.signal });
      if (!current(mine) || mine.controller.signal.aborted) return;
      mine.clip = null; report(mine, "complete");
      if (auto && mine.auto && input.value === draft && result.text && !result.warning && !result.fallback) { showPreview(mine, result.text, draft); return; }
      if (result.text) insert(result.text);
      const warning = result.warning || (!result.text ? "Речь не распознана. Попробуйте говорить ближе к микрофону." : "");
      reset(); if (warning) phase("notice", warning);
    } catch (error) { if (current(mine) && !mine.controller.signal.aborted) fail(mine, error); }
  }
  function showPreview(mine, text, draft) {
    const top = scroller.scrollTop;
    compose.prepend(layer); cardText.textContent = draft.trim() ? `${draft.trim()} ${text}` : text;
    preview = { mine, text, draft };
    holdPhase("preview"); phase("preview");
    bar.style.animation = "none"; void bar.offsetWidth; bar.style.animation = "";
    const delay = Math.round(Math.min(30000, Math.max(7000, 2500 + cardText.textContent.length * 85)));
    bar.style.animationDuration = `${delay}ms`;
    preview.timer = setTimeout(() => commit(true), delay); scroller.scrollTop = top;
  }
  function commit(sendNow, focus = false) {
    if (!preview) return;
    const { mine, text, draft } = preview;
    if (!sameScope(mine) || input.disabled || compose.hidden) { reset(); return; }
    const canSend = sendNow && available() && mine.auto && input.value === draft;
    closeLayer(); insert(text, focus); reset();
    if (canSend) compose.requestSubmit ? compose.requestSubmit() : compose.dispatchEvent(new Event("submit", { cancelable: true }));
  }
  function interruptPreview(event) {
    if (event.type === "pointerdown") swallowUntil = 0;
    if (!attempt) return;
    if (preview && !card.contains(event.target)) commit(false);
    else if (status === "working") attempt.auto = false;
  }
  document.addEventListener("pointerdown", interruptPreview, true);
  scroller.addEventListener("wheel", interruptPreview, { passive: true });
  scroller.addEventListener("touchmove", event => { interruptPreview(event); if (hold?.armed && event.cancelable) event.preventDefault(); }, { passive: false });
  scroller.addEventListener("scroll", () => {
    if (hold?.armed && scroller.scrollTop !== hold.scrollTop) scroller.scrollTop = hold.scrollTop;
    else if (preview) commit(false);
    else if (status === "working" && attempt) attempt.auto = false;
  }, { passive: true });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") { if (preview) commit(false); else if (attempt) reset(); return; }
    if (preview && !card.contains(event.target)) commit(false);
    if (status === "working" && attempt) attempt.auto = false;
  }, true);
  document.addEventListener("click", event => {
    if (event.detail && performance.now() < swallowUntil && !card.contains(event.target)) {
      swallowUntil = 0; event.preventDefault(); event.stopPropagation();
    }
  }, true);
  const busy = () => Boolean(attempt || hold || preview);
  button.addEventListener("click", event => {
    if (suppressClick || hold) { suppressClick = false; event.preventDefault(); return; }
    if (event.detail === 0) start(prepare());
  });
  button.addEventListener("pointerdown", event => {
    suppressClick = false;
    if (event.button !== 0 || event.isPrimary === false || !available() || busy()) return;
    event.preventDefault();
    hold = { id: event.pointerId, x: event.clientX, y: event.clientY, source: "button", prepared: prepare() };
    try { button.setPointerCapture(event.pointerId); } catch (_) { /* Document listeners handle release too. */ }
    hold.timer = setTimeout(beginHold, HOLD_MS);
  });
  const interactive = "button,a,input,textarea,select,label,summary,[contenteditable],[role=button]";
  stream.addEventListener("pointerdown", event => {
    if (event.pointerType !== "touch" || !event.isPrimary || !available() || busy() || event.target.closest(interactive)) return;
    hold = { id: event.pointerId, x: event.clientX, y: event.clientY, source: "stream", prepared: prepare() };
    document.documentElement.classList.add("voice-pending");
    hold.timer = setTimeout(() => {
      if (!hold || hold.id !== event.pointerId) return;
      try { stream.setPointerCapture(hold.id); } catch (_) { /* Document listeners handle release. */ }
      beginHold();
    }, STREAM_HOLD_MS);
  });
  document.addEventListener("pointermove", event => {
    if (!hold || event.pointerId !== hold.id) return;
    if (!hold.armed && hold.source === "stream" && Math.hypot(event.clientX - hold.x, event.clientY - hold.y) > STILL_PX) { releaseGesture(); return; }
    track(event);
  });
  document.addEventListener("pointerup", event => { if (hold?.id === event.pointerId) endHold(false); });
  document.addEventListener("pointercancel", event => { if (hold?.id === event.pointerId) endHold(true); });
  for (const target of [button, stream]) target.addEventListener("lostpointercapture", event => { if (hold?.id === event.pointerId) endHold(true); });
  document.addEventListener("selectstart", event => { if (hold) event.preventDefault(); }, true);
  stream.addEventListener("contextmenu", event => { if (hold?.source === "stream") event.preventDefault(); });
  button.addEventListener("contextmenu", event => event.preventDefault());
  for (const type of ["touchstart", "touchend"]) button.addEventListener(type, event => { if (event.cancelable) event.preventDefault(); }, { passive: false });
  layer.querySelector(".voice-hold-send").addEventListener("click", () => commit(true));
  layer.querySelector(".voice-hold-edit").addEventListener("click", () => commit(false, true));
  layer.querySelector(".voice-hold-clear").addEventListener("click", reset);
  done.addEventListener("click", () => finish());
  cancel.addEventListener("click", reset);
  retry.addEventListener("click", () => {
    if (status !== "error") return;
    if (attempt?.clip) send(attempt); else { reset(); start(prepare()); }
  });
  clear.addEventListener("click", () => { if (input.value) { input.value = ""; input.dispatchEvent(new Event("input", { bubbles: true })); } render(); });
  input.addEventListener("input", () => { if (preview) commit(false); if (status === "working" && attempt) attempt.auto = false; render(); });
  document.addEventListener("cm:voice-availability", render);
  document.addEventListener("cm:chat-selected", reset);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) return;
    if (preview) commit(false);
    if (attempt) report(attempt, "cancelled"); reset(); discardContext(shared);
  });
  window.addEventListener("pagehide", () => { if (preview) commit(false); reset(); discardContext(shared); });
  window.addEventListener("resize", render);
  new MutationObserver(render).observe(input, { attributes: true, attributeFilter: ["disabled"] });
  new MutationObserver(render).observe(compose, { attributes: true, attributeFilter: ["hidden"] });
  setInterval(() => { if (!document.hidden) render(); }, 400);
  render();
  return { reset, render };
};
