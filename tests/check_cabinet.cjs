// Functional check of the supplied full cabinet, with intercepted synthetic APIs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
  try {
    const checked = [];
    for (const role of ['owner', 'admin', 'branch_manager', 'operator', 'observer']) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' }), data = await fixture(context, role), page = await context.newPage();
      page.setDefaultTimeout(10000);
      const admin = ['owner', 'admin'].includes(role);
      const save = async name => { if (process.env.CM_APPEARANCE_SCREENSHOT_DIR && role === 'owner') {
        fs.mkdirSync(process.env.CM_APPEARANCE_SCREENSHOT_DIR, { recursive: true });
        await page.screenshot({ path: path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR, name + '.png'), animations: 'disabled' });
      } };
      await page.goto('https://cabinet.example.test/');
      await page.waitForSelector('#pane-messages');
      assert.equal(await page.locator('#navrail>[data-pane]').first().getAttribute('data-pane'), 'messages');
      assert.equal(await page.locator('#tabbar>[data-pane]').first().getAttribute('data-pane'), 'messages');
      if (role !== 'operator') {
        await page.locator('#summary-tab').click();
        await page.waitForSelector('.kpi'); assert.equal(await page.locator('.kpi dd').first().textContent(), '268');
        assert.ok(await page.locator('#summary-content .card').evaluateAll(cards => cards.every(card => {
          const head = card.querySelector('.card-h').getBoundingClientRect(), body = card.querySelector('.card-b').getBoundingClientRect();
          return body.top >= head.bottom - 1 && body.left >= card.getBoundingClientRect().left;
        })), 'Metric bodies must not overlap their headings');
        await save('installed-summary');
      }
      assert.equal(await page.locator('#team-tab').count(), admin ? 1 : 0);
      assert.equal(await page.locator('#connections-tab').count(), admin ? 1 : 0);
      assert.equal(await page.locator('#clients-tab').count(), role === 'operator' ? 0 : 1);
      await page.locator('#messages-tab').click(); await page.waitForSelector('.chat-bubble');
      assert.equal(await page.locator('.chat-bubble').count(), 4);
      await page.locator('#chat-search-toggle').click(); assert.equal(await page.locator('#chat-search').isVisible(), true);
      // Chat filters moved to Profile: nothing next to search opens settings any more.
      assert.equal(await page.locator('#chat-search-wrap button').count(), 0);
      await page.locator('#chat-person-open').click(); await page.waitForFunction(() => document.querySelector('#chat-details-name').textContent === 'Анна Смирнова');
      await page.locator('[data-close="chat-details-dialog"]').click();
      // A closed client card must leave nothing on screen (it once stayed visible under the list).
      await page.waitForFunction(() => { const d = document.querySelector('#chat-details-dialog'); return !d.open && d.getBoundingClientRect().width === 0; });
      assert.ok(data.calls.some(c => c.path.endsWith('/chats/client-0/family')));
      assert.equal(await page.locator('#chat-input').isDisabled(), role === 'observer');
      assert.equal(await page.locator('#chat-handled').isVisible(), role !== 'observer');
      if (role === 'operator') {
        await page.locator('#queue-tab').click();
        assert.equal(await page.locator('[data-chat-view="unread"]').getAttribute('aria-pressed'), 'true');
      }
      if (role === 'owner') {
        await page.locator('#chat-templates').click(); await page.locator('#chat-template-options button').first().click();
        assert.match(await page.locator('#chat-input').inputValue(), /пробное занятие/); assert.equal(data.mutations.length, 0);
        await page.locator('#chat-send').click(); await page.waitForFunction(() => document.querySelectorAll('.chat-bubble').length === 5);
        assert.equal(data.mutations.filter(c => c.path.endsWith('/messages')).length, 1);
        const send = data.mutations[0]; assert.equal(send.csrf, 'synthetic-csrf'); assert.match(send.body.request_id, /^[a-f0-9-]{36}$/); assert.equal(send.body.seen_inbound_seq, 3);
        await page.locator('#chat-handled').click(); await page.waitForFunction(() => document.querySelector('#chat-handled').hidden);
        assert.ok(data.mutations.some(c => c.path.endsWith('/handled')));
      }
      if (role !== 'operator') {
        // An open client card must not follow the operator into another section.
        await page.locator('#chat-person-open').click(); await page.waitForFunction(() => document.querySelector('#chat-details-dialog').open);
        await page.locator('#clients-tab').click(); await page.waitForSelector('#directory-list .contact-row'); await save('installed-clients');
        assert.equal(await page.evaluate(() => document.querySelector('#chat-details-dialog').getBoundingClientRect().width), 0, 'Client card leaked into another section');
        assert.equal(await page.locator('#directory-sync').isVisible(), admin);
        await page.locator('#directory-list .contact-select').first().click(); await page.waitForSelector('#directory-contact-dialog .student-card');
        await page.locator('[data-close="directory-contact-dialog"]').click();
        assert.ok(data.calls.some(c => c.path.endsWith(admin ? '/directory' : '/chat-contacts')));
      }
      if (admin) {
        await page.locator('#team-tab').click(); await page.waitForSelector('#members-list .mrow'); await save('installed-team');
        await page.getByRole('button', { name: 'Действия с сотрудником: Мария Петрова' }).click();
        await page.locator('#member-options-dialog select').selectOption('branch_manager'); await page.getByRole('button', { name: 'Сохранить роль', exact: true }).click();
        await page.waitForFunction(() => !document.querySelector('#member-options-dialog').open);
        assert.ok(data.mutations.some(c => c.path.endsWith('/members/operator-1/role') && c.body.role === 'branch_manager'));
        await page.locator('#connections-tab').click(); await page.locator('#connections-settings-tab').click();
        await page.waitForSelector('#max-channels .mrow'); await save('installed-connections');
        await page.getByRole('button', { name: 'Настройки бота: MAX-бот' }).click();
        await page.getByRole('button', { name: 'Проверить доступ', exact: true }).click();
        await page.waitForFunction(() => !document.querySelector('#bot-actions-dialog').open);
        assert.ok(data.mutations.some(c => c.path.endsWith('/integrations/max/bot/check')));
      }
      await page.locator('#overview-tab').click(); await page.waitForFunction(() => document.querySelector('#organization-branch-count').textContent === '2'); await save('installed-organization');
      await page.locator('#security-tab').click(); await page.waitForSelector('#sessions-list .session-row'); await save('installed-security');
      await page.locator('#profile-tab').click(); await page.locator('#profile-sound').check();
      assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('cm-sound')).enabled), true);
      assert.equal(await page.locator('#pane-profile #chat-branch option').count(), 3, 'Branch filter lives in Profile');
      await page.locator('#chat-branch').selectOption('2'); await page.locator('#messages-tab').click();
      await page.waitForFunction(() => !document.querySelector('#chat-filter-chip').hidden && /Северный/.test(document.querySelector('#chat-filter-chip').textContent));
      assert.ok(data.calls.some(c => c.path.endsWith('/chats') && c.query.includes('branch=2')), 'Profile filter applies to the chat list');
      await page.locator('#chat-filter-chip').click(); await page.waitForFunction(() => document.querySelector('#chat-filter-chip').hidden && !document.querySelector('#chat-branch').value);
      await page.locator('#profile-tab').click(); await save('installed-light-profile');
      if (!admin) {
        await page.locator('#profile-connections').click(); await page.waitForSelector('#channel-info-dialog .split');
        await page.locator('[data-close="channel-info-dialog"]').click();
        assert.ok(!data.calls.some(c => c.path.includes('/integrations/')), 'Staff must not use admin connector endpoints');
        assert.equal(data.mutations.length, 0);
      }
      assert.deepEqual(data.errors, []); assert.deepEqual(data.unexpected, []);
      checked.push({ role, requests: data.calls.length, mutations: data.mutations.length }); await page.close();
    }
    // A member without the messages grant must not receive chat or CRM data.
    const restricted = await browser.newContext(), data = await fixture(restricted, 'observer', ['security']), page = await restricted.newPage();
    page.setDefaultTimeout(10000); await page.goto('https://cabinet.example.test/'); await page.waitForSelector('#sessions-list .session-row');
    assert.equal(await page.locator('#messages-tab').count(), 0); assert.equal(await page.locator('#clients-tab').count(), 0);
    assert.ok(!data.calls.some(c => /\/chats|\/chat-updates|\/summary|\/directory/.test(c.path)));
    assert.deepEqual(data.errors, []); assert.deepEqual(data.unexpected, []);
    console.log(JSON.stringify({ browser: browser.version(), checked, restrictedMember: true }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
