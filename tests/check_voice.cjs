// Voice input and AI agent settings against intercepted synthetic API replies.
// Chromium's fake microphone plays a tone; nothing reaches Yandex.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
const shots = process.env.CM_TEST_SCREENSHOTS;
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--autoplay-policy=user-gesture-required',
      ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
  const save = async (page, name) => { if (shots) { fs.mkdirSync(shots, { recursive: true }); await page.screenshot({ path: path.join(shots, name + '.png') }); } };
  const visible = (page, selector) => page.locator(selector).isVisible();
  try {
    // Desktop: the microphone sits left of "send"; dictated text lands in the field.
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, permissions: ['microphone'] });
    const data = await fixture(context, 'owner', ['overview', 'messages', 'security', 'connections'], { voice: true });
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    await page.goto('https://cabinet.example.test/?view=messages'); await page.waitForSelector('.chat-bubble');
    await page.waitForSelector('#chat-voice:not([hidden])');
    const order = await page.evaluate(() => {
      const mic = document.querySelector('#chat-voice').getBoundingClientRect(), send = document.querySelector('#chat-send').getBoundingClientRect();
      return { left: mic.right <= send.left + 1, sameRow: Math.abs((mic.top + mic.bottom) - (send.top + send.bottom)) < 4 };
    });
    assert.ok(order.left && order.sameRow, 'mic must sit left of send on one row');
    await page.locator('#chat-input').fill('Добрый день.');
    await page.locator('#chat-voice').click();
    await page.waitForSelector('#chat-voice-panel:not([hidden])[data-phase="recording"]');
    assert.equal(await visible(page, '#chat-send'), false, 'send is hidden while recording');
    await page.waitForTimeout(1600); await save(page, 'voice-recording');
    await page.locator('#chat-voice-done').click();
    await page.waitForFunction(() => document.querySelector('#chat-input').value.includes('Завтра занятие'));
    assert.equal(await page.locator('#chat-input').inputValue(), 'Добрый день. Здравствуйте! Завтра занятие в 17:00.');
    assert.equal(data.ai.transcribe.length, 1);
    const bytes = Buffer.from(data.ai.transcribe[0].audio, 'base64').length;
    assert.equal(bytes % 2, 0, 'Int16 samples');
    assert.ok(bytes > 32000 * 1.2 && bytes < 32000 * 3, `about 1.6 s of 16 kHz audio, got ${bytes} bytes`);
    assert.equal(await visible(page, '#chat-voice-panel'), false);
    assert.equal(await visible(page, '#chat-send'), true);

    // Cancel drops the recording without a request.
    await page.locator('#chat-voice').click(); await page.waitForSelector('#chat-voice-panel[data-phase="recording"]');
    await page.waitForTimeout(500); await page.locator('#chat-voice-cancel').click();
    await page.waitForSelector('#chat-voice-panel', { state: 'hidden' });
    assert.equal(data.ai.transcribe.length, 1);

    // A failed request keeps the audio for "Повторить".
    data.ai.failNext = 1; await page.locator('#chat-input').fill('');
    await page.locator('#chat-voice').click(); await page.waitForSelector('#chat-voice-panel[data-phase="recording"]');
    await page.waitForTimeout(800); await page.locator('#chat-voice-done').click();
    await page.waitForSelector('#chat-voice-panel[data-phase="error"]');
    assert.match(await page.locator('#chat-voice-label').textContent(), /недоступен/);
    await save(page, 'voice-error');
    await page.locator('#chat-voice-retry').click();
    await page.waitForFunction(() => document.querySelector('#chat-input').value === 'Здравствуйте! Завтра занятие в 17:00.');
    assert.equal(data.ai.transcribe.length, 3);
    assert.equal(data.ai.transcribe[1].audio, data.ai.transcribe[2].audio, 'retry resends the same recording');

    // Connections → service settings: key, agent, test.
    await page.locator('#connections-tab').click(); await page.locator('#connections-settings-tab').click();
    await page.waitForSelector('#ai-cloud-card'); await page.locator('#ai-cloud-card').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('#ai-status').textContent(), 'Работает');
    assert.match(await page.locator('#ai-settings-form [name=api_key]').getAttribute('placeholder'), /k3y9/);
    await page.locator('#ai-settings-form [name=api_key]').fill('AQVN' + 'x'.repeat(36));
    await page.locator('#ai-settings-form [name=monthly_limit_rub]').fill('500');
    await page.locator('#ai-settings-form button[type=submit]').click();
    await page.waitForFunction(() => document.querySelector('#ai-spent').textContent.includes('500'));
    const put = data.mutations.find(m => m.method === 'PUT' && m.path.endsWith('/ai/settings'));
    assert.deepEqual({ ...put.body, api_key: put.body.api_key.length }, { folder_id: 'b1gexamplefolder0000', api_key: 40, voice_enabled: true, monthly_limit_rub: 500 });
    assert.equal(await page.locator('#ai-settings-form [name=api_key]').inputValue(), '', 'the key is not kept in the form');
    await page.locator('#ai-verify').click(); await page.waitForFunction(() => document.querySelector('#ai-status').textContent === 'Работает');

    // DeepSeek: its own key, then its models appear without the "no key" mark.
    assert.match(await page.locator('#ai-agent-form [name=model]').innerHTML(), /DeepSeek — ключ не добавлен/);
    await page.locator('#ai-deepseek-form [name=api_key]').fill('sk-' + 'd'.repeat(32));
    await page.locator('#ai-deepseek-form button[type=submit]').click();
    await page.waitForFunction(() => document.querySelector('#ai-deepseek-form [name=api_key]').placeholder.includes('dddd'));
    const dsPut = data.mutations.find(m => m.method === 'PUT' && m.path.endsWith('/ai/deepseek'));
    assert.deepEqual(dsPut.body, { api_key: 'sk-' + 'd'.repeat(32) });
    await page.locator('#ai-deepseek-verify').click(); await page.waitForFunction(() => document.querySelector('#ai-deepseek-status').textContent === 'Работает');
    assert.doesNotMatch(await page.locator('#ai-agent-form [name=model]').innerHTML(), /ключ не добавлен/);
    await page.locator('#ai-deepseek-card').scrollIntoViewIfNeeded(); await save(page, 'ai-deepseek');
    await page.locator('#ai-agent-new').click(); await page.waitForSelector('#ai-agent-form:not([hidden])');
    await page.locator('#ai-agent-form [name=model]').selectOption('deepseek-flash');
    assert.equal(await page.locator('#ai-agent-form [name=prompt]').inputValue(), 'Нормализуй текст, убери слова-паразиты.');
    assert.equal(await page.locator('#ai-agent-form [name=is_default]').isChecked(), true, 'the first agent becomes the main one');
    await page.locator('#ai-agent-sample').fill('ну это завтра короче в пять');
    await page.locator('#ai-agent-test').click(); await page.waitForSelector('#ai-agent-result:not([hidden])');
    assert.match(await page.locator('#ai-agent-result').textContent(), /17:00/);
    await save(page, 'ai-agent-form');
    await page.locator('#ai-agent-form button[type=submit]').click();
    await page.waitForSelector('#ai-agent-list .ai-agent'); await page.waitForSelector('#ai-agent-form', { state: 'hidden' });
    assert.match(await page.locator('#ai-agent-list').textContent(), /Основной/);
    assert.equal(data.mutations.find(m => m.method === 'POST' && m.path.endsWith('/ai/agents')).body.model, 'deepseek-flash');
    assert.equal(data.mutations.find(m => m.path.endsWith('/ai/agents/test')).body.model, 'deepseek-flash');
    await page.locator('#ai-agent-list .ai-agent').click(); await page.locator('#ai-agent-versions summary').click();
    await page.waitForSelector('#ai-agent-version-list li');
    await save(page, 'ai-settings');
    assert.deepEqual(data.errors, []); assert.deepEqual(data.unexpected, []);
    // Keep the desktop context alive until browser.close (single-process CI Chromium).

    // Phone: with an empty field the round button is the microphone, with text it is "send".
    const phoneContext = await browser.newContext({ viewport: { width: 390, height: 844 }, permissions: ['microphone'], isMobile: true, hasTouch: true });
    const phoneData = await fixture(phoneContext, 'operator', ['messages'], { voice: true });
    const phone = await phoneContext.newPage(); phone.setDefaultTimeout(10000);
    await phone.goto('https://cabinet.example.test/?view=messages'); await phone.locator('.chat-select').first().click();
    await phone.waitForFunction(() => document.querySelector('#app').dataset.view === 'chat');
    await phone.locator('#chat-input').fill('');
    await phone.waitForSelector('#chat-voice:not([hidden])');
    assert.equal(await visible(phone, '#chat-send'), false); assert.equal(await visible(phone, '#chat-voice'), true);
    await phone.waitForTimeout(500); await save(phone, 'voice-phone-empty');
    await phone.locator('#chat-input').fill('Текст');
    assert.equal(await visible(phone, '#chat-send'), true); assert.equal(await visible(phone, '#chat-voice'), false);
    await phone.locator('#chat-input').fill('');
    await phone.locator('#chat-voice').click(); await phone.waitForSelector('#chat-voice-panel[data-phase="recording"]');
    await phone.waitForTimeout(700); await save(phone, 'voice-phone-recording');
    const panel = await phone.locator('#chat-voice-panel').boundingBox();
    assert.ok(panel.x >= 0 && panel.x + panel.width <= 390, 'the panel fits the phone width');
    await phone.locator('#chat-voice-done').click();
    await phone.waitForFunction(() => document.querySelector('#chat-input').value.length > 0);
    assert.equal(await visible(phone, '#chat-send'), true);

    // Hold and release only fills the ordinary input. A client receives nothing
    // until the operator explicitly presses the existing Send button.
    const sent = () => phoneData.mutations.filter(m => m.path.endsWith('/messages'));
    const holdMic = async ms => {
      const box = await phone.locator('#chat-voice').boundingBox();
      await phone.mouse.move(box.x + box.width / 2, box.y + box.height / 2); await phone.mouse.down();
      await phone.waitForSelector('#voice-hold[data-phase="recording"]'); await phone.waitForTimeout(ms);
      return box;
    };
    const draftReady = async text => {
      await phone.waitForFunction(text => document.querySelector('#chat-input').value === text, text);
      await phone.waitForSelector('#chat-voice-panel', {state:'hidden'});
      assert.equal(await phone.locator('#voice-hold').isVisible(), false);
      assert.equal(await phone.locator('.voice-hold-card,.voice-hold-bar').count(), 0);
      assert.equal(await phone.evaluate(() => document.documentElement.classList.contains('voice-holding')), false);
    };
    const recognised = 'Здравствуйте! Завтра занятие в 17:00.';
    await phone.locator('#chat-input').fill(''); await phone.waitForSelector('#chat-voice:not([hidden])');
    const before = sent().length, heard = phoneData.ai.transcribe.length;
    await holdMic(1000); await save(phone, 'voice-hold-recording'); await phone.mouse.up();
    await draftReady(recognised); await save(phone, 'voice-manual-draft');
    await phone.waitForTimeout(8000); assert.equal(sent().length, before, 'hold never sends on a timer');
    assert.equal(phoneData.ai.transcribe.length, heard + 1);
    await phone.locator('#chat-send').tap();
    await phone.waitForFunction(() => !document.querySelector('#chat-input').value);
    assert.equal(sent().length, before + 1, 'only the explicit send button sends');
    assert.equal(sent().at(-1).body.text, recognised);

    // Slide up cancels capture; a downward drag still leaves an ordinary draft.
    await phone.waitForSelector('#chat-voice:not([hidden])');
    const box = await holdMic(600);
    await phone.mouse.move(box.x + box.width / 2, box.y - 140);
    await phone.waitForSelector('#voice-hold[data-phase="cancel"]'); await save(phone, 'voice-hold-cancel');
    await phone.mouse.up(); await phone.waitForSelector('#voice-hold', {state:'hidden'});
    assert.equal(phoneData.ai.transcribe.length, heard + 1); assert.equal(sent().length, before + 1);

    // Provider fallback stays in the input and adds no persistent service notice.
    phoneData.ai.fallbackNext = 1; await holdMic(800); await phone.mouse.up();
    await draftReady('ну это здравствуйте завтра занятие в пять');
    assert.equal(sent().length, before + 1);
    await phone.locator('#chat-input').fill('');

    // Real touch on the stream: taps and swipes do not start recording.
    const cdp = await phoneContext.newCDPSession(phone);
    const touchAt = async (type, spot) => cdp.send('Input.dispatchTouchEvent', {type, touchPoints: spot ? [spot] : []});
    const bubble = await phone.locator('.chat-bubble').nth(1).boundingBox();
    const spot = {x:bubble.x + bubble.width/2, y:bubble.y + bubble.height/2};
    const listHeard = phoneData.ai.transcribe.length;
    await touchAt('touchStart',spot); await phone.waitForTimeout(100); await touchAt('touchEnd'); await phone.waitForTimeout(600);
    assert.equal(await phone.locator('#voice-hold').isVisible(), false);
    assert.equal(phoneData.ai.transcribe.length,listHeard);
    await touchAt('touchStart',spot); await touchAt('touchMove',{x:spot.x,y:spot.y+35}); await touchAt('touchEnd'); await phone.waitForTimeout(600);
    assert.equal(await phone.locator('#voice-hold').isVisible(),false);

    await touchAt('touchStart',spot); await phone.waitForSelector('#voice-hold[data-phase="recording"]'); await phone.waitForTimeout(800);
    assert.equal(await phone.evaluate(() => document.documentElement.classList.contains('voice-holding')),true);
    await save(phone,'voice-hold-list'); await touchAt('touchEnd'); await draftReady(recognised);
    assert.equal(sent().length,before + 1,'stream hold only fills draft');
    assert.equal(phoneData.ai.transcribe.length,listHeard + 1);

    // The orb still follows a finger; downward motion does not introduce another
    // result mode or automatic sending, upward motion discards the recording.
    await phone.locator('#chat-input').fill(''); await phone.waitForTimeout(300);
    const dragBox=await phone.locator('.chat-bubble').nth(1).boundingBox();
    const from={x:dragBox.x+dragBox.width/2,y:dragBox.y+dragBox.height/2};
    await touchAt('touchStart',from); await phone.waitForSelector('#voice-hold[data-phase="recording"]'); await phone.waitForTimeout(600);
    const orbStill=await phone.locator('.voice-hold-orb').boundingBox();
    for(let step=1;step<=5;step++){await touchAt('touchMove',{x:from.x,y:from.y+step*18});await phone.waitForTimeout(30);}
    assert.ok((await phone.locator('.voice-hold-orb').boundingBox()).y > orbStill.y+40);
    await touchAt('touchEnd'); await draftReady(recognised);assert.equal(sent().length,before+1);
    await phone.locator('#chat-input').fill(''); await phone.waitForTimeout(300);
    const heardBefore=phoneData.ai.transcribe.length;
    await touchAt('touchStart',from);await phone.waitForSelector('#voice-hold[data-phase="recording"]');await phone.waitForTimeout(500);
    for(let step=1;step<=6;step++){await touchAt('touchMove',{x:from.x,y:from.y-step*22});await phone.waitForTimeout(30);}
    await phone.waitForSelector('#voice-hold[data-phase="cancel"]');await touchAt('touchEnd');await phone.waitForSelector('#voice-hold',{state:'hidden'});
    assert.equal(phoneData.ai.transcribe.length,heardBefore);

    // «Очистить» in the composer clears the field; hidden while it is empty.
    assert.equal(await phone.locator('#chat-clear').isHidden(), true);
    const clipEmpty = await phone.locator('#chat-attachment').boundingBox(), bolt = await phone.locator('#chat-templates').boundingBox();
    assert.ok(Math.abs((clipEmpty.y + clipEmpty.height / 2) - (bolt.y + bolt.height / 2)) < 1, 'paperclip opposite the bolt');
    await phone.locator('#chat-input').fill('Черновик');
    await phone.waitForSelector('#chat-clear:not([hidden])'); await save(phone, 'composer-clear-short');
    const clipShort = await phone.locator('#chat-attachment').boundingBox();
    assert.deepEqual(clipShort, clipEmpty, '«Очистить» does not move the paperclip');
    await phone.locator('#chat-input').fill('Черновик, который не нужен, и ещё одна строка текста');
    await phone.waitForSelector('#chat-clear:not([hidden])'); await save(phone, 'composer-clear');
    const clearBox = await phone.locator('#chat-clear').boundingBox(), clipBox = await phone.locator('#chat-attachment').boundingBox();
    // On a phone it is a small ✕ at the top of the field, right above the paperclip,
    // and it does not narrow the text.
    assert.ok(clearBox.width <= 26 && clearBox.height <= 26, 'a small ✕ on the phone');
    assert.ok(Math.abs((clearBox.x + clearBox.width / 2) - (clipBox.x + clipBox.width / 2)) < 2, '✕ above the paperclip');
    assert.ok(clearBox.y + clearBox.height <= clipBox.y + 2, '✕ is above, not beside');
    const fit = await phone.evaluate(() => { const t = document.querySelector('#chat-input'); return t.scrollHeight - t.clientHeight; });
    assert.ok(fit <= 1, 'no line of the text is cut off');
    await phone.locator('#chat-clear').tap();
    assert.equal(await phone.locator('#chat-input').inputValue(), '');
    await phone.waitForSelector('#chat-clear', { state: 'hidden' });
    await phone.waitForSelector('#chat-voice:not([hidden])');

    // A short tap still opens the old recording panel.
    await phone.locator('#chat-input').fill(''); await phone.waitForSelector('#chat-voice:not([hidden])');
    await phone.locator('#chat-voice').tap(); await phone.waitForSelector('#chat-voice-panel[data-phase="recording"]');
    assert.equal(await phone.locator('#voice-hold').isHidden(), true);
    await phone.locator('#chat-voice-cancel').click();

    assert.deepEqual(phoneData.errors, []); assert.deepEqual(phoneData.unexpected, []);
    // Contexts close together with the browser (single-process test runner).

    // Observers read only: no microphone. Voice off in the workspace: no microphone either.
    for (const [role, voice] of [['observer', true], ['owner', false]]) {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      const d = await fixture(ctx, role, ['messages'], { voice });
      const p = await ctx.newPage(); await p.goto('https://cabinet.example.test/?view=messages'); await p.waitForSelector('.chat-bubble');
      await p.waitForTimeout(300);
      assert.equal(await visible(p, '#chat-voice'), false, `${role}/${voice}: no mic`);
      assert.equal(d.calls.some(c => c.path.endsWith('/voice/transcribe')), false);
      assert.deepEqual(d.errors, []); assert.deepEqual(d.unexpected, []);
      // Keep context alive until browser.close.
    }
    console.log(JSON.stringify({ browser: browser.version(), order, bytes, ok: true }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
