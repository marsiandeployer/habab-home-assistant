const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('puppeteer-core');

const BASE_URL = process.env.HABAB_PUBLIC_BASE_URL || 'https://habab-demo.wpmix.net/';
const OUTPUT = path.join(__dirname, 'unified-assistant');
const PANEL_PATH = 'habab-telegram';
const report = { checkedAt: new Date().toISOString(), baseUrl: BASE_URL, cases: [] };

async function findSidebarLink(page) {
  return page.evaluate((panelPath) => {
    function find(root) {
      for (const element of root.querySelectorAll('*')) {
        if (element.id === `sidebar-panel-${panelPath}`) {
          const anchor = element.shadowRoot?.querySelector('a[href]');
          return {
            element,
            anchor,
            title: element.querySelector('[slot="headline"]')?.textContent?.trim(),
            icon: element.querySelector('[slot="start"]')?.getAttribute('icon') ||
              element.querySelector('[slot="start"]')?.icon,
          };
        }
        if (element.shadowRoot) {
          const result = find(element.shadowRoot);
          if (result) return result;
        }
      }
      return undefined;
    }
    const result = find(document);
    if (!result) return undefined;
    return {
      href: result.anchor?.getAttribute('href') || result.element.getAttribute('href') || result.element.href,
      title: result.title,
      icon: result.icon,
    };
  }, PANEL_PATH);
}

async function clickSidebarLink(page) {
  return page.evaluate((panelPath) => {
    function find(root) {
      for (const element of root.querySelectorAll('*')) {
        if (element.id === `sidebar-panel-${panelPath}`) {
          const anchor = element.shadowRoot?.querySelector('a[href]');
          if (anchor) { anchor.click(); return true; }
          element.click();
          return true;
        }
        if (element.shadowRoot && find(element.shadowRoot)) return true;
      }
      return false;
    }
    return find(document);
  }, PANEL_PATH);
}

async function findChatState(page) {
  return page.evaluate(() => {
    const chat = document.querySelector('habab-setup-chat');
    const launcher = chat?.shadowRoot?.querySelector('.launcher');
    if (!chat || !launcher) return undefined;
    const box = chat.getBoundingClientRect();
    const launcherBox = launcher.getBoundingClientRect();
    const style = getComputedStyle(chat);
    return {
      connected: chat.isConnected,
      fixed: style.position === 'fixed',
      launcherVisible: launcherBox.width > 0 && launcherBox.height > 0 &&
        getComputedStyle(launcher).visibility !== 'hidden' &&
        getComputedStyle(launcher).display !== 'none',
      inViewport: box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight,
      launcherLabel: launcher.getAttribute('aria-label'),
      box: { x: box.x, y: box.y, width: box.width, height: box.height },
    };
  });
}

(async () => {
  const profile = fs.mkdtempSync('/tmp/codex-chrome-public-telegram-');
  let browser;
  try {
    fs.mkdirSync(OUTPUT, { recursive: true });
    browser = await puppeteer.launch({
      executablePath: '/snap/bin/chromium',
      headless: true,
      userDataDir: profile,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    });

    for (const [mode, width, height] of [['desktop', 1280, 800], ['mobile', 390, 844]]) {
      const context = await browser.createBrowserContext();
      try {
        const page = await context.newPage();
        await page.setViewport({ width, height, isMobile: mode === 'mobile', hasTouch: mode === 'mobile' });
        const pageErrors = [];
        const telegramRequests = [];
        const addonResponses = [];
        page.on('pageerror', (error) => pageErrors.push(error.message));
        page.on('request', (request) => {
          if (/api\.telegram\.org|habab_telegram\/(configure|pair|unlink)/i.test(request.url())) {
            telegramRequests.push(request.url());
          }
        });
        page.on('response', (response) => {
          if (/\/integrations\/(telegram-setup|telegram-settings|setup-chat)\.js\b/.test(response.url())) {
            addonResponses.push({ status: response.status(), url: response.url() });
          }
        });

        const response = await page.goto(BASE_URL, { waitUntil: 'networkidle2', timeout: 45000 });
        assert.equal(response.status(), 200, `${mode}: demo entrypoint should load`);
        await page.waitForFunction((panelPath) =>
          document.documentElement.dataset.hababDemo === 'true' &&
          !!document.querySelector('ha-demo')?.hass?.panels?.[panelPath],
        { timeout: 30000 }, PANEL_PATH);
        await page.waitForFunction(() => !!document.querySelector('habab-setup-chat')?.shadowRoot?.querySelector('.launcher'),
          { timeout: 30000 });

        const before = await findChatState(page);
        assert(before?.connected && before.fixed && before.launcherVisible && before.inViewport,
          `${mode}: global assistant launcher should be fixed, visible, and in the viewport`);
        const chatElement = await page.evaluateHandle(() => document.querySelector('habab-setup-chat'));
        await page.evaluate(() => { window.__publicDemoChatBeforeRoute = document.querySelector('habab-setup-chat'); });

        const sidebar = await findSidebarLink(page);
        assert(sidebar, `${mode}: Telegram should have a sidebar entry`);
        assert.equal(sidebar.href, `/${PANEL_PATH}`);
        assert.equal(sidebar.title, 'Telegram');
        const clicked = await clickSidebarLink(page);
        assert(clicked, `${mode}: sidebar entry should be clickable`);
        await page.waitForFunction((panelPath) =>
          document.querySelector('ha-demo')?.hass?.panelUrl === panelPath,
        { timeout: 15000 }, PANEL_PATH);
        await page.waitForFunction(() => {
          function find(root) {
            if (root.querySelector('habab-telegram-demo-panel')) return true;
            return [...root.querySelectorAll('*')].some((element) => element.shadowRoot && find(element.shadowRoot));
          }
          return find(document);
        }, { timeout: 20000 });
        await new Promise((resolve) => setTimeout(resolve, 250));

        const panel = await page.evaluate(() => {
          function find(root) {
            for (const element of root.querySelectorAll('*')) {
              if (element.localName === 'habab-telegram-demo-panel') return element;
              if (element.shadowRoot) {
                const nested = find(element.shadowRoot);
                if (nested) return nested;
              }
            }
            return undefined;
          }
          const element = find(document);
          const root = element?.shadowRoot;
          return {
            text: root?.textContent || '',
            heading: root?.querySelector('h2')?.textContent?.trim(),
            hasTokenInput: !!root?.querySelector('input, textarea, [type="password"]'),
            actionButtons: [...(root?.querySelectorAll('button') || [])].map((button) => button.textContent.trim()).filter(Boolean),
          };
        });
        assert.equal(panel.heading, 'Подключение Telegram');
        assert(panel.text.includes('Подключение бота доступно в домашней установке'),
          `${mode}: panel should explain that bot setup belongs to a home installation`);
        assert(panel.text.includes('не запускает реального бота'),
          `${mode}: panel should identify the public demo as read-only`);
        assert.equal(panel.hasTokenInput, false, `${mode}: public demo must not have a token field`);
        assert.deepEqual(panel.actionButtons, [], `${mode}: read-only panel must not offer a fake connect action`);

        const screenshot = path.join(OUTPUT, `public-telegram-${mode}.png`);
        await page.screenshot({ path: screenshot });

        let mobileBackgroundTap;
        if (mode === 'mobile') {
          mobileBackgroundTap = await page.evaluate(() => {
            function elements(root) {
              const found = [];
              for (const element of root.querySelectorAll('*')) {
                found.push(element);
                if (element.shadowRoot) found.push(...elements(element.shadowRoot));
              }
              return found;
            }
            const menu = elements(document).find((element) => element.localName === 'ha-menu-button');
            const chat = document.querySelector('habab-setup-chat');
            const box = menu?.getBoundingClientRect();
            window.__hababMenuToggleCount = 0;
            document.addEventListener('hass-toggle-menu', () => window.__hababMenuToggleCount++);
            return {
              found: !!menu,
              center: box ? { x: box.left + box.width / 2, y: box.top + box.height / 2 } : undefined,
              closed: !chat?.shadowRoot?.querySelector('dialog')?.open,
              hostPointerEvents: chat ? getComputedStyle(chat).pointerEvents : undefined,
            };
          });
          assert(mobileBackgroundTap.found, 'mobile: Telegram panel should expose the native Home Assistant menu control');
          assert(mobileBackgroundTap.closed, 'mobile: chat should remain closed during the background interaction check');
          assert.equal(mobileBackgroundTap.hostPointerEvents, 'none', 'mobile: closed chat host must let pointer input pass through');
          await page.touchscreen.tap(mobileBackgroundTap.center.x, mobileBackgroundTap.center.y);
          await page.waitForFunction(() => window.__hababMenuToggleCount > 0, { timeout: 5000 });
          mobileBackgroundTap.reachedHomeAssistant = true;
        }

        const after = await page.evaluate(() => {
          const chat = document.querySelector('habab-setup-chat');
          return {
            count: document.querySelectorAll('habab-setup-chat').length,
            sameElement: chat === window.__publicDemoChatBeforeRoute,
            hasOldTelegramLauncher: !!document.querySelector('habab-telegram-setup'),
          };
        });
        assert.equal(after.count, 1, `${mode}: exactly one global assistant element should remain`);
        assert.equal(after.sameElement, true, `${mode}: route navigation must preserve the existing chat session element`);
        assert.equal(after.hasOldTelegramLauncher, false, `${mode}: no floating Telegram launcher should remain`);
        const afterChat = await findChatState(page);
        assert(afterChat?.connected && afterChat.fixed && afterChat.launcherVisible && afterChat.inViewport,
          `${mode}: global assistant launcher should survive route navigation`);
        assert.deepEqual(telegramRequests, [], `${mode}: the public demo must make no Telegram requests`);
        assert.deepEqual(pageErrors, [], `${mode}: page must have no uncaught JavaScript errors`);
        assert(addonResponses.length >= 2 && addonResponses.every((item) => item.status === 200),
          `${mode}: all downstream extension scripts should load successfully`);

        report.cases.push({
          mode,
          passed: true,
          sidebarTitle: sidebar.title,
          panelHeading: panel.heading,
          tokenInput: panel.hasTokenInput,
          fakeConnectAction: panel.actionButtons.length > 0,
          oldTelegramLauncher: after.hasOldTelegramLauncher,
          assistantLauncherSurvivesRoute: after.sameElement && afterChat.launcherVisible,
          mobileBackgroundTap,
          telegramRequests: telegramRequests.length,
          addonResponses,
          screenshot,
          pageErrors,
        });
        await chatElement.dispose();
      } finally {
        await context.close();
      }
    }
    report.passed = true;
  } catch (error) {
    report.passed = false;
    report.error = error.stack;
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    fs.rmSync(profile, { recursive: true, force: true });
    const snapProfile = path.join('/tmp/snap-private-tmp/snap.chromium/tmp', path.basename(profile));
    if (fs.existsSync(snapProfile) && !fs.lstatSync(snapProfile).isSymbolicLink()) {
      fs.rmSync(snapProfile, { recursive: true, force: true });
    }
    fs.writeFileSync(path.join(OUTPUT, 'public-browser-result.json'), `${JSON.stringify(report, null, 2)}\n`);
  }
  console.log(JSON.stringify(report, null, 2));
})();
