"use strict";
window.CenterCallHistory = ({state, api, node, context, changed, sourcePath = "chats", subscriber = () => null}) => {
  const cache = new Map(); let generation = 0, timer = null, controller = null;
  const icon = name => window.CenterIcon ? window.CenterIcon(name) : node("span", "", "icon-fallback");
  const dialogs = new Set(), reads = new Set();
  let activeReads = 0; const pendingReads = [];
  function pumpReads() { while (activeReads < 2 && pendingReads.length) { const next=pendingReads.shift(); activeReads++; Promise.resolve().then(next.task).then(next.resolve,next.reject).finally(()=>{activeReads--;pumpReads();}); } }
  function scheduledRead(task) { return new Promise((resolve,reject)=>{pendingReads.push({task,resolve,reject});pumpReads();}); }
  function closeTranscripts() { for (const dialog of dialogs) { if(dialog.open) dialog.close(); dialog.remove(); } dialogs.clear(); }
  const stopAudio = () => document.querySelectorAll(".call-recording audio").forEach(audio => audio.pause());
  function reset() { generation++; clearTimeout(timer); controller?.abort(); controller = null; stopAudio(); closeTranscripts(); for(const read of reads)read.abort(); cache.clear(); }
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
      if (reset) { stopAudio(); closeTranscripts(); data.calls.clear(); data.oldest = null; data.transcripts?.clear(); data.transcriptReads?.clear(); data.aiRequests?.clear(); }
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
        if ([401,403,404].includes(error.status)) { stopAudio(); data.calls.clear(); data.scope=null; data.transcripts?.clear(); data.transcriptReads?.clear(); closeTranscripts(); }
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

  function transcriptView(bubble,initialCall,conversation) {
    const box=node('div',undefined,'call-ai-transcript'),preview=node('p','','call-transcript-preview');
    const dialog=node('dialog',undefined,'call-transcript-dialog'),heading=node('div',undefined,'call-transcript-heading'),title=node('h2','Текст телефонного разговора'),close=node('button','Закрыть','btn sec');
    const info=node('p','','call-transcript-info'),body=node('div',undefined,'call-transcript-body'),more=node('button','Загрузить ещё','btn sec'),error=node('p','','call-audio-error');
    const label=`call-transcript-${sourcePath}-${initialCall.id}`;title.id=label;dialog.id=`${label}-dialog`;const fullControl=bubble.querySelector('[data-call-action="transcript"]');fullControl.setAttribute('aria-haspopup','dialog');fullControl.setAttribute('aria-controls',dialog.id);dialog.setAttribute('aria-labelledby',label);
    close.type=more.type='button';heading.append(title,close);dialog.append(heading,info,body,more,error);box.append(preview);
    let call=initialCall,run=null,scope=entry(conversation).scope,token=0,busy=false,failed=false;
    const data=entry(conversation); data.transcripts??=new Map();data.transcriptReads??=new Map();
    const key=()=>`${call.id}:${run}`;
    const valid=(epoch,gen,version)=>current(conversation,epoch,gen)&&cache.get(conversation)===data&&data.scope===scope&&token===version;
    function status(){
      if(call.missed)return 'Без разговора';
      const ai=call.ai;
      if(!ai)return 'Расшифровка не подготовлена';
      if(ai.archive_paused)return 'Ночной архив приостановлен';
      if(ai.waiting_for_night)return 'Ожидает ночного окна 00:00–08:00 по Москве';
      return {queued:'Расшифровка в очереди',waiting_recording:'Ожидаем запись разговора',fetching:'Получаем запись для расшифровки',recognizing:'Расшифровывается…',summarizing:'Готовим описание разговора',budget_wait:'Ожидает бюджета ИИ',needs_review:'Расшифровка требует проверки',error:'Обработка приостановлена',complete:'Текст разговора пока недоступен'}[ai.phase]||'Расшифровка не подготовлена';
    }
    function draw(){
      const saved=data.transcripts.get(key()),segments=saved?.segments?.filter(s=>s.text)||[];
      preview.textContent=segments.length?segments.map(s=>s.text).join('\n').slice(0,1400):failed?'Не удалось загрузить текст. Откройте расшифровку, чтобы повторить.':saved?(saved.complete?'В записи не найдено распознанной речи':'Расшифровывается…'):run?'Читаем сохранённую расшифровку…':status();
      preview.classList.toggle('is-status',!segments.length);
      const full=bubble.querySelector('[data-call-action="transcript"]');if(full){full.disabled=!run;full.title=full.disabled?'Расшифровка ещё не подготовлена':'Показать полный текст звонка';}
      if(dialog.open){
        info.textContent=saved?.complete?'Сохранённая расшифровка · каналы обозначены без назначения ролей':'Частичная расшифровка · обработка ещё не завершена';
        const top=body.scrollTop;body.replaceChildren(...segments.map(s=>node('p',`[${Math.floor(s.start/60)}:${String(Math.floor(s.start%60)).padStart(2,'0')}] Канал ${s.channel}: ${s.text}`)));body.scrollTop=top;
        more.hidden=Boolean(saved?.done&&saved.complete);more.disabled=busy;more.textContent=failed?'Повторить загрузку':saved?.done?'Обновить расшифровку':'Загрузить ещё';
      }
    }
    async function read(extend=false){
      if(!run||busy)return;
      const version=token,epoch=state.epoch,gen=generation,readKey=key();let saved=data.transcripts.get(readKey);
      if(saved&&(!extend||(saved.done&&saved.complete))){draw();return;}
      busy=true;draw();
      try{
        let request=data.transcriptReads.get(readKey);
        if(!request){
          const after=saved?.next,requestedRun=run,callId=call.id,workspace=state.workspace.id;
          request=scheduledRead(async()=>{
            if(!valid(epoch,gen,version))return null;
            const abort=new AbortController();reads.add(abort);
            try{const params=new URLSearchParams({run:requestedRun});if(after)params.set('after',after);
              return await api(`/workspaces/${workspace}/${sourcePath}/${conversation}/calls/${callId}/transcript?${params}`,{signal:abort.signal,timeoutMs:12000});
            }finally{reads.delete(abort);}
          });data.transcriptReads.set(readKey,request);
          request.finally(()=>{if(data.transcriptReads.get(readKey)===request)data.transcriptReads.delete(readKey);}).catch(()=>{});
        }
        const value=await request;if(!value||!valid(epoch,gen,version))return;
        saved={segments:[...(saved?.next?saved.segments:[]),...value.segments],next:value.next,done:!value.next,complete:value.complete,readAt:Date.now()};
        if(data.transcripts.size>=100&&!data.transcripts.has(readKey))data.transcripts.delete(data.transcripts.keys().next().value);
        data.transcripts.set(readKey,saved);failed=false;error.textContent='';draw();
      }catch(e){if(valid(epoch,gen,version)&&e.name!=='AbortError'){failed=true;error.textContent='Не удалось загрузить текст разговора. Повторите загрузку.';draw();}}
      finally{if(token===version){busy=false;draw();}}
    }
    box.configure=value=>{
      if(scope!==data.scope){token++;busy=false;failed=false;scope=data.scope;}
      call=value;const next=call.ai?.transcript_run_id||null;
      if(next!==run){token++;busy=false;failed=false;run=next;box.dataset.transcriptRun=run||'';if(dialog.open)dialog.close();}
      const saved=data.transcripts.get(key());
      if(saved&&!saved.complete&&(call.ai?.asr_complete||Date.now()-saved.readAt>25000)){data.transcripts.delete(key());}
      draw();if(run&&!data.transcripts.has(key()))void read();
    };
    bubble.querySelector('[data-call-action="transcript"]').addEventListener('click',()=>{
      if(!run||context()!==conversation)return;
      if(!dialog.isConnected)document.body.append(dialog);dialogs.add(dialog);dialog.showModal();draw();void read();
    });
    close.addEventListener('click',()=>dialog.close());dialog.addEventListener('close',()=>bubble.querySelector('[data-call-action="transcript"]')?.focus({preventScroll:true}));
    more.addEventListener('click',()=>{const saved=data.transcripts.get(key());if(saved?.done&&!saved.complete)data.transcripts.delete(key());void read(true);});
    return box;
  }
  function updateAi(bubble,call,conversation) {
    const person=subscriber(conversation),rawName=typeof person==='string'?person.trim():'',name=/^[+\d\s().-]+$/.test(rawName)?'':rawName;
    const personNode=bubble.querySelector('.call-event-subscriber');if(personNode){personNode.textContent=name?`${call.direction==='out'?'Кому':'Абонент'}: ${name}`:'Абонент не определён';personNode.title='Имя из текущей карточки клиента';}
    const ai=call.ai,old=bubble.querySelector('.call-ai');
    const signature=JSON.stringify(ai||null);if(old?.dataset.aiKey===signature){old.querySelector('.call-ai-transcript')?.configure(call);return;}
    const box=node('section',undefined,'call-ai');box.dataset.aiKey=signature;
    for(const event of ['click','pointerdown','contextmenu'])box.addEventListener(event,e=>e.stopPropagation());
    const stateText={queued:'В очереди',waiting_recording:'Ожидаем запись',fetching:'Получаем запись',recognizing:'Распознаём разговор',summarizing:'Готовим описание',complete:'Обработка завершена',budget_wait:'Ожидаем бюджет ИИ',needs_review:'Нужна проверка',error:'Обработка приостановлена'};
    if(ai){
      const text=ai.archive_paused?'Ночной архив приостановлен':ai.waiting_for_night?'Ожидает ночного окна 00:00–08:00 по Москве':stateText[ai.phase]||'Ожидаем обработку';
      box.append(node('p',text,'call-ai-status'));
      if(ai.summary){box.append(node('small','Краткое описание'),node('p',ai.summary,'call-ai-summary'));if(ai.summary_source==='manual')box.append(node('small','Описание отредактировано сотрудником'));else if(ai.summary_source==='previous')box.append(node('small','Сохранено описание предыдущей обработки'));}
      if(ai.error_code){const errors={provider_result_unknown:'Результат запроса к ИИ не подтверждён. Автоматический повтор остановлен.',processing_interrupted:'Обработка прервалась; перед повтором проверьте возможный расход.',recording_missing:'Аудиозапись пока недоступна.',no_speech_recognized:'Речь не распознана. Прослушайте запись.',ai_budget_wait:'Достигнут месячный бюджет ИИ.',processing_not_ready:'Проверьте подключение и настройки телефонного агента.',provider_result_incomplete:'Модель вернула неполный результат. Нужна проверка.'};box.append(node('p',errors[ai.error_code]||'Обработка требует проверки. Готовые тексты сохранены.','call-audio-error'));}
    }
    const data=entry(conversation);data.transcripts??=new Map();data.aiRequests??=new Map();
    const previousTranscript=old?.querySelector('.call-ai-transcript');
    let transcript=previousTranscript;
    if(!transcript) transcript=transcriptView(bubble,call,conversation);
    box.append(transcript);transcript.configure(call);
    const management=node('details',undefined,'call-ai-management');management.append(node('summary','Подробнее о звонке'));
    const content=node('div',undefined,'call-ai-management-body');management.append(content);if(bubble.audioDiagnostic)content.append(bubble.audioDiagnostic);if(call.employees?.length)content.append(node('p',`Сотрудники в журнале: ${call.employees.join(', ')}`,'call-event-staff'));
    // Operational details and paid commands stay behind an explicit disclosure.
    for(const child of [...box.children])if(child!==transcript)content.append(child);
    box.append(management);
    const manager=['owner','admin'].includes(state.workspace?.role);
    if(manager&&!call.missed&&call.talk_seconds>0&&call.recording_available&&(!ai||['complete','error','needs_review'].includes(ai.phase))){
      const action=node('button',ai?'Повторно обработать':'Расшифровать и описать','btn sec'),error=node('p','','call-audio-error');action.type='button';
      let acknowledge=null;
      if(ai){const label=node('label',undefined,'check-label');acknowledge=document.createElement('input');acknowledge.type='checkbox';label.append(acknowledge,document.createTextNode('Подтверждаю возможный дополнительный расход ИИ'));content.append(label);action.disabled=true;acknowledge.addEventListener('change',()=>action.disabled=!acknowledge.checked);}
      content.append(action,node('small','Обработка оплачивается по тарифам выбранных ИИ-шлюзов.'),error);
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
      if(previous?.dataset.dirty==='true')content.append(previous);
      else{const edit=node('details'),title=node('summary','Исправить описание'),input=document.createElement('textarea'),save=node('button','Сохранить исправление','btn sec'),error=node('p','','call-audio-error');edit.dataset.summaryEditor='true';input.maxLength=6000;input.rows=4;input.value=ai.summary||'';input.setAttribute('aria-label','Описание разговора');save.type='button';
        input.addEventListener('input',()=>edit.dataset.dirty='true');edit.append(title,input,save,error);content.append(edit);
        save.addEventListener('click',async()=>{if(save.disabled)return;const epoch=state.epoch,gen=generation;save.disabled=true;
          try{const value=await api(`/workspaces/${state.workspace.id}/${sourcePath}/${conversation}/calls/${call.id}/summary`,{method:'PUT',data:{expected_version:ai.edit_version,summary:input.value.trim()||null}});
            if(!current(conversation,epoch,gen))return;edit.dataset.dirty='false';const record=data.calls.get(call.id)||call;data.calls.set(call.id,{...record,ai:value});updateAi(bubble,{...record,ai:value},conversation);
          }catch(e){if(current(conversation,epoch,gen))error.textContent=e.message||'Описание не сохранилось.';}finally{save.disabled=false;}});
      }
    }
    management.hidden=!content.childElementCount;
    management.open=Boolean(old?.querySelector('.call-ai-management')?.open);
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
    const outgoing=call.direction==='out';
    const bubble=node('article',undefined,`chat-bubble call-event ${outgoing?'out':'in'}${call.missed?' call-missed':''}`);
    bubble.dataset.callId=call.id;bubble.dataset.callContext=conversation;bubble.dataset.callSource=sourcePath;
    const head=node('div',undefined,'call-event-head'),label=node('div',undefined,'call-event-label'),actions=node('div',undefined,'call-event-actions');
    label.append(icon('phone'),node('strong',outgoing?'Исходящий звонок':'Входящий звонок'));
    function action(name,title){const button=node('button',undefined,'call-icon-button');button.type='button';button.title=title;button.setAttribute('aria-label',title);button.append(icon(name));return button;}
    const play=action('play','Прослушать звонок'),full=action('file','Показать полный текст звонка');play.dataset.callAction='play';full.dataset.callAction='transcript';full.disabled=true;
    actions.append(play,full);head.append(label,actions);bubble.append(head);
    const person=subscriber(conversation),rawName=typeof person==='string'?person.trim():'',name=/^[+\d\s().-]+$/.test(rawName)?'':rawName;
    bubble.append(node('p',name?`${outgoing?'Кому':'Абонент'}: ${name}`:'Абонент не определён','call-event-subscriber'));
    const details=node('div',undefined,'call-event-details'),date=new Date(call.occurred_at),time=node('time',new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit',timeZone:state.workspace?.timezone||'Europe/Moscow'}).format(date));
    time.dateTime=call.occurred_at;time.title=new Intl.DateTimeFormat('ru-RU',{dateStyle:'long',timeStyle:'short',timeZone:state.workspace?.timezone||'Europe/Moscow'}).format(date);
    const seconds=call.talk_seconds,duration=call.missed?'Без разговора':seconds===null||seconds===undefined?'Длительность неизвестна':`${seconds>=3600?`${Math.floor(seconds/3600)}:`:''}${String(Math.floor(seconds/60)%60).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
    const length=node('span',duration);length.title='Продолжительность разговора';details.append(time,node('span','·'),length);bubble.append(details);
    const recording=node('div',undefined,'call-recording'),error=node('p','','call-audio-error');error.hidden=true;
    if(call.recording_available){
      const audio=document.createElement('audio'),unmute=node('button','Включить звук','call-audio-unmute');
      audio.controls=true;audio.preload='none';audio.hidden=true;audio.playsInline=true;audio.defaultMuted=false;audio.muted=false;audio.volume=1;
      audio.setAttribute('aria-label','Аудиозапись звонка');unmute.type='button';unmute.hidden=true;error.setAttribute('role','status');
      let attempt=0,failedMessage=false;
      const diagnostic=node('details',undefined,'call-audio-diagnostic'),report=node('p');diagnostic.append(node('summary','Состояние плеера'),report);bubble.audioDiagnostic=diagnostic;
      function reportState(){if(!diagnostic.open)return;const at=Number.isFinite(audio.currentTime)?Math.floor(audio.currentTime):0;const reason={1:'воспроизведение прервано',2:'ошибка загрузки',3:'ошибка декодирования',4:'формат недоступен'}[audio.error?.code]||'нет';report.textContent=`По данным браузера\nВоспроизведение: ${audio.paused?'пауза':'идёт'}\nЗвук плеера: ${audio.muted?'выключен':'включён'}\nГромкость: ${Math.round(audio.volume*100)}%\nПозиция: ${Math.floor(at/60)}:${String(at%60).padStart(2,'0')}\nОшибка аудио: ${reason}`;}
      diagnostic.addEventListener('toggle',reportState);for(const event of ['timeupdate','volumechange','playing','pause','error'])audio.addEventListener(event,reportState);
      const epoch=state.epoch,gen=generation,callData=entry(conversation);
      const alive=()=>current(conversation,epoch,gen)&&cache.get(conversation)===callData&&bubble.isConnected;
      function playbackIcon(){
        const playing=!audio.paused&&!audio.ended;
        play.replaceChildren(icon(playing?'pause':'play'));play.title=bubble.dataset.audioState==='error'?'Повторить загрузку аудио':playing?'Приостановить звонок':'Прослушать звонок';play.setAttribute('aria-label',play.title);play.setAttribute('aria-expanded',String(!audio.hidden));
      }
      function volumeNotice(){
        const silent=audio.muted||audio.volume===0;
        unmute.hidden=!silent||audio.hidden;
        if(silent&&!audio.hidden){error.textContent='Звук плеера выключен.';error.hidden=false;}
        else if(!failedMessage){error.textContent='';error.hidden=true;}
        bubble.dataset.audioMuted=String(audio.muted);bubble.dataset.audioVolume=String(audio.volume);
      }
      function enableSound(){audio.muted=false;if(audio.volume===0)audio.volume=1;volumeNotice();}
      const failed=()=>{
        attempt++;failedMessage=true;audio.hidden=true;audio.removeAttribute('src');unmute.hidden=true;
        play.replaceChildren(icon('play'));play.title='Повторить загрузку аудио';play.setAttribute('aria-label',play.title);play.setAttribute('aria-expanded','false');
        error.textContent='Запись не удалось воспроизвести. Нажмите прослушивание ещё раз.';error.hidden=false;bubble.dataset.audioState='error';
      };
      play.addEventListener('click',()=>{
        if(!alive())return;
        if(!audio.paused&&!audio.ended&&!audio.muted&&audio.volume>0){audio.pause();return;}
        const request=++attempt;failedMessage=false;error.hidden=true;
        // This explicit user gesture enables this player only. Site and OS
        // mute settings cannot be inspected or overridden by the page.
        enableSound();document.querySelectorAll('.call-recording audio').forEach(other=>{if(other!==audio)other.pause();});
        if(!audio.src)audio.src=`/api/workspaces/${state.workspace.id}/${sourcePath}/${conversation}/calls/${call.id}/recording`;
        audio.hidden=false;play.setAttribute('aria-expanded','true');bubble.dataset.audioState='loading';
        const rejected=e=>{
          if(request!==attempt||!alive())return;
          if(audio.error){failed();return;}
          failedMessage=true;error.hidden=false;bubble.dataset.audioState='blocked';
          error.textContent=e?.name==='NotAllowedError'?'Браузер не разрешил запуск. Нажмите ▶ в самом плеере.':e?.name==='AbortError'?'Воспроизведение прервано. Нажмите ▶ ещё раз.':'Не удалось запустить плеер. Попробуйте кнопку ▶ в самом плеере.';
          playbackIcon();
        };
        try{audio.play()?.catch(rejected);}catch(e){rejected(e);}
      });
      unmute.addEventListener('click',()=>{if(alive()){failedMessage=false;enableSound();if(audio.paused)play.click();}});
      audio.addEventListener('error',()=>{if(alive())failed();});
      audio.addEventListener('volumechange',volumeNotice);
      audio.addEventListener('play',()=>{
        document.querySelectorAll('.call-recording audio').forEach(other=>{if(other!==audio)other.pause();});playbackIcon();volumeNotice();
      });
      audio.addEventListener('playing',()=>{failedMessage=false;bubble.dataset.audioState='playing';playbackIcon();volumeNotice();});
      audio.addEventListener('pause',()=>{attempt++;if(bubble.dataset.audioState!=='error')bubble.dataset.audioState='paused';playbackIcon();});
      audio.addEventListener('ended',()=>{bubble.dataset.audioState='ended';playbackIcon();});
      recording.append(audio,error,unmute);
    }else{play.disabled=true;play.title=call.missed?'Записи разговора нет':'Запись ещё не получена';recording.append(node('p',play.title,'call-recording-note'));}
    bubble.append(recording);updateAi(bubble,entry(conversation).calls.get(call.id)||call,conversation);return bubble;
  }
  document.addEventListener("cm:chat-selected", () => { clearTimeout(timer); controller?.abort(); closeTranscripts(); for(const read of reads)read.abort(); for (const data of cache.values()) { data.request = null; data.busy = false; } stopAudio(); });
  return {refresh, messages, controls, render, reset, pause:stopAudio};
};
