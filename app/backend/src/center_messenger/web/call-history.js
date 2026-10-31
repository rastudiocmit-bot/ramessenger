"use strict";
window.CenterCallHistory = ({state, api, node, context, changed, sourcePath = "chats"}) => {
  const cache = new Map(); let generation = 0, timer = null, controller = null;
  const icon = name => window.CenterIcon ? window.CenterIcon(name) : node("span", "", "icon-fallback");
  const stopAudio = () => document.querySelectorAll(".call-recording audio").forEach(audio => audio.pause());
  function reset() { generation++; clearTimeout(timer); controller?.abort(); controller = null; stopAudio(); cache.clear(); }
  function entry(id) { if (!cache.has(id)) { if (cache.size >= 60) { const old = [...cache.keys()].find(key => key !== context() && !cache.get(key).busy); if (old) cache.delete(old); } cache.set(id, {calls:new Map(), more:false, oldest:null, loaded:false, busy:false, error:null, readAt:0, scope:null}); } return cache.get(id); }
  function current(id, epoch, gen) { return generation === gen && state.epoch === epoch && context() === id; }
  async function refresh(id, older = false, force = false) {
    const data = entry(id), epoch = state.epoch, gen = generation;
    if (data.busy || context() !== id || (older && (!data.more || !data.oldest))) return;
    if (!older && !force && Date.now() - data.readAt < 25000) return;
    data.busy = true; clearTimeout(timer); const abort = new AbortController(); controller = abort; data.request = abort;
    const params = new URLSearchParams({limit:"40"});
    if (older) { params.set("before_time", data.oldest.occurred_at); params.set("before_id", data.oldest.id); }
    try {
      const result = await api(`/workspaces/${state.workspace.id}/${sourcePath}/${id}/calls?${params}`, {signal:abort.signal, timeoutMs:12000});
      if (!current(id, epoch, gen) || data.request !== abort) return;
      // Each head response is authoritative for current phone membership. Drop
      // older cached pages on refresh so a changed CRM phone cannot retain audio.
      const reset = data.scope !== result.scope;
      if (reset) { stopAudio(); data.calls.clear(); data.oldest = null; }
      data.scope = result.scope;
      for (const call of result.calls) data.calls.set(call.id, call);
      if (reset || !data.loaded || older) { data.more = Boolean(result.has_more); data.oldest = result.calls[0] || null; }
      data.loaded = true; data.error = null; data.readAt = Date.now(); data.busy = false; changed(id);
    } catch (error) {
      if (current(id, epoch, gen) && data.request === abort && error.name !== "AbortError") {
        if ([401,403,404].includes(error.status)) { stopAudio(); data.calls.clear(); }
        data.error = [401,403,404].includes(error.status) ? "Звонки недоступны в этой карточке." : "Звонки не обновились. Сохранённые данные доступны; повторите загрузку."; data.busy = false; changed(id);
      }
    } finally {
      if (data.request !== abort) return;
      data.busy = false; data.request = null; if (controller === abort) controller = null;
      if (current(id, epoch, gen)) timer = setTimeout(() => refresh(id, false, true), 30000);
    }
  }
  function stopAudioIfRemoved(data, calls) {
    const present = new Set(calls.map(call => call.id));
    for (const call of data.calls.values()) if (!present.has(call.id)) {
      document.querySelector(`[data-call-id="${CSS.escape(call.id)}"] audio`)?.pause();
    }
  }
  function messages(id) { return [...entry(id).calls.values()].map(call => ({id:`call:${call.id}`, actor_type:"call", direction:call.direction, occurred_at:call.occurred_at, version:call.version, call})); }
  function controls(id) {
    const data = entry(id); if (!data.more && !data.error) return null;
    const box = node("div", undefined, "call-history-controls"); box.dataset.key = "call-controls";
    if (data.error) box.append(node("span", data.error, "call-history-note"));
    const button = node("button", data.error ? "Повторить загрузку звонков" : "Более ранние звонки", "btn sec"); button.type = "button";
    button.disabled = data.busy;
    button.addEventListener("click", () => { button.disabled = true; refresh(id, !data.error, true); }); box.append(button); return box;
  }
  function render(call, conversation) {
    const bubble = node("article", undefined, `chat-bubble call-event${call.missed ? " call-missed" : ""}`);
    bubble.dataset.callId = call.id;
    const head = node("div", undefined, "call-event-head");
    head.append(icon("phone"), node("strong", call.missed ? (call.direction === "in" ? "Пропущенный звонок" : "Исходящий · без ответа") : call.direction === "in" ? "Входящий звонок" : "Исходящий звонок"));
    const date = new Date(call.occurred_at), time = node("time", new Intl.DateTimeFormat("ru-RU", {hour:"2-digit",minute:"2-digit",timeZone:state.workspace?.timezone || "Europe/Moscow"}).format(date));
    time.dateTime = call.occurred_at; head.append(time); bubble.append(head);
    const details = node("p", undefined, "call-event-details");
    const seconds = call.talk_seconds;
    details.textContent = seconds === null ? "Длительность разговора не получена" : `Разговор: ${Math.floor(seconds/60)} мин ${seconds%60} с`;
    if (call.employees?.length) details.textContent += ` · ${call.employees.join(", ")}`;
    bubble.append(details);
    if (call.recording_available) {
      const recording = node("div", undefined, "call-recording"), play = node("button", "Прослушать запись", "btn sec"), error = node("p", "", "call-audio-error");
      play.type = "button"; error.hidden = true;
      const audio = document.createElement("audio"); audio.controls = true; audio.preload = "none"; audio.hidden = true;
      audio.setAttribute("aria-label", "Аудиозапись звонка");
      play.addEventListener("click", () => {
        error.hidden = true; stopAudio();
        if (!audio.src) audio.src = `/api/workspaces/${state.workspace.id}/${sourcePath}/${conversation}/calls/${call.id}/recording`;
        audio.hidden = false; play.hidden = true;
        const promise = audio.play(); promise?.catch(() => { if (audio.error) failed(); });
      });
      const failed = () => { audio.hidden = true; audio.removeAttribute("src"); play.hidden = false; play.textContent = "Повторить загрузку аудио"; error.textContent = "Запись пока недоступна. Её можно запросить повторно."; error.hidden = false; };
      audio.addEventListener("error", failed);
      audio.addEventListener("play", () => document.querySelectorAll(".call-recording audio").forEach(other => { if (other !== audio) other.pause(); }));
      recording.append(play, audio, error); bubble.append(recording);
    } else bubble.append(node("p", call.missed ? "Записи разговора нет" : "Запись ещё не получена", "call-recording-note"));
    return bubble;
  }
  document.addEventListener("cm:chat-selected", () => { clearTimeout(timer); controller?.abort(); for (const data of cache.values()) { data.request = null; data.busy = false; } stopAudio(); });
  return {refresh, messages, controls, render, reset, pause:stopAudio};
};
