"use strict";
// A read-only view of the saved directory response. No fetching, queueing or
// financial/identity merging here: totals and links are resolved on the server.
window.CenterDirectoryCard = ({ node, date }) => {
  const labels = { client: "Клиент", lead: "Лид", archive: "Архивный" };
  const known = value => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
  const money = value => known(value) ? new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value)) + " ₽" : "Нет данных";
  const day = value => /^\d{4}-\d{2}-\d{2}$/.test(value || "") ? value.split("-").reverse().join(".") : "Не указана";
  const lessonDay = lesson => lesson?.date ? [day(lesson.date), lesson.time].filter(Boolean).join(", ") : "Нет сохранённых данных";
  const title = item => item.name || (item.tariff_id ? `Абонемент №${item.tariff_id}` : "Абонемент");
  const el = (tag, cls, ...children) => { const e = node(tag, undefined, cls); for (const c of children) if (c !== null && c !== undefined && c !== false) e.append(c); return e; };
  const text = (value, cls) => node("span", value, cls);
  const note = value => node("p", value, "directory-data-note");
  const fact = (label, value) => el("div", "directory-data-fact", node("dt", label), node("dd", value ?? "Нет данных"));
  const part = (label, ...body) => el("section", "directory-data-part", node("h4", label), ...body);
  function crmLink(value, label) {
    try {
      const url = new URL(value);
      if (url.protocol !== "https:" || !/^[a-z0-9-]+\.s20\.online$/i.test(url.hostname)) return null;
      const a = node("a", label, "directory-data-link"); a.href = url.href; a.target = "center-messenger-crm"; a.rel = "noopener noreferrer"; return a;
    } catch (_) { return null; }
  }
  function period(begin, end) { return `${begin ? day(begin) : "Начало не указано"} — ${end ? day(end) : "срок не указан"}`; }
  function subscription(item, today) {
    const status = item.archived ? "Архивный" : item.end && item.end < today ? "Срок истёк" : item.begin && item.begin > today ? "Ещё не начался" : "Действующий";
    const row = el("article", "directory-subscription",
      el("div", "directory-data-row", node("strong", title(item)), text(status, "directory-data-chip")),
      el("dl", "directory-data-facts", fact("Срок действия", period(item.begin, item.end)), fact("Остаток", money(item.balance)),
        fact("ID абонемента", item.id === undefined ? null : String(item.id)), fact("Customer ID", item.customer_id === undefined ? null : String(item.customer_id))));
    if (known(item.balance) && Number(item.balance) < 0) row.classList.add("negative");
    if (item.needs_review) row.append(note("Сохранённые источники расходятся. Сумму и срок нужно уточнить."));
    if (item.relation_status && item.relation_status !== "confirmed") row.append(note("Связь с занятиями пока не подтверждена."));
    return row;
  }
  function group(item) {
    const links = item.subscriptions || [];
    const payment = links.length ? links.map(s => `${title(s)} · ${money(s.balance)} · ${period(s.begin, s.end)}`).join("\n") : "Связь с абонементом не подтверждена";
    const previous = item.previous_lesson;
    const attendance = previous?.attended === true ? " · посещено" : previous?.attended === false ? " · пропуск" : previous ? " · посещение не отмечено" : "";
    const g = el("article", "directory-data-group",
      el("div", "directory-data-row", node("strong", item.name || `Группа №${item.id}`), crmLink(item.url, "Группа в CRM")),
      el("dl", "directory-data-facts", fact("Филиал", item.branch_name || `№${item.branch_id}`), fact("ID группы", String(item.id)),
        fact("Прошлое занятие", lessonDay(previous) + attendance), fact("Следующее занятие", lessonDay(item.next_lesson)), fact("Оплата группы", payment)));
    if (item.memberships?.length) g.append(note("Участие: " + item.memberships.map(m => `${period(m.begin, m.end)} · Customer ID ${m.customer_id}`).join("; ")));
    return g;
  }
  function history(student) {
    // Canonical saved lessons come from current_lessons on the server. Never
    // concatenate raw branch observations or merge by names/phone numbers.
    if (!Array.isArray(student.lessons)) return part("Загруженные занятия", note("История занятий пока не получена."));
    const lessons = [...student.lessons].sort((a, b) => `${b.date || ""} ${b.time || ""}`.localeCompare(`${a.date || ""} ${a.time || ""}`) || String(a.id).localeCompare(String(b.id)));
    const section = part(`Загруженные занятия · ${lessons.length}`);
    if (!lessons.length) { section.append(note("В сохранённых данных занятий нет.")); return section; }
    const list = el("div", "directory-lesson-list"); section.append(list); let shown = 0;
    const more = node("button", "", "text-button directory-lessons-more"); more.type = "button";
    function append() {
      const end = Math.min(shown + 20, lessons.length);
      for (const lesson of lessons.slice(shown, end)) {
        const groups = (student.groups || []).filter(g => String(g.branch_id) === String(lesson.branch_id) && (lesson.group_ids || []).some(id => String(id) === String(g.id))).map(g => g.name);
        const branch = (student.cards || []).find(c => String(c.branch_id) === String(lesson.branch_id))?.branch_name || `Филиал №${lesson.branch_id ?? "—"}`;
        const attended = lesson.attended === true ? "Посещено" : lesson.attended === false ? "Пропуск" : "Посещение не отмечено";
        list.append(el("article", "directory-lesson", node("strong", lessonDay(lesson)), text(groups.join(" · ") || "Без текущей группы"), text(branch),
          node("small", `${attended} · занятие ID ${lesson.id} · Customer ID ${lesson.customer_id ?? "—"}${lesson.subscription_id ? ` · абонемент ID ${lesson.subscription_id}` : ""}${lesson.subject_id ? ` · предмет ID ${lesson.subject_id}` : ""}`)));
      }
      shown = end; more.hidden = shown === lessons.length; more.textContent = `Показать ещё ${Math.min(20, lessons.length - shown)} · осталось ${lessons.length - shown}`;
    }
    more.addEventListener("click", append); section.append(more); append(); return section;
  }
  function sourceCard(card) {
    const row = el("article", "directory-source-card",
      el("div", "directory-data-row", node("strong", card.branch_name || `Филиал №${card.branch_id}`), crmLink(card.url, "Карточка в CRM")),
      el("dl", "directory-data-facts", fact("Customer ID", String(card.id)), fact("ID филиала", String(card.branch_id)),
        fact("Статус", labels[card.status] || "Не указан"), fact("Статус обучения", card.study_status || "Не указан"),
        fact("Родитель / плательщик", card.legal_name || "Не указан"), fact("Баланс CRM-карточки", money(card.balance)),
        fact("Оплаченных занятий", known(card.paid_lesson_count) ? String(card.paid_lesson_count) : null)));
    const times = [["Баланс", card.balance_updated_at || card.refreshed_at], ["Занятия", card.lessons_refreshed_at], ["История", card.lessons_full_refreshed_at], ["Группы", card.groups_refreshed_at]].filter(([,v]) => v);
    if (times.length) row.append(note(times.map(([label,v]) => `${label}: ${date(v)}`).join(" · ")));
    if (card.needs_mapping_refresh) row.append(note("Связи абонементов этой карточки ещё не подтверждены."));
    return row;
  }
  function studentCard(student, today) {
    const cards = student.cards || [], statuses = [...new Set(cards.map(c => labels[c.status]).filter(Boolean))];
    const card = el("section", "student-card directory-student",
      el("header", "student-heading", el("div", "directory-student-name", node("h3", student.name || "Имя ребёнка не указано"), node("small", student.dob ? `Дата рождения: ${day(student.dob)}` : "Дата рождения не указана")), text(statuses.join(" · ") || "Статус не указан", "directory-data-chip")));
    card.append(el("dl", "directory-totals", fact("Общий баланс", money(student.balance)), fact("Оплаченных занятий", known(student.paid_lesson_count) ? String(student.paid_lesson_count) : null)));
    if (known(student.balance)) card.querySelector(".directory-totals").classList.add(Number(student.balance) < 0 ? "negative" : "positive");
    const subs = part("Абонементы");
    if (!Array.isArray(student.subscriptions)) subs.append(note("Сведения об абонементах пока не получены."));
    else if (!student.subscriptions.length) subs.append(note("В сохранённых данных абонементов нет."));
    else subs.append(...student.subscriptions.map(s => subscription(s, today)));
    card.append(subs, part("Группы и связи с абонементами", ...(student.groups?.length ? student.groups.map(group) : [note("Текущие группы не указаны.")])),
      part("Карточки AlfaCRM", ...cards.map(sourceCard)), history(student));
    return card;
  }
  return family => {
    const fragment = document.createDocumentFragment();
    const head = el("header", "contact-heading directory-family-head", node("span", "Родитель / контакт", "eyebrow"), node("h2", family.name || "Имя контакта не указано"));
    const channels = el("div", "directory-data-channels", text(family.max_connected ? "MAX через AlfaCRM подключён" : "MAX через AlfaCRM не подключён", "directory-data-chip"), text(family.max_bot_connected ? "MAX-бот подключён" : "MAX-бот не подключён", "directory-data-chip"));
    head.append(channels);
    const contacts = el("div", "contact-channels");
    for (const phone of family.phones || []) {
      const digits = String(phone).replace(/[^\d]/g, "");
      if (digits.length < 7 || digits.length > 15) { contacts.append(text(phone + " · номер требует проверки")); continue; }
      const a = node("a", phone); a.href = `tel:${String(phone).replace(/[^\d+]/g, "")}`; contacts.append(a);
    }
    for (const email of family.emails || []) { const a = node("a", email); a.href = `mailto:${email}`; contacts.append(a); }
    if (!contacts.childNodes.length) contacts.append(text("Телефон и почта в CRM не указаны."));
    head.append(contacts);fragment.append(head);
    if (family.needs_review) fragment.append(note("Есть совпадения контактных данных или расхождения в сведениях. CRM-карточки показаны отдельно по точным ID."));
    if (family.cache?.snapshot?.published_at || family.snapshot_at) fragment.append(note("Сохранённая карточка: " + date(family.cache?.snapshot?.published_at || family.snapshot_at)));
    const states = {ready:"Обновлено",stale:"Сохранённые данные устарели",missing:"Данные не получены",partial:"Получена часть данных",queued:"В очереди",loading:"Загружается",busy:"Канал занят",retry_wait:"Ожидается повтор",error:"Ошибка обновления"};
    if (family.cache?.sections) {
      const dl = el("dl", "directory-data-facts directory-cache-status");
      for (const [key,label] of [["finance","Баланс и абонементы"],["lessons","Ближайшие занятия"],["groups","Группы и связи"],["history","История занятий"]]) {
        const s=family.cache.sections[key];if(!s)continue;
        dl.append(fact(label, [states[s.status] || "Состояние не определено",s.updated_at && date(s.updated_at),s.retry_at && `повтор ${date(s.retry_at)}`,s.error_message].filter(Boolean).join(" · ")));
      }
      fragment.append(dl);
    }
    if (!(family.students || []).length) fragment.append(note("У этого контакта нет сохранённых карточек детей."));
    for (const student of family.students || []) fragment.append(studentCard(student, family.today || ""));
    if (family.has_more) fragment.append(note("Получена часть карточек или групп. Выберите филиал в фильтре, чтобы увидеть остальные данные."));
    return fragment;
  };
};
