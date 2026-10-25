// Synthetic data only. Every request is intercepted; no real CRM or MAX delivery.
const fs = require('node:fs');
const path = require('node:path');
const web = path.join(__dirname, '../app/backend/src/center_messenger/web');
async function fixture(context, role = 'owner', sections = ['overview', 'messages', 'security'], options = {}) {
  const calls = [], mutations = [], errors = [], unexpected = [];
  const preferences = { name_order: "parent_first" };
  // Voice input and AI agents (Connections → service settings). Off in the chat unless asked.
  const ai = { settings: { configured: Boolean(options.voice), folder_id: options.voice ? 'b1gexamplefolder0000' : '', key_hint: options.voice ? 'k3y9' : '',
      voice_enabled: Boolean(options.voice), monthly_limit_rub: null, status: options.voice ? 'active' : 'not_configured', error: null, verified_at: null,
      month_spent_rub: 0, vault_ready: true, models: [{ id: 'yandexgpt-5.1', title: 'YandexGPT Pro 5.1', provider: 'yandex', provider_title: 'Yandex Cloud' },
        { id: 'aliceai-llm', title: 'Alice AI LLM', provider: 'yandex', provider_title: 'Yandex Cloud' },
        { id: 'deepseek-flash', title: 'DeepSeek V4.1 Flash', provider: 'deepseek', provider_title: 'DeepSeek' },
        { id: 'deepseek-v4-pro', title: 'DeepSeek V4 Pro', provider: 'deepseek', provider_title: 'DeepSeek' }],
      deepseek: { configured: false, key_hint: '', status: 'not_configured', error: null },
      tasks: [{ id: 'voice_input', title: 'Голосовой ввод' }], default_model: 'yandexgpt-5.1', default_prompt: 'Нормализуй текст, убери слова-паразиты.' },
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
    { id: 'm4', text: 'Спасибо! Запишите нас, пожалуйста, на пробное занятие.', direction: 'in', actor_type: 'contact', state: 'received' }
  ].map((m, n) => ({ ...m, occurred_at: `2026-10-24T10:0${n}:00Z`, customer_branch_id: '1',
    sort_time: String(1792836000000000 + n * 60000000), crm_order: String(n + 1), version: 1, attachments: [] }));
  const contacts = conversations.slice(0, 9).map((c, n) => ({ id: c.contact_id, name: c.name, status: c.status,
    students: c.students, student_count: 1, groups: ['Подготовка к школе'], group_count: 1,
    max_connected: true, max_bot_connected: true, needs_review: false }));
  const family = { id: 'contact-0', name: conversations[0].name, phones: ['+7 900 000-00-00'], emails: [], today: '2026-10-24',
    max_connected: true, max_bot_connected: true, students: [{ id: 'student-0', name: 'Миша Смирнов', dob: '2020-05-14', balance: 2400,
      groups: [], cards: [{ id: '100', branch_id: '1', branch_name: 'Центральный', status: 'client', needs_refresh: false, subscriptions: [], url: 'https://example.s20.online/customer/view?id=100' }] }] };
  const members = [{ user_id: 'test', display_name: 'Олег Иванов', email: 'demo@example.test', role },
    { user_id: 'operator-1', display_name: 'Мария Петрова', email: 'operator@example.test', role: 'operator', sections: ['messages', 'security'] }];
  const branches = [{ id: '1', name: 'Центральный' }, { id: '2', name: 'Северный' }];
  const channels = [{ id: 'native', name: 'MAX через AlfaCRM', kind: 'alfa_max_browser', enabled: true, status: 'active' }];
  const metrics = { incoming: 268, previous_incoming: 240, waiting: 3, average_response_seconds: 240, new_leads: 18,
    days: [24, 43, 36, 48, 41, 46, 30].map((incoming, n) => ({ label: ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'][n], incoming })),
    branches: [{ name: 'Центральный', incoming: 164 }, { name: 'Северный', incoming: 104 }],
    operators: [{ name: 'Мария Петрова', answered: 73, average_response_seconds: 210 }, { name: 'Олег Иванов', answered: 51, average_response_seconds: 270 }] };
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname, method = request.method();
    if (url.hostname !== 'cabinet.example.test') return route.abort();
    if (p === '/') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(web + '/index.html') });
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
    else if (p === '/api/auth/session') payload = { user: { id: 'test', display_name: 'Олег Иванов', email: 'demo@example.test' }, csrf: 'synthetic-csrf',
      workspaces: [{ id: 'test', name: 'Развивайся', role, timezone: 'Europe/Moscow', sections }] };
    else if (p === '/api/auth/sessions') payload = { sessions: [{ id: 'session', current: true, device_label: 'Chrome · Linux', last_seen_at: '2026-10-24T11:00:00Z' }] };
    else if (p === '/api/auth/logout') payload = {};
    else if (p.endsWith('/chats/context')) payload = { preferences, channels, branches, can_write: role !== 'observer', send_attachments: false,
      voice_input: Boolean(options.voice) && role !== 'observer' };
    else if (p.endsWith('/chats/preferences')) { Object.assign(preferences, entry.body); payload=preferences; }
    else if (p.endsWith('/ai/settings')) {
      if (method === 'PUT') Object.assign(ai.settings, { folder_id: entry.body.folder_id, voice_enabled: entry.body.voice_enabled, monthly_limit_rub: entry.body.monthly_limit_rub,
        key_hint: entry.body.clear_key ? '' : entry.body.api_key ? entry.body.api_key.slice(-4) : ai.settings.key_hint, status: 'not_configured' },
        { configured: Boolean(entry.body.folder_id) && !entry.body.clear_key && Boolean(entry.body.api_key || ai.settings.key_hint) });
      payload = ai.settings;
    } else if (p.endsWith('/ai/deepseek')) {
      ai.settings.deepseek = entry.body.clear_key ? { configured: false, key_hint: '', status: 'not_configured', error: null }
        : { configured: true, key_hint: entry.body.api_key.slice(-4), status: 'not_configured', error: null };
      payload = ai.settings;
    } else if (p.endsWith('/ai/deepseek/verify')) { ai.settings.deepseek.status = 'active'; payload = { ...ai.settings, ok: true }; }
    else if (p.endsWith('/ai/settings/verify')) { ai.settings.status = 'active'; payload = { ...ai.settings, ok: true }; }
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
        messages.push({ ...messages[1], id: 'sent-' + messages.length, text: entry.body.text });
        payload = { request_id: entry.body.request_id, state: 'accepted' };
      } else payload = { conversation, messages, next_seq: messages.length, has_more: false, more_changes: false, total: messages.length };
    } else if (p.endsWith('/handled')) { const c = conversations.find(c => p.includes('/' + c.id + '/')); c.unread = false; c.unread_count = 0; c.revision = (c.revision || 1) + 1; payload = {conversation:c}; }
    else if (p.endsWith('/family') || /\/(directory|chat-contacts)\/contacts\//.test(p)) payload = family;
    else if (/\/(directory|chat-contacts)\/sync$/.test(p)) payload = { configured: true, state: 'idle', last_success_at: '2026-10-24T11:00:00Z', auto_refresh: false, interval_minutes: 30, stats: {} };
    else if (/\/(directory|chat-contacts)$/.test(p)) payload = { contacts, total: contacts.length, branches, snapshot: 'run-1', page: 0, has_more: false };
    else if (p.endsWith('/members')) payload = { members };
    else if (/\/members\/.*\/role$/.test(p)) { members.find(m => p.includes('/' + m.user_id + '/')).role = entry.body.role; payload = {}; }
    else if (p.endsWith('/integrations/max')) payload = { vault_ready: true, channels: [{ id: 'bot', name: 'MAX-бот', kind: 'max_bot', bot_name: '@center_bot', enabled: true, verified: true, can_send: true }] };
    else if (/\/integrations\/max\/bot\/(check|disable|enable)$/.test(p)) payload = {};
    else if (p.endsWith('/integrations/alfa')) payload = { configured: true, status: 'active', vault_ready: true, branches, origin: 'https://example.s20.online', email: 'demo@example.test', last_success_at: '2026-10-24T11:00:00Z' };
    else if (p.endsWith('/integrations/alfa-max')) payload = { configured: true, state: 'active', vault_ready: true, crm_configured: true, branches };
    else if (p.endsWith('/sync-progress')) payload = { configured: true, state: 'active', messages: 120, conversations: 40, backfill_pending: false, history_complete: true, streams: [] };
    else { unexpected.push(method + ' ' + p); return route.fulfill({ status: 501, contentType: 'application/json', body: '{"detail":"Unmocked request"}' }); }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(payload) });
  });
  return { calls, mutations, errors, unexpected, conversations, messages, ai, preferences };
}
module.exports = { fixture };
