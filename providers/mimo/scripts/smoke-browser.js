const { chromium } = require('playwright-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
const stealth = StealthPlugin();
stealth.enabledEvasions.delete('user-agent-override');
chromium.use(stealth);

(async () => {
  const proxy = process.env.SMOKE_PROXY || 'http://127.0.0.1:7897';
  const url = process.env.SMOKE_URL || 'https://account.xiaomi.com/';
  console.log('launch proxy=', proxy, 'url=', url);
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
    proxy: { server: proxy },
  }).catch(async () => {
    return chromium.launch({
      headless: true,
      channel: 'chrome',
      args: ['--no-sandbox'],
      proxy: { server: proxy },
    });
  });
  const page = await browser.newPage();
  const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  console.log('status', resp && resp.status(), 'final', page.url());
  console.log('title', await page.title());
  await page.screenshot({ path: 'output/smoke-xiaomi.png', fullPage: true }).catch(() => {});
  await browser.close();
  console.log('SMOKE_OK');
})().catch((e) => {
  console.error('SMOKE_FAIL', e);
  process.exit(1);
});
