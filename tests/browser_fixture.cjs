// Synthetic data only. Every request is intercepted; no real CRM or MAX delivery.
const fs = require('node:fs');
const path = require('node:path');
const web = path.join(__dirname, '../app/backend/src/center_messenger/web');
async function fixture(context, role = 'owner', sections = ['overview', 'messages', 'tasks', 'security'], options = {}) {
  const calls = [], mutations = [], errors = [], unexpected = [];
  const tasks = { items: (options.tasks || []).map(t => ({ version: 1, status: 'open', sources: [], details: '', assignee_id: 'test', assignee_name: 'Олег Иванов', created_by: 'test', ...t })),
    studio: { day_start: '10:00', day_end: '20:00', work_days: [1, 2, 3, 4, 5, 6], default_due: 'end_of_day', visibility: 'all', alfa_copy: false },
    personal: { press: 'split', voice_task: 'instant', reminders: true, hints: true, digest: false }, voice: [] };
  const preferences = { name_order: "parent_first" };
  const notificationPreferences = {configured:true,sound_enabled:false,volume:55,client:'soft',lead:'clear',archive:'low',service_popups:false,task_sound:false};
  const cardLoader = { preload_unanswered: true, preload_active_groups: true,
    finance_interval_minutes: 30, lessons_interval_minutes: 30, groups_interval_minutes: 360,
    history_interval_minutes: 0, requests_per_turn: 3, busy_retry_seconds: 5,
    retry_initial_seconds: 30, retry_max_seconds: 1800, manual_priority_seconds: 30,
    manual_interest_minutes: 10, manual_burst: 5, parallel_card_jobs: 1, host_request_gap_ms: 350 };
  // Voice input and AI agents (Connections → service settings). Off in the chat unless asked.
  const ai = { settings: { configured: Boolean(options.voice), folder_id: options.voice ? 'b1gexamplefolder0000' : '', key_hint: options.voice ? 'k3y9' : '',
      voice_enabled: Boolean(options.voice), voice_max_seconds: 180, monthly_limit_rub: null, status: options.voice ? 'active' : 'not_configured', error: null, verified_at: null,
      month_spent_rub: 0, vault_ready: true, models: [{ id: 'yandexgpt-5.1', title: 'YandexGPT Pro 5.1', provider: 'yandex', provider_title: 'Yandex Cloud' },
        { id: 'aliceai-llm', title: 'Alice AI LLM', provider: 'yandex', provider_title: 'Yandex Cloud' },
        { id: 'deepseek-flash', title: 'DeepSeek V4.1 Flash', provider: 'deepseek', provider_title: 'DeepSeek' },
        { id: 'deepseek-v4-pro', title: 'DeepSeek V4 Pro', provider: 'deepseek', provider_title: 'DeepSeek' },
        { id: 'mimo-v2.6-flash', title: 'MiMo V2.6 Flash', provider: 'mimo', provider_title: 'MiMo' },
        { id: 'mimo-plan-v2.6-pro', title: 'MiMo V2.6 Pro · Token Plan', provider: 'mimo_plan', provider_title: 'MiMo Token Plan' }],
      deepseek: { configured: false, key_hint: '', status: 'not_configured', error: null },
      gateways: {recognition_provider:'yandex',recognition_language:'ru',synthesis_provider:'disabled',synthesis_voice:'Mia',synthesis_voices:['Mia','Chloe'],mimo:{configured:false},mimo_plan:{configured:false}},
      tasks: [{ id: 'voice_input', title: 'Голосовой ввод' },{ id:'text_processing', title:'Обработка текста' }], default_model: 'yandexgpt-5.1', default_prompt: 'Нормализуй текст, убери слова-паразиты.' },
    agents: [], transcribe: [], failNext: 0, fallbackNext: 0 };
  context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
  const conversations = Array.from({ length: 40 }, (_, n) => ({ id: 'client-' + n,
    name: n === 0 ? 'Анна Смирнова' : 'Клиент ' + String(n + 1).padStart(2, '0'), students: [n === 0 ? 'Миша, 6 лет' : 'Ученик ' + (n + 1)],
    status: n % 2 ? 'lead' : 'client', channel_name: 'MAX через AlfaCRM', contact_id: 'contact-' + n,
    unread: n < 3, unread_count: n < 3 ? n + 1 : 0, can_send: role !== 'observer', revision: 1, inbound_seq: 3,
    last_message_at: '2026-10-24T11:00:00Z', waiting_since: '2026-10-24T11:00:00Z', preview: 'Подскажите, пожалуйста, расписание занятий', last_direction: 'in' }));
  const messages = [
    { id: 'm1', text: 'Здравствуйте! Подскажите, есть ли места на подготовку к школе для Миши?', direction: 'in', actor_type: 'contact', state: 'received' },
    { id: 'm2', text: 'Анна, добрый день! Да, места есть. Можем предложить вторник и четверг в 17:00.', direction: 'out', actor_type: 'human', state: 'read' },
    { id: 'm3', text: 'Напоминаем о занятии сегодня в 17:00. Будем рады видеть Мишу!', direction: 'out', actor_type: 'automation', template_name: 'Напоминание о занятии', state: 'accepted' },
    { id: 'm4', text: 'Спасибо! Запишите нас, пожалуйста, на пробное занятие.', direction: 'in', actor_type: 'contact', state: 'received' },
    ...(options.failedSend ? [{ id: 'm5', text: 'Записали Мишу на четверг в 17:00.', direction: 'out', actor_type: 'human', state: 'failed',
      error: 'AlfaCRM не вернула форму отправки MAX для этой карточки.' }] : []),
    ...(options.failedTwice ? [{ id: 'm6', text: 'Записали Мишу на четверг в 17:00.', direction: 'out', actor_type: 'human', state: 'failed',
      error: 'AlfaCRM не вернула форму отправки MAX для этой карточки.' }] : [])
  ].map((m, n) => ({ ...m, occurred_at: `2026-10-24T10:0${n}:00Z`, customer_branch_id: '1',
    sort_time: String(1792836000000000 + n * 60000000), crm_order: String(n + 1), version: 1, attachments: [] }));
  const contacts = conversations.slice(0, 9).map((c, n) => ({ id: c.contact_id, name: c.name, status: c.status,
    students: c.students, student_count: 1, groups: ['Подготовка к школе'], group_count: 1,
    max_connected: true, max_bot_connected: true, needs_review: false }));
  const family = { id: 'contact-0', name: conversations[0].name, phones: ['+7 900 000-00-00'], emails: [], today: '2026-10-24',
    max_connected: true, max_bot_connected: true, students: [{ id: 'student-0', name: 'Миша Смирнов', dob: '2020-05-14', balance: 2400,
      groups: [], cards: [{ id: '100', branch_id: '1', branch_name: 'Центральный', status: 'client', needs_refresh: false, subscriptions: [], url: 'https://example.s20.online/customer/view?id=100' }] }] };
  const members = [{ user_id: 'test', display_name: 'Олег Иванов', email: 'demo@example.test', role },
    { user_id: 'operator-1', display_name: 'Мария Петрова', email: 'operator@example.test', role: 'operator', sections: ['messages', 'tasks', 'security'] }];
  const branches = [{ id: '1', name: 'Центральный' }, { id: '2', name: 'Северный' }];
  const channels = [{ id: 'native', name: 'MAX через AlfaCRM', kind: 'alfa_max_browser', enabled: true, status: 'active' }];
  const metrics = { incoming: 268, previous_incoming: 240, waiting: 3, average_response_seconds: 240, new_leads: 18,
    days: [24, 43, 36, 48, 41, 46, 30].map((incoming, n) => ({ label: ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'][n], incoming })),
    branches: [{ name: 'Центральный', incoming: 164 }, { name: 'Северный', incoming: 104 }],
    operators: [{ name: 'Мария Петрова', answered: 73, average_response_seconds: 210 }, { name: 'Олег Иванов', answered: 51, average_response_seconds: 270 }] };
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname, method = request.method();
    if (url.hostname !== (options.hostname || 'cabinet.example.test')) return route.abort();
    if (p === '/') return route.fulfill({ headers:options.csp?{'Content-Security-Policy':options.csp}:{}, contentType: 'text/html', body: fs.readFileSync(web + '/index.html') });
    if (p.startsWith('/assets/')) {
      const file = path.join(web, path.basename(p));
      if (!fs.existsSync(file)) { unexpected.push(p); return route.abort(); }
      const mime = { '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.png': 'image/png', '.jpg': 'image/jpeg' }[path.extname(file)];
      return route.fulfill({ contentType: mime, body: fs.readFileSync(file) });
    }
    const entry = { method, path: p, query: url.search, body: request.postDataJSON(), csrf: request.headers()['x-csrf-token'] }; calls.push(entry);
    if (method !== 'GET') mutations.push(entry);
    let payload;
    if (p === '/api/auth/status') payload = { setup_required: false };
    else if (p === '/api/auth/session') payload = { user: { id: 'test', display_name: 'Олег Иванов', email: 'demo@example.test' }, csrf: 'synthetic-csrf',chat_protocol:options.protocol||1,
      workspaces: [{ id: 'test', name: 'Развивайся', role, timezone: 'Europe/Moscow', sections }] };
    else if (p === '/api/auth/sessions') payload = { sessions: [{ id: 'session', current: true, device_label: 'Chrome · Linux', last_seen_at: '2026-10-24T11:00:00Z' }] };
    else if (p === '/api/auth/logout') payload = {};
    else if (p.endsWith('/notification-preferences')) {
      if(method==='PATCH'){
        Object.assign(notificationPreferences,entry.body);
        if('task_reminders' in entry.body)tasks.personal.reminders=entry.body.task_reminders;
      }
      payload={...notificationPreferences,task_reminders:tasks.personal.reminders};
    }
    else if(p.endsWith('/notification-status'))payload={crm:{configured:true,status:'active'},max:{configured:true,state:'active',recovery:{phase:'active'}}};
    else if (p.includes('/tasks')) {
      // Operator tasks (tasks.js): an in-memory list with the server's shapes.
      const now = Date.now(), iso = ms => new Date(ms).toISOString();
      const findTask = () => tasks.items.find(t => p.includes('/tasks/' + t.id));
      if (p.endsWith('/tasks/options')) payload = { me: 'test', admin: ['owner', 'admin'].includes(role), read_only: role === 'observer', timezone: 'Europe/Moscow',
        studio: tasks.studio, personal: tasks.personal, kinds: [], assignees: [{ id: 'test', name: 'Олег Иванов' }].concat(['owner', 'admin'].includes(role) ? [{ id: 'operator-1', name: 'Мария Петрова' }] : []),
        presets: { hour: iso(now + 3600e3), two_hours: iso(now + 7200e3), end_of_day: iso(now + 5 * 3600e3), tomorrow_morning: iso(now + 20 * 3600e3), default: iso(now + 5 * 3600e3), default_kind: 'end_of_day' } };
      else if (p.endsWith('/tasks/voice')) {
        const b = entry.body, text = b.text.replace(/^\s*(задача|напомни)[:,\s]*/i, ''), title = text.charAt(0).toUpperCase() + text.slice(1);
        const draft = { title, due_at: iso(now + 20 * 3600e3), kind: null, assignee_id: 'test', conversation_id: b.conversation_id, client_label: b.client_label,
          sources: [{ id: 'm4', text: messages.find(m => m.id === 'm4')?.text || '', direction: 'in' }] };
        tasks.voice.push(b);
        if (!b.create || title.split(/\s+/).length < 3) payload = { draft };
        else { const task = { id: 'task-' + (tasks.items.length + 1), status: 'open', origin: 'voice', created_by: 'test', assignee_name: 'Олег Иванов', details: '', version: 1, completed_at: null, ...draft };
          tasks.items.push(task); payload = { task }; }
      }
      else if (p.endsWith('/tasks/draft')) payload = options.taskAi ? { ai: true, title: 'Записать Мишу на пробное занятие, отправить ссылку на оплату', details: '— Записать Мишу на пробное в четверг\n— Отправить ссылку на оплату', kind: 'group', due_at: iso(now + 22 * 3600e3) } : { ai: false };
      else if (p.endsWith('/tasks/hint')) payload = /уточн|провер|напиш/i.test(entry.body.text) ? { hint: { title: 'Уточнить расписание для Миши', kind: 'group', due_at: iso(now + 4 * 3600e3),
        sources: [{ id: 'm4', text: messages.find(m => m.id === 'm4')?.text || '', direction: 'in' }], ai: true } } : { hint: null };
      else if (/\/tasks\/[^/]+\/reply$/.test(p)) payload = { text: 'Анна, записали Мишу на пробное занятие в четверг в 17:00.' };
      else if (p.endsWith('/tasks/settings')) { Object.assign(tasks.studio, entry.body); payload = tasks.studio; }
      else if (p.endsWith('/tasks/preferences')) { Object.assign(tasks.personal, entry.body); payload = tasks.personal; }
      else if (p.endsWith('/tasks/summary')) {
        const open = tasks.items.filter(t => t.status === 'open'), mine = open.filter(t => t.assignee_id === 'test');
        const overdue = mine.filter(t => Date.parse(t.due_at) < now).length, today = mine.filter(t => Date.parse(t.due_at) >= now && Date.parse(t.due_at) < now + 12 * 3600e3).length; // tests run at any hour
        const conversations = {}; for (const t of open) if (t.conversation_id) { const c = conversations[t.conversation_id] ||= { count: 0, due_at: t.due_at }; c.count += 1; if (t.due_at < c.due_at) c.due_at = t.due_at; }
        payload = { now: iso(now), overdue, today, open: mine.length, badge: overdue + today, conversations, upcoming: mine.filter(t => Date.parse(t.due_at) < now + 26 * 3600e3), studio: tasks.studio };
      } else if (/\/tasks\/[^/]+\/(complete|cancel|reopen|snooze)$/.test(p)) {
        const task = findTask(), verb = p.split('/').pop();
        if (verb === 'complete') Object.assign(task, { status: 'done', completed_at: iso(now) });
        if (verb === 'cancel') Object.assign(task, { status: 'cancelled', completed_at: iso(now) });
        if (verb === 'reopen') Object.assign(task, { status: 'open', completed_at: null });
        if (verb === 'snooze') Object.assign(task, { status: 'open', due_at: entry.body.due_at, completed_at: null });
        task.version += 1; payload = task;
      } else if (/\/tasks\/[^/]+$/.test(p) && method === 'PATCH') { const task = findTask(); Object.assign(task, entry.body); task.version += 1; payload = task; }
      else if (/\/tasks\/[^/]+$/.test(p) && method === 'GET') payload = { task: findTask(), events: [] };
      else if (p.endsWith('/tasks') && method === 'POST') {
        const b = entry.body, task = { id: 'task-' + (tasks.items.length + 1), conversation_id: b.conversation_id, client_label: b.client_label, title: b.title, details: b.details || '',
          kind: b.kind, status: 'open', due_at: b.due_at, origin: b.origin, created_by: 'test', assignee_id: b.assignee_id || 'test', assignee_name: 'Олег Иванов', completed_at: null, version: 1,
          sources: (b.message_ids || []).map(id => { const m = messages.find(x => x.id === id); return { id, text: m?.text || '', direction: m?.direction || 'in', at: m?.occurred_at }; }) };
        tasks.items.push(task); payload = task;
      } else if (p.endsWith('/tasks')) {
        const conversation = url.searchParams.get('conversation'), status = url.searchParams.get('status') || 'open', scope = url.searchParams.get('scope') || 'mine';
        payload = { tasks: tasks.items.filter(t => conversation ? t.conversation_id === conversation : (status === 'open' ? t.status === 'open' : t.status !== 'open') && (scope === 'all' || t.assignee_id === 'test'))
          .sort((a, b) => a.due_at.localeCompare(b.due_at)) };
      } else { unexpected.push(method + ' ' + p); return route.fulfill({ status: 501, contentType: 'application/json', body: '{"detail":"Unmocked request"}' }); }
    }
    else if (p.endsWith('/chats/context')) payload = { preferences, channels, branches, can_write: role !== 'observer', send_attachments: false,
      voice_input: Boolean(options.voice) && role !== 'observer' };
    else if (p.endsWith('/chats/bootstrap')) payload = {context:{version:'1:2026-10-26',preferences,channels,branches,can_write:role!=='observer',voice_input:Boolean(options.voice)&&role!=='observer'},
      listing:{version:'1:2026-10-26',conversations:conversations.slice(0,90),total:conversations.length,unread:3,page:0,has_more:conversations.length>90},
      history:conversations.length?{conversation:conversations[0],messages,next_seq:messages.length,total:messages.length,has_more:false,more_changes:false}:null};
    else if (p.endsWith('/chats/preferences')) { Object.assign(preferences, entry.body); payload=preferences; }
    else if (p.endsWith('/ai/settings')) {
      if (method === 'PUT') {
        const oldFolder=ai.settings.folder_id;
        for (const key of ['voice_max_seconds','folder_id','voice_enabled','monthly_limit_rub'])
          if (key in entry.body) ai.settings[key]=entry.body[key];
        if (entry.body.clear_key) ai.settings.key_hint='';
        else if (entry.body.api_key) ai.settings.key_hint=entry.body.api_key.slice(-4);
        if (entry.body.api_key || entry.body.clear_key || oldFolder!==ai.settings.folder_id) ai.settings.status='not_configured';
        ai.settings.configured=Boolean(ai.settings.folder_id && ai.settings.key_hint);
      }
      payload = ai.settings;
    } else if (p.endsWith('/ai/deepseek')) {
      ai.settings.deepseek = entry.body.clear_key ? { configured: false, key_hint: '', status: 'not_configured', error: null }
        : { configured: true, key_hint: entry.body.api_key.slice(-4), status: 'not_configured', error: null };
      payload = ai.settings;
    } else if (p.endsWith('/ai/deepseek/verify')) { ai.settings.deepseek.status = 'active'; payload = { ...ai.settings, ok: true }; }
    else if (p.endsWith('/ai/settings/verify')) { ai.settings.status = 'active'; payload = { ...ai.settings, ok: true }; }
    else if (p.endsWith('/ai/gateways/yandex/verify')) { ai.settings.gateways.yandex={configured:ai.settings.configured,checks:{synthesis:{ok:true,checked_at:'2026-10-26T12:00:00Z'}}};payload={...ai.settings,ok:true}; }
    else if (p.endsWith('/ai/gateways/routes')) { ai.settings.gateways={...ai.settings.gateways,...entry.body};payload=ai.settings; }
    else if (/\/ai\/gateways\/(mimo|mimo_plan)(\/verify)?$/.test(p)) {
      const provider=p.includes('mimo_plan')?'mimo_plan':'mimo',old=ai.settings.gateways[provider];
      if(p.endsWith('/verify'))old.checks={...old.checks,[entry.body.capability]:{ok:true,checked_at:'2026-10-26T12:00:00Z'}};
      else ai.settings.gateways[provider]={...old,configured:!entry.body.clear_key,key_hint:entry.body.api_key?.slice(-4)||'',base_url:entry.body.base_url||old.base_url,protocol:entry.body.protocol||old.protocol};
      payload=ai.settings;
    }
    else if (p.endsWith('/ai/synthesize')) {
      const pcm=Buffer.alloc(32000);for(let n=0;n<16000;n++)pcm.writeInt16LE(Math.round(Math.sin(n*2*Math.PI*440/16000)*2000),n*2);
      const wav=Buffer.alloc(44+pcm.length);wav.write('RIFF',0);wav.writeUInt32LE(36+pcm.length,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(16000,24);wav.writeUInt32LE(32000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(pcm.length,40);pcm.copy(wav,44);
      payload={audio:(ai.synthesisBroken?Buffer.from('invalid wave'):wav).toString('base64'),content_type:'audio/wav',seconds:1,billing:ai.settings.gateways.synthesis_provider==='yandex'?'pay_as_you_go':'subscription',cost_rub:.02};
    }
    else if (p.endsWith('/ai/agents/test')) payload = { text: 'Здравствуйте! Занятие завтра переносится на 17:00.', fallback: false, cost_rub: 0.12, tokens: { input: 140, output: 18 } };
    else if (/\/ai\/agents\/[^/]+\/versions$/.test(p)) payload = { versions: [{ version: 1, name: 'Чистовик', model: 'yandexgpt-5.1', prompt: 'Первая версия', temperature: 0.2, created_at: '2026-10-24T20:00:00Z', author: 'Олег Иванов' }] };
    else if (/\/ai\/agents\/[^/]+$/.test(p)) {
      const id = p.split('/').pop(), index = ai.agents.findIndex(agent => agent.id === id);
      if (method === 'DELETE') { ai.agents.splice(index, 1); return route.fulfill({ status: 204, body: '' }); }
      if (entry.body.is_default) ai.agents.forEach(agent => { agent.is_default = false; });
      ai.agents[index] = { ...ai.agents[index], ...entry.body, version: ai.agents[index].version + 1 }; payload = ai.agents[index];
    } else if (p.endsWith('/ai/agents')) {
      if (method === 'POST') {
        if (entry.body.is_default || !ai.agents.some(agent => agent.is_default)) ai.agents.forEach(agent => { agent.is_default = false; });
        const agent = { id: 'agent-' + (ai.agents.length + 1), ...entry.body, is_default: entry.body.is_default || !ai.agents.some(a => a.is_default),
          task_title: 'Голосовой ввод', model_title: 'YandexGPT Pro 5.1', version: 1, updated_at: '2026-10-24T20:00:00Z' };
        ai.agents.push(agent); payload = agent;
      } else payload = { agents: ai.agents };
    } else if (p.endsWith('/voice/diagnostic')) {
      return route.fulfill({ status: 204, body: '' });
    } else if (p.endsWith('/voice/transcribe')) {
      ai.transcribe.push(entry.body);
      if (ai.failNext > 0) { ai.failNext -= 1; return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Yandex Cloud сейчас недоступен. Повторите позже."}' }); }
      payload = { raw_text: 'ну это здравствуйте завтра занятие в пять', text: 'Здравствуйте! Завтра занятие в 17:00.', seconds: 1.5,
        agent: { id: 'agent-1', name: 'Чистовик' }, fallback: false, warning: null };
      if (ai.fallbackNext > 0) { ai.fallbackNext -= 1; Object.assign(payload, { text: payload.raw_text, fallback: true,
        warning: 'Текст вставлен без обработки: агент сейчас недоступен.' }); }
    }
    else if (p.endsWith('/summary')) payload = metrics;
    else if (p.endsWith('/chat-updates')) payload = { updates: [], next_seq: 4, has_more: false };
    else if (p.endsWith('/chats')) {
      const view = url.searchParams.get('view'), status = url.searchParams.get('status');
      const shown = conversations.filter(c => (view !== 'unread' || c.unread) && (!status || c.status === status));
      const lead_unread = conversations.filter(c => c.status === 'lead' && c.unread).reduce((sum, c) => sum + c.unread_count, 0);
      payload = { conversations: shown, total: shown.length, unread: 3, lead_unread, page: 0, has_more: false };
    } else if (p.endsWith('/messages')) {
      const conversation = conversations.find(c => p.includes('/' + c.id + '/'));
      if (method === 'POST') {
        messages.push({ ...messages[1], id: 'sent-' + messages.length, text: entry.body.text, state: 'accepted', error: undefined,
          occurred_at: '2026-10-24T10:30:00Z', sort_time: String(BigInt(messages.at(-1).sort_time) + 60000000n), crm_order: String(messages.length + 1) });
        payload = { request_id: entry.body.request_id, state: 'accepted' };
      } else payload = { conversation, messages, next_seq: messages.length, has_more: false, more_changes: false, total: messages.length };
    } else if (p.endsWith('/handled')) { const c = conversations.find(c => p.includes('/' + c.id + '/')); c.unread = false; c.unread_count = 0; c.revision = (c.revision || 1) + 1; payload = {conversation:c}; }
    else if (p.endsWith('/family') || /\/(directory|chat-contacts)\/contacts\//.test(p)) payload = family;
    else if (p.endsWith('/directory/card-loader')) { if (method === 'PUT') Object.assign(cardLoader, entry.body); payload = cardLoader; }
    else if (/\/(directory|chat-contacts)\/sync$/.test(p)) payload = { configured: true, state: 'idle', last_success_at: '2026-10-24T11:00:00Z', auto_refresh: false, interval_minutes: 30, stats: {} };
    else if (/\/(directory|chat-contacts)$/.test(p)) payload = { contacts, total: contacts.length, branches, snapshot: 'run-1', page: 0, has_more: false };
    else if (p.endsWith('/members')) payload = { members };
    else if (/\/members\/.*\/sections$/.test(p)) { members.find(m => p.includes('/' + m.user_id + '/')).sections = entry.body.sections; payload = {}; }
    else if (/\/members\/.*\/role$/.test(p)) { members.find(m => p.includes('/' + m.user_id + '/')).role = entry.body.role; payload = {}; }
    else if (p.endsWith('/integrations/max')) payload = { vault_ready: true, channels: [{ id: 'bot', name: 'MAX-бот', kind: 'max_bot', bot_name: '@center_bot', enabled: true, verified: true, can_send: true }] };
    else if (/\/integrations\/max\/bot\/(check|disable|enable)$/.test(p)) payload = {};
    else if (p.endsWith('/integrations/alfa')) payload = { configured: true, status: 'active', vault_ready: true, branches, origin: 'https://example.s20.online', email: 'demo@example.test', last_success_at: '2026-10-24T11:00:00Z' };
    else if (p.endsWith('/integrations/alfa-max')) payload = { configured: true, state: 'active', vault_ready: true, crm_configured: true, branches };
    else if (p.endsWith('/sync-progress')) payload = { configured: true, state: 'active', messages: 120, conversations: 40, backfill_pending: false, history_complete: true, streams: [] };
    else { unexpected.push(method + ' ' + p); return route.fulfill({ status: 501, contentType: 'application/json', body: '{"detail":"Unmocked request"}' }); }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(payload) });
  });
  return { calls, mutations, errors, unexpected, conversations, messages, ai, preferences, tasks };
}
module.exports = { fixture };
