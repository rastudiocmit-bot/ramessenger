// Optional whole-app appearance check. All API replies and clients are synthetic.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const web = path.join(__dirname, "../app/backend/src/center_messenger/web");
const screenshotDir = process.env.CM_APPEARANCE_SCREENSHOT_DIR;
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ["--no-sandbox", "--disable-dev-shm-usage", ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || "[]")] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: "dark" });
    const errors = [], mutations = [];
    context.on("page", page => page.on("pageerror", error => errors.push(error.message)));
    const conversations = Array.from({ length: 40 }, (_, n) => ({ id: "client-" + n,
      name: "Клиент " + String(n + 1).padStart(2, "0"), students: ["Ученик " + (n + 1)],
      status: n % 2 ? "lead" : "client", channel_name: "AlfaCRM", contact_id: "contact-" + n,
      unread: n < 3, unread_count: n < 3 ? n + 1 : 0, can_send: true, revision: 1,
      last_message_at: "2026-10-24T11:00:00Z", preview: "Демонстрационное сообщение" }));
    const messages = [
      { id: "m1", text: "Здравствуйте! Подскажите расписание занятий.", direction: "in", actor_type: "contact", state: "received" },
      { id: "m2", text: "Добрый день! Занятия по вторникам и четвергам.", direction: "out", actor_type: "human", state: "accepted" },
      { id: "m3", text: "Напоминаем о занятии в 17:00.", direction: "out", actor_type: "automation", template_name: "Напоминание о занятии", state: "accepted" }
    ].map((m, n) => ({ ...m, occurred_at: `2026-10-24T10:0${n}:00Z`,
      sort_time: String(1792836000000000 + n * 60000000), crm_order: String(n + 1), version: 1, attachments: [] }));
    await context.route("**/*", route => {
      const request = route.request(), url = new URL(request.url()), p = url.pathname;
      if (url.hostname !== "appearance.example.test") return route.abort();
      if (request.method() !== "GET") mutations.push({ method: request.method(), path: p });
      if (p === "/") return route.fulfill({ contentType: "text/html", body: fs.readFileSync(web + "/index.html") });
      if (p.startsWith("/assets/")) {
        const file = path.join(web, path.basename(p));
        if (!fs.existsSync(file)) return route.abort();
        return route.fulfill({ contentType: file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "image/png", body: fs.readFileSync(file) });
      }
      let payload = {};
      if (p === "/api/auth/status") payload = { setup_required: false };
      else if (p === "/api/auth/session") payload = { user: { id: "test", display_name: "Демонстрационный сотрудник", email: "demo@example.test" }, csrf: "test",
        workspaces: [{ id: "test", name: "Проверка оформления", role: "owner", timezone: "Europe/Moscow" }] };
      else if (p.endsWith("/integrations/max")) payload = { vault_ready: true, channels: [{ id: "native", name: "AlfaCRM", kind: "alfa_max_browser" }] };
      else if (p.endsWith("/integrations/alfa")) payload = { branches: [] };
      else if (p.endsWith("/chat-updates")) payload = { updates: [], next_seq: 0, has_more: false };
      else if (p.endsWith("/chats")) payload = { conversations, total: conversations.length, unread: 3, page: 0, has_more: false };
      else if (p.endsWith("/messages")) payload = { conversation: conversations.find(c => p.includes("/" + c.id + "/")),
        messages: url.searchParams.has("after_seq") ? [] : messages, next_seq: 3, has_more: false, more_changes: false, total: 3 };
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(payload) });
    });
    const page = await context.newPage();
    const open = async target => { await target.goto("https://appearance.example.test/?view=messages"); await target.waitForSelector(".chat-bubble.system"); };
    const theme = target => target.locator("html").getAttribute("data-theme");
    const saveScreenshot = async (name, target = page) => {
      if (!screenshotDir) return;
      fs.mkdirSync(screenshotDir, { recursive: true });
      await target.screenshot({ path: path.join(screenshotDir, name + ".png"), fullPage: true, animations: "disabled" });
    };
    await open(page);
    assert.equal(await theme(page), "light", "A dark operating system must not select the application theme");
    assert.equal(await page.locator(".avatar-count").first().textContent(), "1");
    assert.equal(await page.locator(".avatar-letter").first().textContent(), "К");
    assert.equal(await page.locator(".avatar-count").first().evaluate(e => getComputedStyle(e).backgroundColor), "rgb(61, 141, 196)");
    for (const selector of [".chat-list-scroll", ".chat-stream", ".directory-list-panel"])
      assert.equal(await page.locator(selector).evaluate(e => getComputedStyle(e).scrollbarWidth), "none");
    await saveScreenshot("installed-light-desktop");
    const list = page.locator(".chat-list-scroll"), bounds = await list.boundingBox();
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.wheel(0, 500);
    await page.waitForFunction(() => document.querySelector(".chat-list-scroll").scrollTop > 0);
    const listWheelScroll = await list.evaluate(e => e.scrollTop);
    await page.locator("#chat-settings-open").click();
    await page.locator("#chat-theme").selectOption("dark");
    assert.equal(await theme(page), "dark");
    for (const selector of [".sidebar", ".topbar", ".chat-list-panel", "#chat-settings-dialog"])
      assert.equal(await page.locator(selector).evaluate(e => getComputedStyle(e).backgroundColor), "rgb(19, 31, 36)");
    await saveScreenshot("installed-dark-settings");
    await page.locator('[data-close="chat-settings-dialog"]').click();
    await list.evaluate(e => { e.scrollTop = 0; });
    await saveScreenshot("installed-dark-desktop");
    await page.emulateMedia({ colorScheme: "light" });
    assert.equal(await theme(page), "dark");
    await page.reload(); await page.waitForSelector(".chat-bubble.system");
    assert.equal(await theme(page), "dark", "Chosen theme must survive reload");
    const other = await context.newPage(); await open(other);
    assert.equal(await theme(other), "dark");
    await other.locator("#chat-settings-open").click();
    await other.locator("#chat-theme").selectOption("light");
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    assert.equal(await page.locator("#chat-theme").inputValue(), "light");
    await other.close();
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    await saveScreenshot("installed-light-mobile");
    await page.locator("#chat-settings-open").click();
    await page.locator("#chat-theme").selectOption("dark");
    assert.equal(await page.locator("#chat-settings-dialog").evaluate(e => e.scrollWidth <= e.clientWidth), true);
    await page.locator('[data-close="chat-settings-dialog"]').click();
    await saveScreenshot("installed-dark-mobile");
    assert.deepEqual(errors, []); assert.deepEqual(mutations, []);
    console.log(JSON.stringify({ browser: browser.version(), defaultLight: true, explicitTheme: true,
      persistedAfterReload: true, updatedOtherTab: true, listWheelScroll, mobileWidth: 390, errors, mutations }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
