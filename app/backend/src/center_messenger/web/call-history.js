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
      if (reset) { stopAudio(); data.calls.clear(); data.oldest = null; data.transcripts?.clear(); data.aiRequests?.clear(); }
      data.scope = result.scope;
      for (const call of result.calls) data.calls.set(call.id, call);
      if (reset || !data.loaded || older) { data.more = Boolean(result.has_more); data.oldest = result.calls[0] || null; }
      data.loaded = true; data.error = null; data.readAt = Date.now(); data.busy = false;
      // AI status/text updates do not replace the recording player or move a
      // call in history. The parent message key contains only stable call facts.
      for (const bubble of document.querySelectorAll('.call-event[data-call-id]')) {
        if (bubble.dataset.callContext === id && bubble.dataset.callSource === sourcePath) {
          const call=data.calls.get(bubble.dataset.callId); if (call) updateAi(bubble,call,id);
        }
      }
      changed(id);
    } catch (error) {
      if (current(id, epoch, gen) && data.request === abort && error.name !== "AbortError") {
        if ([401,403,404].includes(error.status)) { stopAudio(); data.calls.clear(); data.transcripts?.clear(); }
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
  function messages(id) { return [...entry(id).calls.values()].map(value => {const call={...value};delete call.ai;delete call.transcription_status;return {id:`call:${call.id}`, actor_type:"call", direction:call.direction, occurred_at:call.occurred_at, version:call.version, call};}); }

  function updateAi(bubble,call,conversation) {
    const ai=call.ai,old=bubble.querySelector('.call-ai');
    const signature=JSON.stringify(ai||null);if(old?.dataset.aiKey===signature)return;
    const box=node('section',undefined,'call-ai');box.dataset.aiKey=signature;
    for(const event of ['click','pointerdown','contextmenu'])box.addEventListener(event,e=>e.stopPropagation());
    const stateText={queued:'В очереди',waiting_recording:'Ожидаем запись',fetching:'Получаем запись',recognizing:'Распознаём разговор',summarizing:'Готовим описание',complete:'Обработка завершена',budget_wait:'Ожидаем бюджет ИИ',needs_review:'Нужна проверка',error:'Обработка приостановлена'};
    if(ai){
      const text=ai.archive_paused?'Ночной архив приостановлен':ai.waiting_for_night?'Ожидает ночного окна 00:00–08:00 по Москве':stateText[ai.phase]||'Ожидаем обработку';
      box.append(node('p',text,'call-ai-status'));
      if(ai.summary){box.append(node('p',ai.summary,'call-ai-summary'));if(ai.summary_source==='manual')box.append(node('small','Описание отредактировано сотрудником'));else if(ai.summary_source==='previous')box.append(node('small','Сохранено описание предыдущей обработки'));}
      if(ai.error_code){const errors={provider_result_unknown:'Результат запроса к ИИ не подтверждён. Автоматический повтор остановлен.',processing_interrupted:'Обработка прервалась; перед повтором проверьте возможный расход.',recording_missing:'Аудиозапись пока недоступна.',no_speech_recognized:'Речь не распознана. Прослушайте запись.',ai_budget_wait:'Достигнут месячный бюджет ИИ.',processing_not_ready:'Проверьте подключение и настройки телефонного агента.',provider_result_incomplete:'Модель вернула неполный результат. Нужна проверка.'};box.append(node('p',errors[ai.error_code]||'Обработка требует проверки. Готовые тексты сохранены.','call-audio-error'));}
    }
    const data=entry(conversation);data.transcripts??=new Map();data.aiRequests??=new Map();
    if(ai?.transcript_run_id){
      const previous=old?.querySelector('details[data-transcript-run]');
      const savedTranscript=data.transcripts.get(ai.transcript_run_id);
      if(previous?.dataset.transcriptRun===ai.transcript_run_id&&!(ai.asr_complete&&savedTranscript&&!savedTranscript.complete))box.append(previous);
      else{
        if(ai.asr_complete&&savedTranscript&&!savedTranscript.complete)data.transcripts.delete(ai.transcript_run_id);
        const details=node('details',undefined,'call-ai-transcript'),summary=node('summary','Полная расшифровка'),body=node('div'),more=node('button','Загрузить ещё','btn sec'),error=node('p','','call-audio-error');
        details.dataset.transcriptRun=ai.transcript_run_id;more.type='button';more.hidden=true;details.append(summary,body,more,error);
        let busy=false;const run=ai.transcript_run_id;
        async function read(){
          if(busy)return;const epoch=state.epoch,gen=generation;let saved=data.transcripts.get(run);
          if(saved?.done){draw(saved);return;}busy=true;more.disabled=true;
          try{const params=new URLSearchParams({run});if(saved?.next)params.set('after',saved.next);
            const value=await api(`/workspaces/${state.workspace.id}/${sourcePath}/${conversation}/calls/${call.id}/transcript?${params}`);
            if(!current(conversation,epoch,gen))return;
            saved={segments:[...(saved?.segments||[]),...value.segments],next:value.next,done:!value.next,complete:value.complete};data.transcripts.set(run,saved);draw(saved);error.textContent='';
          }catch(e){if(current(conversation,epoch,gen)){error.textContent=e.message||'Не удалось прочитать расшифровку.';more.hidden=false;more.textContent='Повторить загрузку';}}
          finally{busy=false;more.disabled=false;}
        }
        function draw(value){const offset=body.scrollTop||previous?.querySelector('div')?.scrollTop||0;body.replaceChildren(...value.segments.filter(s=>s.text).map(s=>node('p',`[${Math.floor(s.start/60)}:${String(s.start%60).padStart(2,'0')}] Канал ${s.channel}: ${s.text}`)));body.scrollTop=offset;more.hidden=value.done&&value.complete;more.textContent=value.done&&!value.complete?'Обновить расшифровку':'Загрузить ещё';summary.textContent=value.complete?'Полная расшифровка':'Расшифровка · пока частичная';}
        details.addEventListener('toggle',()=>{if(details.open){const saved=data.transcripts.get(run);if(saved)draw(saved);else void read();}});more.addEventListener('click',()=>{const saved=data.transcripts.get(run);if(saved?.done&&!saved.complete)data.transcripts.delete(run);void read();});box.append(details);if(previous?.open)details.open=true;
      }
    }
    const manager=['owner','admin'].includes(state.workspace?.role);
    if(manager&&!call.missed&&call.talk_seconds>0&&call.recording_available&&(!ai||['complete','error','needs_review'].includes(ai.phase))){
      const action=node('button',ai?'Повторно обработать':'Расшифровать и описать','btn sec'),error=node('p','','call-audio-error');action.type='button';
      let acknowledge=null;
      if(ai){const label=node('label',undefined,'check-label');acknowledge=document.createElement('input');acknowledge.type='checkbox';label.append(acknowledge,document.createTextNode('Подтверждаю возможный дополнительный расход ИИ'));box.append(label);action.disabled=true;acknowledge.addEventListener('change',()=>action.disabled=!acknowledge.checked);}
      box.append(action,node('small','Обработка оплачивается по тарифам выбранных ИИ-шлюзов.'),error);
      action.addEventListener('click',async()=>{
        if(action.disabled)return;const epoch=state.epoch,gen=generation;action.disabled=true;
        let payload=data.aiRequests.get(call.id);
        if(!payload){payload={request_id:crypto.randomUUID(),reprocess:Boolean(ai),expected_run:ai?.run_id||null,acknowledge_cost:Boolean(acknowledge?.checked)};data.aiRequests.set(call.id,payload);}
        try{const value=await api(`/workspaces/${state.workspace.id}/${sourcePath}/${conversation}/calls/${call.id}/ai`,{method:'POST',data:payload});
          if(!current(conversation,epoch,gen))return;data.aiRequests.delete(call.id);const record=data.calls.get(call.id)||call;data.calls.set(call.id,{...record,ai:value});updateAi(bubble,{...record,ai:value},conversation);void refresh(conversation,false,true);
        }catch(e){if(current(conversation,epoch,gen)){error.textContent=e.message||'Не удалось поставить звонок в очередь.';action.disabled=Boolean(ai&&!acknowledge?.checked);}}
      });
    }
    if(ai&&state.workspace?.role!=='observer'&&['owner','admin','operator'].includes(state.workspace?.role)){
      const previous=old?.querySelector('details[data-summary-editor]');
      if(previous?.dataset.dirty==='true')box.append(previous);
      else{const edit=node('details'),title=node('summary','Исправить описание'),input=document.createElement('textarea'),save=node('button','Сохранить исправление','btn sec'),error=node('p','','call-audio-error');edit.dataset.summaryEditor='true';input.maxLength=6000;input.rows=4;input.value=ai.summary||'';input.setAttribute('aria-label','Описание разговора');save.type='button';
        input.addEventListener('input',()=>edit.dataset.dirty='true');edit.append(title,input,save,error);box.append(edit);
        save.addEventListener('click',async()=>{if(save.disabled)return;const epoch=state.epoch,gen=generation;save.disabled=true;
          try{const value=await api(`/workspaces/${state.workspace.id}/${sourcePath}/${conversation}/calls/${call.id}/summary`,{method:'PUT',data:{expected_version:ai.edit_version,summary:input.value.trim()||null}});
            if(!current(conversation,epoch,gen))return;edit.dataset.dirty='false';const record=data.calls.get(call.id)||call;data.calls.set(call.id,{...record,ai:value});updateAi(bubble,{...record,ai:value},conversation);
          }catch(e){if(current(conversation,epoch,gen))error.textContent=e.message||'Описание не сохранилось.';}finally{save.disabled=false;}});
      }
    }
    if(!box.childElementCount){old?.remove();return;}
    if(old)old.replaceWith(box);else bubble.append(box);
  }
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
    bubble.dataset.callContext=conversation;bubble.dataset.callSource=sourcePath;
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
    updateAi(bubble,entry(conversation).calls.get(call.id)||call,conversation);
    return bubble;
  }
  document.addEventListener("cm:chat-selected", () => { clearTimeout(timer); controller?.abort(); for (const data of cache.values()) { data.request = null; data.busy = false; } stopAudio(); });
  return {refresh, messages, controls, render, reset, pause:stopAudio};
};
