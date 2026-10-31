"use strict";
// Device checks stay in this tab. They never call Novofon, ASR or a customer.
window.CenterPhoneDevices = ({ state, api = null }) => {
  let generation = 0, stream = null, recorder = null, timer = null, clip = null;
  let output = "", busy = false, returnFocus = null, dialog = null;
  let profilesRequest = 0, profileList = null;
  const create = (tag, text, cls) => {
    const e = document.createElement(tag); if (text) e.textContent = text;
    if (cls) e.className = cls; return e;
  };
  const button = (label, action, cls = "btn sec") => {
    const e = create("button", label, cls); e.type = "button";
    e.addEventListener("click", action); return e;
  };
  const scope = () => `${state.user?.id || ""}:${state.workspace?.id || ""}:${state.epoch}`;
  const valid = (token, context) => token === generation && context === scope() && dialog?.open;
  let player, message, capture, speaker, input, sink, heard, silent, status;
  function controls() {
    if (!dialog) return;
    capture.disabled = busy && !recorder;
    capture.textContent = recorder ? "Закончить запись" : "Записать 5 секунд";
    speaker.disabled = busy; input.disabled = busy; sink.disabled = busy;
  }
  function stopCapture() {
    clearTimeout(timer); timer = null;
    const current = recorder; recorder = null;
    if (current && current.state !== "inactive") { try { current.stop(); } catch (_) {} }
    stream?.getTracks().forEach(track => track.stop()); stream = null;
    busy = false; controls();
  }
  function clearClip() {
    if (player) { player.pause(); player.removeAttribute("src"); player.load(); player.hidden = true; }
    if (clip) URL.revokeObjectURL(clip); clip = null;
  }
  function cleanup() {
    generation++; stopCapture(); clearClip();
    if (message) message.textContent = "";
    if (heard) { heard.hidden = true; silent.hidden = true; }
  }
  function close() {
    if (dialog?.open) dialog.close();
    cleanup();
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
  }
  async function prepare(audio) {
    const wanted = output, context = scope();
    const unavailable = text => { const error = new Error(text); error.name = "AudioOutputError"; return error; };
    if (typeof audio.setSinkId !== "function") {
      if (wanted) throw unavailable("Браузер не поддерживает выбор динамика. Выберите устройство в настройках системы.");
      return;
    }
    if ((audio.sinkId || "") === wanted) return;
    try { await audio.setSinkId(wanted); }
    catch (_) { throw unavailable("Не удалось включить выбранный динамик. Откройте проверку звука и выберите доступное устройство."); }
    if (context !== scope()) {
      try { await audio.setSinkId(""); } catch (_) {}
      throw new DOMException("Session changed", "AbortError");
    }
  }
  state.prepareCallAudio = prepare;
  async function enumerate() {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const token = generation, context = scope();
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      if (!valid(token, context)) return;
      for (const [select, kind, label] of [[input, "audioinput", "Микрофон"], [sink, "audiooutput", "Динамик"]]) {
        const previous = select.value; select.replaceChildren(new Option(`${label} по умолчанию`, ""));
        let n = 0;
        for (const device of devices.filter(d => d.kind === kind && d.deviceId && !["default", "communications"].includes(d.deviceId))) {
          select.append(new Option(device.label || `${label} ${++n}`, device.deviceId));
        }
        if ([...select.options].some(o => o.value === previous)) select.value = previous;
        else if (select === sink && output) select.append(new Option("Ранее выбранное устройство — проверьте подключение", output));
        if (select === sink) select.value = output;
      }
      controls();
    } catch (_) { if (valid(token, context)) message.textContent = "Не удалось получить список устройств. Проверьте доступ в настройках браузера."; }
  }
  function tone() {
    // PCM WAV avoids relying on WebAudio routing, a provider or notification volume.
    const rate = 24000, length = rate, data = new ArrayBuffer(44 + length * 2), view = new DataView(data);
    const word = (at, s) => [...s].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
    word(0, "RIFF"); view.setUint32(4, 36 + length * 2, true); word(8, "WAVEfmt ");
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    word(36, "data"); view.setUint32(40, length * 2, true);
    for (let i = 0; i < length; i++) {
      const envelope = Math.min(1, i / 480, (length - i) / 480);
      view.setInt16(44 + i * 2, Math.round(7000 * envelope * Math.sin(2 * Math.PI * (i < rate / 2 ? 440 : 660) * i / rate)), true);
    }
    return new Blob([data], { type: "audio/wav" });
  }
  async function playClip(blob, caption, token, context) {
    if (!valid(token, context)) return;
    clearClip(); clip = URL.createObjectURL(blob); player.src = clip;
    player.hidden = false; player.muted = false; player.volume = 1; message.textContent = caption;
    try { await prepare(player); if (valid(token, context)) await player.play(); }
    catch (_) { if (valid(token, context)) message.textContent = "Не удалось запустить звук. Нажмите ▶ в плеере. Если звука нет, проверьте разрешение звука для сайта и микшер громкости системы."; }
    if (!valid(token, context)) return;
    heard.hidden = false; silent.hidden = false;
  }
  async function testSpeaker() {
    if (busy) return; cleanup();
    document.querySelectorAll(".call-recording audio").forEach(audio => audio.pause());
    const token = generation, context = scope();
    await playClip(tone(), "Должны прозвучать два коротких тона. Слышен ли сигнал?", token, context);
  }
  async function record() {
    if (recorder) { stopCapture(); return; }
    if (busy) return;
    cleanup(); const token = generation, context = scope(); busy = true; controls();
    document.querySelectorAll(".call-recording audio").forEach(audio => audio.pause());
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      busy = false; controls(); message.textContent = "Для проверки микрофона откройте сайт по HTTPS в браузере с поддержкой записи звука."; return;
    }
    message.textContent = "Разрешите доступ к микрофону в браузере.";
    try {
      const captured = await navigator.mediaDevices.getUserMedia({ audio: input.value ? { deviceId: { exact: input.value }, echoCancellation: true } : { echoCancellation: true }, video: false });
      if (!valid(token, context)) { captured.getTracks().forEach(t => t.stop()); return; }
      stream = captured;
      const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find(m => MediaRecorder.isTypeSupported?.(m));
      const current = new MediaRecorder(stream, mime ? { mimeType: mime } : {}), chunks = [];
      let size = 0;
      recorder = current;
      current.addEventListener("dataavailable", event => {
        if (!valid(token, context) || !event.data?.size) return;
        size += event.data.size; if (size <= 2 * 1024 * 1024) chunks.push(event.data);
        else stopCapture();
      });
      current.addEventListener("error", () => {
        if (!valid(token, context)) return;
        generation++; stopCapture(); message.textContent = "Запись прервана. Проверьте подключение микрофона и попробуйте ещё раз.";
      });
      current.addEventListener("stop", () => {
        if (!valid(token, context)) return;
        stopCapture();
        if (chunks.length) void playClip(new Blob(chunks, { type: current.mimeType || mime || "audio/webm" }), "Прослушайте запись. Должен быть слышен ваш голос.", token, context);
        else message.textContent = "Звук не записан. Выберите другой микрофон и повторите проверку.";
      });
      current.start(250); message.textContent = "Скажите несколько слов. Запись остановится через 5 секунд.";
      timer = setTimeout(stopCapture, 5000); controls(); void enumerate();
    } catch (error) {
      if (!valid(token, context)) return;
      stopCapture();
      message.textContent = error.name === "NotAllowedError" ? "Доступ к микрофону запрещён. Разрешите микрофон для этого сайта в настройках браузера."
        : ["NotFoundError", "OverconstrainedError"].includes(error.name) ? "Выбранный микрофон не найден. Подключите его или выберите другой."
          : "Микрофон недоступен. Проверьте устройство и закройте приложения, которые могли его занять.";
    }
  }
  function build() {
    dialog = create("dialog", null, "phone-devices-dialog"); dialog.id = "phone-devices-dialog";
    dialog.setAttribute("aria-labelledby", "phone-devices-title");
    const heading = create("header", null, "phone-devices-heading"), title = create("h2", "Звук и микрофон"); title.id = "phone-devices-title";
    const x = button("Закрыть", close, "text-button"); heading.append(title, x);
    const intro = create("p", "Проверьте звук в этой вкладке перед прослушиванием записей и телефонными разговорами.", "form-note");
    const fields = create("div", null, "phone-devices-fields");
    input = create("select"); input.setAttribute("aria-label", "Микрофон для проверки"); input.append(new Option("Микрофон по умолчанию", ""));
    sink = create("select"); sink.setAttribute("aria-label", "Устройство вывода звука"); sink.append(new Option("Динамик по умолчанию", ""));
    const micField = create("label", "Микрофон", "fld"), sinkField = create("label", "Динамик или гарнитура", "fld"); micField.append(input); sinkField.append(sink);
    const supported = typeof document.createElement("audio").setSinkId === "function";
    const outputNote = create("small", supported ? "Выбор применяется к тесту и записям звонков в этой вкладке. После выхода из кабинета сбрасывается." : "В этом браузере устройство вывода выбирается в настройках системы.");
    sink.hidden = !supported; sinkField.append(outputNote); fields.append(micField, sinkField);
    sink.addEventListener("change", async () => {
      if (busy) return; const previous = output, chosen = sink.value, context = scope(), token = generation;
      output = chosen; busy = true; controls();
      try {
        const results = await Promise.allSettled([...document.querySelectorAll(".call-recording audio"), player].map(prepare));
        const failed = results.find(r => r.status === "rejected"); if (failed) throw failed.reason;
        if (valid(token, context)) message.textContent = "Устройство вывода выбрано. Проверьте сигнал.";
      } catch (error) {
        if (!valid(token, context)) return;
        output = previous; sink.value = previous;
        await Promise.allSettled([...document.querySelectorAll(".call-recording audio"), player].map(prepare));
        if (valid(token, context)) message.textContent = error.message;
      } finally { if (valid(token, context)) { busy = false; controls(); } }
    });
    const actions = create("div", null, "btn-row");
    speaker = button("Проверить динамик", testSpeaker, "btn primary"); speaker.dataset.phoneCheck = "speaker";
    capture = button("Записать 5 секунд", record); capture.dataset.phoneCheck = "microphone";
    actions.append(speaker, capture, button("Обновить устройства", enumerate, "text-button"));
    message = create("p", "", "phone-devices-message"); message.setAttribute("role", "status"); message.setAttribute("aria-live", "polite");
    player = create("audio"); player.controls = true; player.preload = "none"; player.hidden = true; player.playsInline = true; player.setAttribute("aria-label", "Проверка звука и микрофона");
    const answers = create("div", null, "btn-row phone-devices-answers");
    heard = button("Звук слышен", () => { message.textContent = "Вы подтвердили, что звук слышен. Это проверка устройства, а не подключения к Новофону."; }, "text-button");
    silent = button("Звука нет", () => { message.textContent = "Разрешите звук для сайта в браузере. В микшере громкости Windows проверьте громкость браузера и выбранный динамик или гарнитуру. Браузер не сообщает сайту, отключён ли звук вкладки или приложения в системе."; }, "text-button");
    heard.hidden = true; silent.hidden = true; answers.append(heard, silent);
    const privacy = create("p", "Тестовая запись хранится только в памяти этой вкладки и удаляется при закрытии проверки. В Новофон и ИИ она не отправляется.", "form-note");
    status = create("p", "Разговоры через браузер ещё не подключены. Проверка устройств не подтверждает работу телефонной линии.", "phone-devices-line-status");
    const profiles = create("section", null, "phone-device-profiles"); profiles.hidden = !api;
    profileList = create("div"); profiles.append(create("h3", "Мои SIP-линии"), profileList);
    dialog.append(heading, intro, fields, actions, message, player, answers, privacy, profiles, status); document.body.append(dialog);
    dialog.addEventListener("cancel", e => { e.preventDefault(); close(); });
    dialog.addEventListener("close", () => { if (!dialog.open) cleanup(); });
    navigator.mediaDevices?.addEventListener?.("devicechange", () => { if (dialog.open) void enumerate(); });
  }
  function open() {
    if (!state.user || !state.workspace) return;
    if (!dialog) build();
    if (dialog.open) return;
    cleanup(); returnFocus = document.activeElement; dialog.showModal(); void enumerate();
    if (api) {
      const request = ++profilesRequest, context = scope(), workspace = state.workspace.id;
      const active = () => request === profilesRequest && context === scope() && dialog.open;
      profileList.replaceChildren(create("p", "Получаем назначенные линии…", "form-note"));
      api(`/workspaces/${workspace}/integrations/novofon/sip-lines/mine`, { timeoutMs: 10000 }).then(value => {
        if (!active()) return; profileList.replaceChildren();
        for (const line of value.lines || []) profileList.append(create("p", `${line.primary ? "Основная · " : ""}SIP ${line.sip_number}${line.extension ? ` · внутренний ${line.extension}` : ""} · ${line.enabled ? "Включена" : "Отключена"}`, "form-note"));
        if (!value.lines?.length) profileList.append(create("p", "Линии не назначены. Администратор добавляет их в разделе «Подключения → Новофон».", "form-note"));
        if (value.may_call === false) profileList.append(create("p", "Для вашей роли звонки недоступны.", "form-note"));
      }).catch(() => { if (active()) profileList.replaceChildren(create("p", "Не удалось получить назначенные линии. Проверка звука доступна отдельно.", "form-note")); });
    }
  }
  function reset() {
    cleanup(); output = ""; profilesRequest++; profileList?.replaceChildren();
    if (dialog?.open) dialog.close();
    if (input) { input.replaceChildren(new Option("Микрофон по умолчанию", "")); sink.replaceChildren(new Option("Динамик по умолчанию", "")); }
    returnFocus = null;
  }
  document.addEventListener("click", e => { if (e.target.closest("[data-phone-devices]")) open(); });
  window.addEventListener("pagehide", reset);
  return { open, reset, prepare };
};
