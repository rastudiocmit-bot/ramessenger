"use strict";
// Voice input in the chat composer. The browser takes raw sound from the
// microphone and turns it into 16 kHz, 16-bit, mono LPCM — the format Yandex
// SpeechKit accepts as is — so no converter is needed on the server. Audio
// stays in memory until the text arrives or the operator cancels; nothing is
// sent to a client: the text lands in the message field for the operator to send.
window.CenterVoice = ({ $, state, api, toast }) => {
  const LIMIT = 30, RATE = 16000;
  const compose = $("#chat-compose"), input = $("#chat-input"), button = $("#chat-voice");
  const panel = $("#chat-voice-panel"), label = $("#chat-voice-label"), time = $("#chat-voice-time");
  const level = $("#chat-voice-level"), done = $("#chat-voice-done"), cancel = $("#chat-voice-cancel"), retry = $("#chat-voice-retry");
  let recording = null, pending = null, failedAudio = null, run = 0;

  const phone = () => matchMedia("(max-width: 719px)").matches;
  const supported = () => Boolean(navigator.mediaDevices?.getUserMedia && (window.AudioContext || window.webkitAudioContext));
  const available = () => Boolean(state.voiceInput) && state.workspace && !input.disabled && !compose.hidden && supported();

  function render() {
    const can = available(), busy = Boolean(recording || pending || failedAudio);
    // On a phone one round button: the microphone while the field is empty, "send" once there is text.
    const empty = !input.value.trim();
    button.hidden = !can || busy || (phone() && !empty);
    compose.classList.toggle("voice-swap", can && phone() && empty && !busy);
    compose.classList.toggle("voice-active", busy);
    panel.hidden = !busy;
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
    context.close().catch(() => {});
    return { chunks, rate };
  }
  function reset() {
    stopCapture(); run += 1; pending = null; failedAudio = null; render();
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
    const Context = window.AudioContext || window.webkitAudioContext, context = new Context();
    try { await context.resume(); } catch (_) { /* Resumed on the next gesture. */ }
    const source = context.createMediaStreamSource(stream), processor = context.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    let frames = 0, peak = 0;
    processor.onaudioprocess = event => {
      const data = event.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(data)); frames += data.length;
      let sum = 0; for (let i = 0; i < data.length; i += 1) sum += data[i] * data[i];
      peak = Math.max(peak * 0.85, Math.sqrt(sum / data.length));
      if (frames / context.sampleRate >= LIMIT) finish();
    };
    source.connect(processor); processor.connect(context.destination);
    const started = performance.now();
    recording = { stream, context, source, processor, chunks, rate: context.sampleRate, started,
      ticker: setInterval(() => {
        if (!available()) { reset(); return; }
        time.textContent = `${clock((performance.now() - started) / 1000)} / ${clock(LIMIT)}`;
        level.style.transform = `scaleX(${Math.min(1, peak * 6).toFixed(3)})`;
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

  async function send(audio) {
    const mine = ++run;
    pending = audio; failedAudio = null; phase("working", "Распознаём речь и улучшаем текст…"); render();
    try {
      const result = await api(`/workspaces/${state.workspace.id}/voice/transcribe`, { method: "POST", data: { audio: base64(audio) }, timeoutMs: 35000 });
      if (mine !== run) return;
      pending = null; render();
      if (result.text) insert(result.text);
      if (result.warning) toast(result.warning, !result.text);
    } catch (error) {
      if (mine !== run || error.name === "AbortError") return;
      pending = null; failedAudio = audio;
      phase("error", error.message || "Не удалось распознать запись."); render();
    }
  }
  function finish() {
    const captured = stopCapture(); if (!captured) return;
    const audio = encode(captured.chunks, captured.rate);
    if (audio.length < RATE * 2 * 0.3) { render(); toast("Запись слишком короткая.", true); return; }
    send(audio);
  }

  button.addEventListener("click", start);
  done.addEventListener("click", finish);
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
