// Optional whole-app browser check with synthetic API responses only.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const root = path.join(__dirname, "../app/backend/src/center_messenger/web");
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ["--no-sandbox", "--disable-dev-shm-usage", ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || "[]")] });
  try {
    const page = await browser.newPage(), errors = [];
    let statusRequests = 0;
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(() => {
      const originalFetch = window.fetch.bind(window), timeout = window.setTimeout.bind(window);
      window.progressTest = { requests: 0, aborts: 0, offline: false };
      // Accelerate only the status request deadline and its polling delays.
      window.setTimeout = (callback, delay, ...args) => timeout(callback, [15000, 5000, 2000, 4000, 1000].includes(delay) ? 100 : delay, ...args);
      window.fetch = (url, options) => {
        if (new URL(url, location.origin).pathname.endsWith("/sync-progress") && ++window.progressTest.requests === 3) {
          return new Promise((resolve, reject) => options.signal.addEventListener("abort", () => {
            window.progressTest.aborts += 1;
            reject(new DOMException("Synthetic stalled response", "AbortError"));
          }));
        }
        return originalFetch(url, options);
      };
      document.addEventListener("DOMContentLoaded", () => {
        new MutationObserver(() => {
          if (document.querySelector("#alfa-max-progress.is-offline")) window.progressTest.offline = true;
        }).observe(document.body, { subtree: true, attributes: true });
      });
    });
    await page.route("**/*", route => {
      const url = new URL(route.request().url()), pathname = url.pathname;
      if (url.hostname !== "synthetic.example.test") return route.abort();
      if (pathname === "/") return route.fulfill({ contentType: "text/html", body: fs.readFileSync(root + "/index.html", "utf8") });
      if (pathname.startsWith("/assets/")) {
        const file = path.join(root, path.basename(pathname));
        const ext = path.extname(file), mime = { ".js": "application/javascript", ".css": "text/css", ".woff2": "font/woff2", ".jpg": "image/jpeg", ".png": "image/png" }[ext];
        return fs.existsSync(file) ? route.fulfill({ contentType: mime, body: fs.readFileSync(file) }) : route.abort();
      }
      let payload;
      if (pathname === "/api/auth/status") payload = { setup_required: false };
      else if (pathname === "/api/auth/session") payload = { user: { id: "test", display_name: "Test", email: "test@example.test" }, csrf: "test",
        workspaces: [{ id: "test", name: "Synthetic workspace", role: "owner", timezone: "Europe/Moscow" }] };
      else if (pathname.endsWith("/summary")) payload = { incoming: 0, previous_incoming: 0, waiting: 0, new_leads: 0, average_response_seconds: null, days: [], branches: [], operators: [] };
      else if (pathname.endsWith("/chats/context")) payload = { channels: [], branches: [] };
      else if (pathname.endsWith("/chat-updates")) payload = { updates: [], next_seq: 0, has_more: false };
      else if (pathname.endsWith("/sync-progress")) {
        statusRequests += 1;
        if (statusRequests === 2) return route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"Temporary failure"}' });
        payload = { configured: true, state: "active", messages: statusRequests > 2 ? 82770 : 82749, conversations: 2411,
          backfill_pending: true, streams: [{ branch_id: "2", direction: "sent", source_rows: 16974, expected_rows: 19499, checked_page: 848 + statusRequests }] };
      } else if (pathname.endsWith("/integrations/alfa-max")) payload = { configured: true, state: "active", vault_ready: true, crm_configured: true, branches: [{ id: "2", name: "Test" }] };
      else if (pathname.endsWith("/integrations/alfa")) payload = { configured: true, status: "active", vault_ready: true, branches: [{ id: "2", name: "Test" }] };
      else if (pathname.endsWith("/integrations/max")) payload = { channels: [], vault_ready: true };
      else return route.abort();
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(payload) });
    });
    await page.goto("https://synthetic.example.test/");
    await page.locator("#connections-tab").click();
    await page.waitForFunction(() => window.progressTest.aborts === 1 && document.querySelector('[data-metric="messages"] strong')?.dataset.count === "82770");
    const result = await page.evaluate(() => ({ ...window.progressTest,
      recovered: !document.querySelector("#alfa-max-progress").classList.contains("is-offline"),
      stream: document.querySelector("#alfa-max-progress-streams").textContent }));
    assert.ok(result.offline && result.recovered);
    assert.match(result.stream, /страница/);
    await page.locator("#overview-tab").click();
    const before = await page.evaluate(() => window.progressTest.requests);
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal(await page.evaluate(() => window.progressTest.requests), before);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ browser: browser.version(), ...result, pollingStoppedOnLeave: true, errors }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
