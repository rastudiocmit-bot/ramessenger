"use strict";
window.CenterAlfaProgress = function ({ $, state, api, show, date, onStatus = () => {} }) {
  let timer = null, generation = 0, pending = null, failures = 0, lastDetail = "", autoExpanded = false;
  let lastPayload = null, activityReads = 0, lastFullAt = 0, selectedEvent = null, feedKey = "";
  const number = value => new Intl.NumberFormat("ru-RU").format(Number(value) || 0);
  const node = (tag, value, className) => { const e = document.createElement(tag); if (value !== undefined) e.textContent = value; if (className) e.className = className; return e; };
  const crmIds = client => [...new Set((client?.cards || []).map(c => c.customer_id))].join(", ");
  const resultText = event => event.rolled_back ? "Не сохранено · повторим" : !event.committed ? "В обработке"
    : ({ imported: "Сообщение сохранено", checked: "Проверено", no_history: "Нет переписки", matched_send: "Отправка сопоставлена" })[event.result] || "Проверено";
  function clock(value) {
    if (!value || Number.isNaN(Date.parse(value))) return "—";
    return new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: state.workspace?.timezone || "Europe/Moscow" }).format(new Date(value));
  }
  function freshness(payload) {
    const now = Date.parse(payload.server_at) || Date.now(), at = Date.parse(payload.worker?.updated_at);
    return { age: Number.isFinite(at) ? Math.max(0, now - at) : Infinity,
      fresh: Boolean(payload.worker?.live && Number.isFinite(at) && now - at < 90000) };
  }
  const phaseNames = { reading:"Читаем журнал AlfaCRM", saving:"Подготавливаем пакет", templates:"Получаем шаблоны уведомлений",
    waiting_db:"Ожидаем доступ к базе", projecting:"Сохраняем новые сообщения", committing:"Подтверждаем сохранение пакета",
    repair:"Проверяем связи клиентов по ID", waiting:"Ожидаем следующую проверку", error:"Повтор после ошибки" };
  function renderEntity(payload) {
    const worker = payload.worker || {}, events = worker.recent || [], fresh = freshness(payload).fresh;
    if (selectedEvent) selectedEvent = events.find(e => e.id === selectedEvent.id) || selectedEvent;
    const previous = !worker.client && !["reading","saving","templates","waiting_db","projecting","committing"].includes(worker.phase) ? events[0] : null;
    const event = selectedEvent || previous, current = !event && fresh && payload.syncing && worker.client && ["projecting","repair"].includes(worker.phase);
    const entity = event || worker, client = event?.client || worker.client || (!worker.phase ? worker.last_client : null);
    $("#alfa-max-progress-client-label").textContent = selectedEvent ? "Выбранная операция" : current ? "Обрабатываем сейчас" : client ? "Последний обработанный клиент" : "Текущая операция";
    $("#alfa-max-progress-client-live").textContent = fresh && !event ? "Сейчас на сервере" : "Последний отчёт";
    $("#alfa-max-progress-client-live").dataset.live = String(Boolean(fresh && !event));
    $("#alfa-max-progress-client-name").textContent = client?.name || phaseNames[worker.phase] || "Ожидаем следующий проход";
    $("#alfa-max-progress-client-subtitle").textContent = client ? crmIds(client) ? "ID AlfaCRM: " + crmIds(client) : "Клиент без подтверждённой связи" : payload.next_attempt_at && worker.phase === "waiting" ? "Следующая проверка: " + date(payload.next_attempt_at) : "Страница и сообщение обрабатываются отдельными этапами";
    $("#alfa-max-progress-client-avatar").textContent = client?.name ? client.name.trim().split(/\s+/).slice(0,2).map(v=>v[0]).join("").toUpperCase() : "↻";
    let operation = event ? resultText(event) : ({save_message:"Сохраняем новое сообщение",message_checked:"Запись обработана; пакет ожидает подтверждения",check_client:"Проверяем сохранённые связи по ID",client_checked:"Связь клиента проверена",waiting:"Сервер продолжит проверку новых сообщений автоматически"})[worker.operation] || phaseNames[worker.phase] || "Получаем состояние сервера";
    if (event?.checked_messages !== undefined) operation += " · сообщений: " + number(event.checked_messages);
    if (event?.restored_ids) operation += " · восстановлено ID: " + number(event.restored_ids);
    if (event?.hidden_duplicates) operation += " · сопоставлено повторов: " + number(event.hidden_duplicates);
    $("#alfa-max-progress-client-operation").textContent = operation;
    const facts = $("#alfa-max-progress-client-facts"), values = [], branch = (payload.streams||[]).find(v=>v.branch_id===String(entity.branch_id)&&v.direction===entity.direction);
    if (entity.branch_id) values.push(["Филиал",branch?.branch_name || "Филиал " + entity.branch_id]);
    if (entity.direction) values.push(["Направление",entity.direction==="sent"?"Исходящие":"Входящие"]);
    if (entity.page != null && (!client || entity.record_id)) values.push(["Страница журнала",number(entity.page+1)]);
    if (entity.record_id) values.push(["ID записи CRM",entity.record_id]);
    if (entity.position) values.push(["Запись в пакете",number(entity.position)+(entity.position_total?" из "+number(entity.position_total):"")]);
    if (entity.occurred_at) values.push(["Время сообщения",date(entity.occurred_at)]);
    if (event?.at || worker.updated_at) values.push(["Подтверждение операции",clock(event?.at||worker.updated_at)]);
    facts.replaceChildren(...values.map(([label,value])=>{const e=node("div");e.append(node("dt",label),node("dd",value));return e;}));
    const cards=$("#alfa-max-progress-client-cards"); cards.replaceChildren(...(client?.cards||[]).map(c=>{const e=node("li");e.append(node("strong",c.name||"Карточка клиента"),node("span",`ID ${c.customer_id} · ${c.branch_name||"филиал "+c.branch_id}`));return e;}));
    show(cards,Boolean(client?.cards?.length));show($("#alfa-max-progress-client-back"),Boolean(selectedEvent));
  }
  function renderRecent(payload) {
    const events = payload.worker?.recent || [], key = JSON.stringify(events) + (selectedEvent?.id || "");
    if (key === feedKey) return; feedKey = key;
    const list = $("#alfa-max-progress-recent"), oldHeight = list.scrollHeight, oldScroll = list.scrollTop;
    const focus = document.activeElement?.dataset.event;
    $("#alfa-max-progress-recent-count").textContent = String(events.length);
    list.replaceChildren(...events.map(event => {
      const button = node("button", undefined, "sync-event"); button.type = "button"; button.dataset.event = event.id;
      button.dataset.state = event.rolled_back ? "error" : event.committed ? "done" : "pending";
      button.setAttribute("aria-pressed", String(event.id === selectedEvent?.id));
      const who = node("span", undefined, "sync-event-who"); who.append(node("strong", event.client?.name || "Клиент не указан"), node("small", crmIds(event.client) ? "ID AlfaCRM " + crmIds(event.client) : "Без ID клиента"));
      button.append(node("time", clock(event.at)), who, node("span", event.kind === "repair" ? "История клиента" : "Запись CRM " + event.record_id, "sync-event-what"), node("span", resultText(event), "sync-event-result")); return button;
    }));
    if (!events.length) list.append(node("p", "Последние операции появятся после обработки первого пакета.", "sync-event-empty"));
    if (oldScroll > 0) list.scrollTop = oldScroll + list.scrollHeight - oldHeight;
    if (focus) [...list.querySelectorAll("button")].find(b => b.dataset.event === focus)?.focus({ preventScroll: true });
  }
  function clearTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
  function reset() {
    generation += 1; clearTimer(); pending = null; failures = 0; lastDetail = ""; autoExpanded = false;
    lastPayload = null; activityReads = 0; lastFullAt = 0; selectedEvent = null; feedKey = "";
    renderEntity({}); renderRecent({});
    show($("#alfa-max-progress"), false); $("#alfa-max-progress-streams").replaceChildren();
    $("#alfa-max-progress-stats").replaceChildren(); $("#alfa-max-progress-journals").open = false;
    $("#alfa-max-progress-summary").replaceChildren();
    meter("Обход журналов", 0, 0); $("#alfa-max-progress-warning").textContent = "";
    show($("#alfa-max-progress-warning"), false); $("#alfa-max-progress-activity").textContent = "";
    $("#alfa-max-progress-detail").textContent = "";
    for (const name of ["connection", "archive", "repair"]) step(name, "waiting", "Получаем состояние");
  }
  function schedule(delay) { clearTimer(); if (document.hidden) return; timer = setTimeout(() => { timer = null; start(); }, delay); }
  function mode(value) {
    const target = $("#alfa-max-progress"); target.dataset.mode = value;
    target.classList.toggle("is-complete", value === "complete");
    target.classList.toggle("is-paused", value === "paused");
    target.classList.toggle("is-offline", value === "offline"); show(target);
    $("#alfa-max-progress-badge").textContent = ({ running: "В работе", queued: "В очереди", paused: "Пауза", error: "Ошибка", attention: "Нужна проверка", complete: "Проверено", offline: "Нет связи", idle: "Ожидание", stale: "Нет свежего отчёта" })[value];
  }
  function step(name, status, description) {
    const target = $(`[data-sync-step="${name}"]`); target.dataset.state = status;
    target.querySelector("small").textContent = description;
    const badge = target.querySelector("b"); badge.replaceChildren();
    if (window.CenterIcon) badge.append(window.CenterIcon(status === "done" ? "check" : ({ connection: "plug", archive: "clock", repair: "people" })[name]));
    else badge.textContent = status === "done" ? "✓" : ({ connection: 1, archive: 2, repair: 3 })[name];
  }
  function meter(caption, value, total) {
    $("#alfa-max-progress-caption").textContent = caption;
    $("#alfa-max-progress-value").textContent = total > 0 ? `${number(value)} из ${number(total)}` : "Считаем объём";
    const track = $("#alfa-max-progress-track"), percent = total > 0 ? Math.min(100, Math.max(0, value / total * 100)) : 0;
    if (total > 0) { track.setAttribute("aria-valuenow", String(Math.round(percent))); track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100"); }
    else { track.removeAttribute("aria-valuenow"); track.removeAttribute("aria-valuemin"); track.removeAttribute("aria-valuemax"); }
    track.setAttribute("aria-valuetext", total > 0 ? `${caption}: ${value} из ${total}` : "Общий объём пока неизвестен");
    $("#alfa-max-progress-fill").style.width = percent + "%";
  }
  function render(payload) {
    const streams=payload.streams||[], worker=payload.worker||{}, review=payload.active_review||{}, repair=payload.client_repair;
    const fresh=freshness(payload), passed=streams.filter(v=>v.complete).length, loaded=payload.history_loaded ?? Boolean(streams.length&&passed===streams.length);
    const paused=streams.filter(v=>v.paused_until), unknown=Number(payload.unidentified_rows)||0;
    const proofIssues=streams.filter(v=>v.complete&&v.verification&&v.verification.status!=="verified");
    const issue=loaded&&["history_gap","history_incomplete"].includes(payload.error_code)?"":payload.error||"";
    const active=payload.state==="active", busy=Boolean(active&&payload.syncing&&fresh.fresh&&!["waiting","error"].includes(worker.phase));
    const stale=Boolean(active&&payload.syncing&&!fresh.fresh);
    const liveErrors=streams.filter(v=>v.live_error_code);
    const reviewStale=review.state==="running"&&(!review.updated_at||Math.max(0,(Date.parse(payload.server_at)||Date.now())-Date.parse(review.updated_at))>=90000);
    const reviewing=review.state==="running"&&!reviewStale;
    const attention=Boolean(unknown||proofIssues.length||paused.length||issue||review.error||review.unresolved||reviewStale||liveErrors.length||streams.some(v=>v.missing_rows));
    const complete=Boolean(payload.history_complete&&!attention&&(!repair||repair.complete));
    const displayMode=!active?"error":stale?"stale":busy?"running":paused.length&&!payload.backfill_pending?"paused":attention?"attention":complete?"complete":payload.backfill_pending?"queued":"idle";
    mode(displayMode);
    const title=!active?"Нужно восстановить подключение":stale?"Ожидаем свежий отчёт сервера":busy?phaseNames[worker.phase]||"Получаем новые сообщения":loaded?"История в базе · получаем новые сообщения":"Первичная загрузка истории";
    $("#alfa-max-progress-text").textContent=title;
    $("#alfa-max-progress-note").textContent=loaded?"Старую переписку повторно не обходим. Действующие клиенты проверяются отдельно каждые 6 часов.":"Загружаем историю один раз. Новые сообщения принимаются параллельно, включая ответы из AlfaCRM.";
    const heads=streams.map(v=>v.new_messages_at).filter(Boolean).sort(), lastHead=heads.length===streams.length?heads[0]:null;
    step("connection",!active||liveErrors.length?"error":lastHead?"done":busy?"active":"waiting",lastHead?"Последнее чтение: "+clock(lastHead):"Ожидаем первое успешное чтение");
    step("archive",loaded?"done":busy&&worker.phase!=="repair"?"active":paused.length?"error":"waiting",`${passed} из ${streams.length} загружено`);
    step("repair",review.state==="complete"?"done":reviewing?"active":review.error||reviewStale?"error":"waiting",reviewStale?"Нет свежего отчёта":review.state==="complete"?"Проверено карточек: "+number(review.processed):reviewing?"Проверено "+number(review.processed):"Раз в 6 часов · только действующие");
    if(repair&&!repair.complete) meter("Сопоставлено клиентов",Number(repair.processed)||0,Number(repair.total)||0);
    else meter("Первичная история · журналы",passed,streams.length);
    const summary=$("#alfa-max-progress-summary");
    summary.replaceChildren(...[
      ["Новые сообщения",!active?"Требуется вход":liveErrors.length?"Проблема в журналах: "+liveErrors.length:lastHead?"Прочитаны в "+clock(lastHead):"Ожидаем подтверждение",payload.next_attempt_at?"Следующий цикл: "+clock(payload.next_attempt_at):"Все статусы клиентов"],
      ["Первичная история",`${passed} из ${streams.length} журналов загружено`,loaded?"Результат сохранён; повторного обхода нет":"Продолжаем с сохранённой позиции"],
      ["Действующие клиенты",reviewStale?"Нет свежего отчёта":reviewing?"Проверяем: "+number(review.processed):review.last_success_at?"Проверено карточек: "+number(review.processed):"Ожидает первого прохода",reviewing&&review.last_client?`${review.last_client.name||"Клиент"} · ID ${review.last_client.customer_id} · филиал ${review.last_client.branch_id}`:review.next_at?"Следующая проверка: "+date(review.next_at):"Каждые 6 часов; без старой переписки"],
      ["Требует внимания",attention?"Есть отдельные вопросы":"Нет зарегистрированных ошибок",unknown?"Без исходного ID: "+number(unknown):proofIssues.length?"Полнота прежнего импорта требует подтверждения":"Состояния журналов ниже"]
    ].map(([label,value,note])=>{const card=node("div","","sync-service-card");card.append(node("span",label),node("strong",value),node("small",note));return card;}));
    const stats=$("#alfa-max-progress-stats");stats.replaceChildren();
    for(const [key,label,value] of [["messages","Сообщений в переписках",payload.messages],["stored","Всего сохранённых записей",payload.stored_messages??payload.messages],["conversations","Диалогов",payload.conversations]]){
      const cell=node("div"),count=node("strong",value==null?"—":number(value));cell.dataset.metric=key;count.dataset.count=String(value??0);cell.append(count,node("span",label));stats.append(cell);
    }
    let movement=busy?phaseNames[worker.phase]||"Обрабатываем новые сообщения":stale?"Нет свежего подтверждения работы. Последняя операция показана выше.":payload.next_attempt_at?"Следующий проход: "+date(payload.next_attempt_at):"Ожидаем следующий серверный цикл";
    if(busy&&worker.phase==="reading"&&worker.page!=null)movement+=` · страница ${worker.page+1} · текущий пакет ${worker.completed_pages||0} из ${worker.total_pages||"—"} страниц`;
    if(worker.updated_at)movement+=". Последнее движение: "+date(worker.updated_at);
    $("#alfa-max-progress-activity").textContent=movement;
    const warnings=[];
    for(const stream of paused)warnings.push(`${stream.branch_name||"Филиал "+stream.branch_id} · ${stream.direction==="sent"?"исходящие":"входящие"}: приостановлена первичная загрузка. Повтор после ${date(stream.paused_until)}. ${stream.error||"Проверьте этот журнал."}`);
    for(const stream of liveErrors)warnings.push(`${stream.branch_name||"Филиал "+stream.branch_id} · ${stream.direction==="sent"?"исходящие":"входящие"}: не удалось проверить новые сообщения. Сервер повторит запрос.`);
    if(issue)warnings.push(issue);
    if(unknown)warnings.push(`Старых записей без подтверждённого исходного ID: ${number(unknown)}. Данные сохранены; требуется отдельное сопоставление ID.`);
    if(proofIssues.length)warnings.push("Первичный обход завершён. Доказательства полноты прежнего импорта или повторов источника требуют отдельной проверки; повтор всего архива автоматически не запускается.");
    if(stale)warnings.push("Давно нет нового отчёта о движении. Занятая серверная задача ещё не подтверждает продвижение.");
    if(reviewStale)warnings.push("Сверка действующих давно не присылала новый отчёт. Показан последний сохранённый результат; сервер возобновит работу с контрольной точки.");
    if(review.unresolved)warnings.push("Карточек с неподтверждённым текущим статусом: "+number(review.unresolved)+". История сохранена; проверьте их связи с CRM.");
    if(review.error)warnings.push("Сверка действующих временно недоступна. Сохранённые сведения доступны; следующая попытка: "+date(review.next_at));
    const warning=$("#alfa-max-progress-warning");warning.replaceChildren(...warnings.map(v=>node("p",v)));show(warning,Boolean(warnings.length));
    if(paused.length&&!autoExpanded){$("#alfa-max-progress-journals").open=true;autoExpanded=true;}
    $("#alfa-max-progress-journal-count").textContent=`${passed} из ${streams.length} загружено`;
    $("#alfa-max-progress-streams").replaceChildren(...streams.map(stream=>{
      const line=node("li"),head=node("div",undefined,"sync-stream-head"),reading=busy&&worker.phase==="reading"&&String(worker.branch_id)===stream.branch_id&&worker.direction===stream.direction;
      line.dataset.state=stream.paused_until?"paused":reading?"reading":stream.error||stream.live_error_code?"error":stream.complete?"done":"waiting";
      head.append(node("strong",stream.branch_name||"Филиал "+stream.branch_id),node("span",stream.direction==="sent"?"Исходящие":"Входящие"));
      const facts=[];
      facts.push("уникальных ID в базе: "+number(stream.source_rows));
      if(stream.expected_rows!=null)facts.push("строк по счётчику CRM: "+number(stream.expected_rows));
      if(stream.checked_page!=null)facts.push("последняя страница первичного обхода "+(stream.checked_page+1));
      if(stream.catchup_page)facts.push("догружаем новые: страница "+(stream.catchup_page+1));
      if(stream.new_messages_at)facts.push("новые проверены: "+clock(stream.new_messages_at));
      if(stream.verification?.duplicate_rows)facts.push("повторов источника: "+number(stream.verification.duplicate_rows));
      if(stream.unidentified_rows)facts.push("без исходного ID: "+number(stream.unidentified_rows));
      const detail=stream.paused_until?"Повтор после "+date(stream.paused_until):stream.live_error_code?"Новое чтение не удалось; сервер повторит запрос":stream.error|| (stream.complete?"Первичная история сохранена":"Первичная загрузка продолжается");
      line.append(head,node("p",facts.join(" · ")),node("small",detail));
      if(stream.missing_rows)line.append(node("small","Прочитанных ID без сохранённой записи: "+number(stream.missing_rows)));
      if(stream.paused_until||stream.error||stream.live_error_code){const retry=node("button","Повторить этот журнал","btn sec");retry.type="button";retry.dataset.retryBranch=stream.branch_id;retry.dataset.retryDirection=stream.direction;line.append(retry);}
      return line;
    }));
    lastDetail=`В переписках сообщений: ${payload.messages} · Всего записей в базе: ${payload.stored_messages??payload.messages} · Диалогов: ${payload.conversations}`;
    $("#alfa-max-progress-detail").textContent=(payload.last_sync_at?"Последнее чтение CRM: "+date(payload.last_sync_at):"Успешного чтения пока нет")+(payload.counters_as_of?" · Счётчики на "+date(payload.counters_as_of):"");
    renderEntity(payload);renderRecent(payload);onStatus(payload,{mode:displayMode,title});
  }
  function start() {
    if (pending) return pending;
    const workspace = state.workspace, epoch = state.epoch, token = generation;
    if (!workspace || (state.pane && state.pane !== "connections")) { reset(); return Promise.resolve(); }
    clearTimer();
    const current = () => token === generation && epoch === state.epoch && workspace.id === state.workspace?.id && (!state.pane || state.pane === "connections");
    pending = (async () => {
      try {
        const activityOnly = Boolean(lastPayload && Date.now() - lastFullAt < 15000);
        const response = await api("/workspaces/" + workspace.id + "/integrations/alfa-max/sync-progress" + (activityOnly ? "?activity_only=true" : ""), { timeoutMs: 15000 });
        if (!current()) return;
        const payload = response.activity_only ? { ...lastPayload, ...response } : response;
        activityReads = activityOnly ? activityReads + 1 : 0; if (!activityOnly) lastFullAt = Date.now();
        if (!payload.configured || payload.state === "not_configured") { onStatus(payload, { mode: "idle", title: "Сначала подключите MAX" }); reset(); return; }
        failures = 0; lastPayload = payload; render(payload);
        schedule(payload.state === "active" && payload.syncing && freshness(payload).fresh && state.connectionsView !== "settings" ? 1000 : 10000);
      } catch (error) {
        if (!current() || error.status === 401) return;
        mode("offline"); $("#alfa-max-progress-text").textContent = "Статус синхронизации временно недоступен";
        if (lastPayload) renderEntity({ ...lastPayload, syncing: false, worker: { ...lastPayload.worker, live: false } });
        $("#alfa-max-progress-note").textContent = "Показаны последние полученные данные. Ошибка просмотра статуса не означает остановку сервера.";
        if (error.status === 403) {
          $("#alfa-max-progress-detail").textContent = "Для просмотра нужны права администратора.";
          $("#alfa-max-progress-stats").replaceChildren(); $("#alfa-max-progress-streams").replaceChildren();
          $("#alfa-max-progress-summary").replaceChildren();
          lastPayload = null; selectedEvent = null; renderEntity({}); renderRecent({});
          onStatus(null, { mode: "offline", title: "Нужны права администратора" }); return;
        }
        failures += 1;
        const delay = Math.min(30000, 2000 * 2 ** Math.min(failures - 1, 4));
        $("#alfa-max-progress-detail").textContent = (lastDetail ? lastDetail + " · " : "") + "Последние полученные данные. Повторяем запрос через " + delay / 1000 + " с.";
        onStatus(null, { mode: "offline", title: "Статус временно недоступен" }); schedule(delay);
      } finally { if (token === generation) pending = null; }
    })(); return pending;
  }
  async function requestTask(button, data) {
    const workspace=state.workspace,epoch=state.epoch; if(!workspace)return;
    button.disabled=true;
    try { await api(`/workspaces/${workspace.id}/integrations/alfa-max/sync`,{method:"POST",data}); if(state.epoch===epoch){lastFullAt=0;await start();} }
    catch(error){if(state.epoch===epoch){$("#alfa-max-progress-warning").textContent=error.message;show($("#alfa-max-progress-warning"));}}
    finally {button.disabled=false;}
  }
  $("#alfa-max-progress-streams").addEventListener("click",event=>{const b=event.target.closest("[data-retry-branch]");if(b)requestTask(b,{branch_id:Number(b.dataset.retryBranch),direction:b.dataset.retryDirection});});
  $("#alfa-active-review").addEventListener("click",event=>requestTask(event.currentTarget,{review_active:true}));
  document.addEventListener("visibilitychange",()=>{if(document.hidden)clearTimer();else if(state.pane==="connections"){lastFullAt=0;start();}});
  $("#alfa-max-progress-refresh").addEventListener("click", () => {lastFullAt=0;return start();});
  $("#alfa-max-progress-client-back").addEventListener("click", () => { selectedEvent = null; if (lastPayload) { renderEntity(lastPayload); renderRecent(lastPayload); } });
  $("#alfa-max-progress-recent").addEventListener("click", event => {
    const id = event.target.closest("[data-event]")?.dataset.event;
    const entry = lastPayload?.worker?.recent?.find(e => e.id === id);
    if (entry) { selectedEvent = entry; renderEntity(lastPayload); renderRecent(lastPayload); $("#alfa-max-progress-client").scrollIntoView?.({ block: "nearest", behavior: "smooth" }); }
  });
  return { start, reset };
};
