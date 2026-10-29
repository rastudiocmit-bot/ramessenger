"use strict";
window.CenterSyncMaster = ({ $, state, show, refreshStatus = () => {} }) => {
  const names = ["status", "connections", "policy"], scroll = $("#pane-connections > .scroll");
  const positions = new Map();
  let active = "status", away = false, report = null, reportView = null, seenMax = null, action = null;
  function display() {
    state.syncMasterSection = active;
    for (const name of names) {
      const selected = name === active, tab = $("#sync-master-" + name + "-tab");
      tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1;
      show($("#sync-master-" + name + "-panel"), selected);
    }
  }
  function select(name, { focus = false } = {}) {
    if (!names.includes(name)) name = "status";
    if (name !== active) {
      positions.set(active, scroll.scrollTop); active = name; display();
      scroll.scrollTop = positions.get(active) || 0;
    }
    if (focus) $("#sync-master-" + active + "-tab").focus({ preventScroll: true });
  }
  function openSetup(id) {
    select("connections");
    const details = $("#" + id); details.open = true;
    details.scrollIntoView?.({ block: "nearest" });
  }
  for (const button of document.querySelectorAll("[data-sync-section]")) button.addEventListener("click", () => {
    select(button.dataset.syncSection);
    if (button.dataset.syncOpen) openSetup(button.dataset.syncOpen);
  });
  $(".sync-master-tabs").addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    const index = names.indexOf(event.target.closest("[data-sync-section]")?.dataset.syncSection || active);
    const next = event.key === "Home" ? names[0] : event.key === "End" ? names.at(-1)
      : names[(index + (event.key === "ArrowLeft" ? names.length - 1 : 1)) % names.length];
    select(next, { focus: true });
  });
  $("#sync-master-alert-action").addEventListener("click", () => {
    if (action === "refresh") { select("status"); refreshStatus(); }
    else if (action) openSetup(action);
  });
  function render() {
    const crm = state.alfa, max = state.alfaMax;
    const at = report?.last_sync_at || max?.last_sync_at;
    const valid = at && Number.isFinite(Date.parse(at));
    $("#sync-master-last-sync").textContent = valid ? new Intl.DateTimeFormat("ru-RU", {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
      timeZone: state.workspace?.timezone || "Europe/Moscow"
    }).format(new Date(at)) : "Ожидаем первую проверку";
    $("#sync-master-last-sync").title = valid ? "Последнее успешное чтение сообщений из AlfaCRM" : "Успешное чтение сообщений пока не подтверждено";
    let message = null;
    const restoring = ["checking", "reconnecting", "waiting"].includes(max?.recovery?.phase);
    if (crm?.vault_ready === false) message = ["Подключение пока недоступно", "На сервере не подготовлено защищённое хранилище. Нужна настройка администратором сервера.", "alfa-config", "Показать подробности"];
    else if (crm && !crm.configured) message = ["Подключите AlfaCRM", "Так в мессенджер попадут клиенты, абонементы и занятия.", "alfa-config", "Подключить AlfaCRM"];
    else if (crm?.configured && crm.status !== "active") message = ["Проверьте доступ к AlfaCRM", crm.error || "Откройте подключение и нажмите «Проверить связь».", "alfa-config", "Проверить доступ"];
    else if (max?.state === "otp_required") message = ["Нужен код из письма", "Откройте подключение MAX и введите код, который прислала AlfaCRM.", "sync-max-config", "Ввести код"];
    else if (max && !restoring && (!max.configured || max.state === "not_configured")) message = ["Подключите переписку MAX", "Войдите под сотрудником AlfaCRM, у которого работает MAX.", "sync-max-config", "Подключить MAX"];
    else if (max && !restoring && ["error", "reauth_required"].includes(max.state)) message = ["Подключению MAX нужно внимание", max.error || "Проверьте данные входа. Если запрошен код из письма, подтвердите его.", "sync-max-config", "Открыть подключение"];
    else if (reportView?.mode === "offline") message = ["Статус временно недоступен", "Показан последний отчёт. Это не означает, что загрузка на сервере остановилась.", "refresh", "Повторить проверку"];
    show($("#sync-master-alert"), Boolean(message)); action = message?.[2] || null;
    $("#sync-master-alert-title").textContent = message?.[0] || "";
    $("#sync-master-alert-text").textContent = message?.[1] || "";
    $("#sync-master-alert-action").textContent = message?.[3] || "Открыть подключения";
    if (max && max.state !== seenMax) {
      if (["otp_required", "reauth_required", "error", "not_configured"].includes(max.state)) $("#sync-max-config").open = true;
      seenMax = max.state;
    }
  }
  function reset() {
    active = "status"; away = false; report = null; reportView = null; seenMax = null; positions.clear();
    $("#sync-max-config").open = false;
    display(); render();
  }
  function leave() { if (!away) positions.set(active, scroll.scrollTop); away = true; }
  function enter() { display(); if (away) scroll.scrollTop = positions.get(active) || 0; away = false; }
  reset();
  return { select, enter, leave, reset, render, progress: (payload, view) => {
    if (payload) report = payload; reportView = view; render();
  } };
};
