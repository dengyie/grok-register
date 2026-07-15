/**
 * One-shot MiMo register for pxed (email path).
 * Singapore email form → Geetest slide → OTP (tinyhost, multi-round) → platform API key.
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const { generateTempMailAddress: rawGenerateTempMailAddress } = require('../dist/temp-mail');

const BAD_DOMAIN_RE = /(infinityfree|000\.pe|\.\.$|\.\s*$)/i;
const LOCAL_FALLBACK_DOMAINS = ['graphiclens.site', 'sewink.my.id', 'nexorabio.pro.vn', 'kimora.space', 'sasukiez.shop'];

async function generateTempMailAddress() {
  for (let i = 0; i < 8; i++) {
    let email = await rawGenerateTempMailAddress();
    email = String(email || '').trim().replace(/\.+$/, '');
    const [user, domain] = email.split('@');
    if (!user || !domain) continue;
    if (BAD_DOMAIN_RE.test(domain)) continue;
    if (domain.includes('..') || domain.startsWith('.') || domain.endsWith('.')) continue;
    if (!/^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$/i.test(domain)) continue;
    return `${user}@${domain}`;
  }
  // last resort local random on fallback domain
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let local = '';
  for (let i = 0; i < 10; i++) local += chars[Math.floor(Math.random() * chars.length)];
  const dom = LOCAL_FALLBACK_DOMAINS[Math.floor(Math.random() * LOCAL_FALLBACK_DOMAINS.length)];
  return `${local}@${dom}`;
}

const { randomBytes } = require('crypto');

const REG =
  process.env.XIAOMI_REGISTER_URL ||
  'https://account.xiaomi.com/fe/service/register?_locale=en&region=SG&sid=api-platform&_uRegion=SG&callback=https%3A%2F%2Fplatform.xiaomimimo.com%2Fsts%3Ffollowup%3Dhttps%253A%252F%252Fplatform.xiaomimimo.com%252Fconsole%252Fbalance%26sid%3Dapi-platform';

const PROXY = process.env.MIMO_PROXY || 'http://127.0.0.1:7897';
const REGION = process.env.XIAOMI_REGION || 'Singapore';
const TEMP_MAIL_BASE = process.env.TEMP_MAIL_BASE || 'https://tinyhost.shop';
const OUT = path.resolve('output');
fs.mkdirSync(OUT, { recursive: true });

function password() {
  const core = randomBytes(6).toString('base64url').replace(/[^a-zA-Z0-9]/g, 'x').slice(0, 8);
  return `${core}Aa1!`.slice(0, 16);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function isOnMimoPlatform(url) {
  try {
    const u = new URL(url);
    return /(?:^|\.)platform\.xiaomimimo\.com$/i.test(u.hostname);
  } catch {
    return false;
  }
}


async function pollTempMailOTP(emailAddress, maxRetries = 40, newerThan = new Date(), usedCodes = new Set()) {
  const [user, domain] = emailAddress.split('@');
  if (!user || !domain) throw new Error(`Invalid temp mail address: ${emailAddress}`);
  const url = `${TEMP_MAIL_BASE}/api/email/${encodeURIComponent(domain)}/${encodeURIComponent(user)}/?page=1&limit=100`;
  const since = newerThan.getTime() - 15 * 1000;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const response = await fetch(url, {
        headers: { Accept: 'application/json' },
        signal: AbortSignal.timeout(12_000),
      });
      if (!response.ok) {
        console.log(`[otp] tinyhost HTTP ${response.status} (${attempt}/${maxRetries})`);
        await sleep(3000);
        continue;
      }
      const data = await response.json();
      const emails = Array.isArray(data?.emails) ? data.emails : [];
      console.log(`[otp] poll ${attempt}/${maxRetries}: ${emails.length} mail(s)`);
      const xiaomi = emails
        .filter((m) => /xiaomi/i.test(`${m.sender || ''} ${m.subject || ''}`))
        .filter((m) => {
          if (!m.date) return true;
          const t = Date.parse(m.date);
          return Number.isNaN(t) || t >= since;
        })
        .sort((a, b) => (Date.parse(b.date || '') || 0) - (Date.parse(a.date || '') || 0));
      for (const mail of xiaomi) {
        const html = [mail.subject, mail.body, mail.html_body].filter(Boolean).join('\n');
        const normalized = html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
        const match =
          normalized.match(/verification code is\s*(\d{6})/i) ||
          normalized.match(/\b(\d{6})\b/);
        if (match?.[1] && !usedCodes.has(match[1])) {
          console.log(`[otp] found code from mail date=${mail.date || 'n/a'}`);
          return match[1];
        }
      }
    } catch (err) {
      console.log(`[otp] poll error: ${err && err.message ? err.message : err}`);
    }
    await sleep(3000);
  }
  throw new Error('Failed to retrieve OTP from Tinyhost after maximum retries');
}

async function acceptCookies(page) {
  for (let round = 0; round < 3; round++) {
    for (const re of [/accept cookies/i, /accept all/i, /Agree/i]) {
      const b = page.getByRole('button', { name: re });
      if (await b.count()) await b.first().click({ force: true }).catch(() => {});
    }
    await page
      .evaluate(() => {
        for (const el of document.querySelectorAll('[class*="cookie" i], [id*="cookie" i]')) {
          if (el && el.style) el.style.display = 'none';
        }
      })
      .catch(() => {});
    await page.waitForTimeout(200);
  }
}

async function switchRegionEmail(page) {
  await acceptCookies(page);
  await page.locator('#rc-tabs-0-tab-register').click({ timeout: 5000 }).catch(() => {});
  await page.getByText('Sign up', { exact: true }).first().click().catch(() => {});

  const emailVisible = await page.getByRole('textbox', { name: /^Email$/i }).isVisible().catch(() => false);
  if (emailVisible) {
    console.log('[region] already email form');
    return;
  }

  const trigger = page.locator('[role="button"]').filter({ hasText: /Region|China|Singapore/i }).first();
  await trigger.click({ timeout: 15000 });
  await page.waitForTimeout(600);

  const search = page.locator('input[type="search"]:visible').first();
  await search.waitFor({ state: 'visible', timeout: 10000 });
  await search.click();
  await search.fill('');
  await search.type(REGION, { delay: 40 });
  await page.waitForTimeout(600);

  const option = page
    .locator('.rc-virtual-list, .ant-select-dropdown, [class*="dropdown"]')
    .getByText(REGION, { exact: true });
  if (await option.count()) await option.first().click();
  else await page.getByText(REGION, { exact: true }).last().click();
  await page.waitForTimeout(1000);

  await page.getByRole('textbox', { name: /^Email$/i }).waitFor({ state: 'visible', timeout: 20000 });
  console.log('[region] switched to', REGION, 'email form OK');
}

async function fillForm(page, email, pass) {
  console.log('[form] passLen=', pass.length, 'email=', email);
  await acceptCookies(page);

  const emailBox = page.getByRole('textbox', { name: /^Email$/i });
  await emailBox.waitFor({ state: 'visible', timeout: 15000 });
  await emailBox.fill(email, { force: true });

  const pw1 = page
    .getByRole('textbox', { name: /Enter your new password/i })
    .or(page.locator('input[type="password"][aria-label*="new password" i]'))
    .or(page.locator('input[type="password"]').nth(0));
  const pw2 = page
    .getByRole('textbox', { name: /Confirm new password/i })
    .or(page.locator('input[type="password"]').nth(1));
  await pw1.first().fill(pass, { force: true });
  await pw2.first().fill(pass, { force: true });

  await page.evaluate((pwd) => {
    const set = (el, v) => {
      if (!el) return;
      const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
      desc.set.call(el, v);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    };
    const emailEl = document.querySelector('input[aria-label="Email"], input[type="email"]');
    const pws = [...document.querySelectorAll('input[type="password"]')];
    if (emailEl) {
      emailEl.dispatchEvent(new Event('input', { bubbles: true }));
      emailEl.dispatchEvent(new Event('change', { bubbles: true }));
    }
    if (pws[0]) set(pws[0], pwd);
    if (pws[1]) set(pws[1], pwd);
  }, pass);

  const agree = page.getByRole('checkbox', { name: /I've read and agreed|read and agreed/i });
  if (await agree.count()) await agree.first().check({ force: true });
  else await page.locator('input[type="checkbox"]').first().check({ force: true });

  await page.waitForTimeout(600);
  const next = page.getByRole('button', { name: /^Next$/i });
  for (let i = 0; i < 50; i++) {
    if (!(await next.isDisabled().catch(() => true))) break;
    await page.waitForTimeout(400);
  }
  if (await next.isDisabled().catch(() => true)) {
    await page.screenshot({ path: path.join(OUT, 'next-disabled.png'), fullPage: true });
    throw new Error('Next still disabled');
  }
  await next.click({ force: true });
  console.log('[form] Next clicked');
}

async function detectGeetestDistance(page) {
  return page.evaluate(() => {
    const bg = document.querySelector('canvas.geetest_canvas_bg');
    const full = document.querySelector('canvas.geetest_canvas_fullbg');
    const slice = document.querySelector('canvas.geetest_canvas_slice');
    if (!bg) return { ok: false, reason: 'no-bg-canvas' };

    if (full) {
      full.classList.remove('geetest_fade');
      full.style.display = 'block';
      full.style.opacity = '1';
      full.style.visibility = 'visible';
      full.style.pointerEvents = 'none';
    }

    const w = bg.width;
    const h = bg.height;
    const bgd = bg.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, w, h).data;
    let fulld = null;
    if (full && full.width === w && full.height === h) {
      try {
        fulld = full.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, w, h).data;
      } catch {}
    }

    const col = new Float32Array(w);
    let method = 'edge';
    if (fulld) {
      method = 'diff';
      for (let x = 0; x < w; x++) {
        let s = 0;
        let n = 0;
        for (let y = 0; y < h; y++) {
          const i = (y * w + x) * 4;
          const d =
            Math.abs(fulld[i] - bgd[i]) +
            Math.abs(fulld[i + 1] - bgd[i + 1]) +
            Math.abs(fulld[i + 2] - bgd[i + 2]);
          if (d > 40) {
            s += d;
            n++;
          }
        }
        col[x] = s + n * 20;
      }
    } else {
      for (let x = 1; x < w - 1; x++) {
        let s = 0;
        for (let y = Math.floor(h * 0.15); y < Math.floor(h * 0.85); y++) {
          const i = (y * w + x) * 4;
          const bri = 0.299 * bgd[i] + 0.587 * bgd[i + 1] + 0.114 * bgd[i + 2];
          const il = (y * w + (x - 1)) * 4;
          const ir = (y * w + (x + 1)) * 4;
          const bl = 0.299 * bgd[il] + 0.587 * bgd[il + 1] + 0.114 * bgd[il + 2];
          const br = 0.299 * bgd[ir] + 0.587 * bgd[ir + 1] + 0.114 * bgd[ir + 2];
          s += Math.abs(br - bl);
          if (bri < 80) s += 15;
        }
        col[x] = s;
      }
    }

    const sm = new Float32Array(w);
    for (let x = 2; x < w - 2; x++) {
      sm[x] = (col[x - 2] + col[x - 1] * 2 + col[x] * 3 + col[x + 1] * 2 + col[x + 2]) / 9;
    }

    const startX = Math.floor(w * 0.18);
    let bestX = startX;
    let best = -1;
    for (let x = startX; x < w - 10; x++) {
      if (sm[x] > best) {
        best = sm[x];
        bestX = x;
      }
    }
    let leftEdge = bestX;
    const thr = best * 0.55;
    for (let x = bestX; x >= startX; x--) {
      if (sm[x] < thr) {
        leftEdge = x + 1;
        break;
      }
      leftEdge = x;
    }

    let pieceLeft = 0;
    let pieceW = 55;
    if (slice) {
      try {
        const sd = slice
          .getContext('2d', { willReadFrequently: true })
          .getImageData(0, 0, slice.width, slice.height).data;
        let minX = slice.width;
        let maxX = 0;
        for (let y = 0; y < slice.height; y++) {
          for (let x = 0; x < slice.width; x++) {
            if (sd[(y * slice.width + x) * 4 + 3] > 30) {
              if (x < minX) minX = x;
              if (x > maxX) maxX = x;
            }
          }
        }
        if (maxX > minX) {
          pieceLeft = minX;
          pieceW = maxX - minX + 1;
        }
      } catch {}
    }

    const distPx = leftEdge - pieceLeft;
    const cssW = bg.getBoundingClientRect().width || w;
    const scale = cssW / w;
    const track = document.querySelector('.geetest_slider_track');
    const trackW = track ? track.getBoundingClientRect().width : cssW;
    const distanceCss = Math.max(20, Math.min(trackW - 8, distPx * scale));

    return {
      ok: true,
      method,
      bestX,
      leftEdge,
      pieceLeft,
      pieceW,
      distPx,
      canvasW: w,
      canvasH: h,
      cssW,
      trackW,
      scale,
      distanceCss,
      score: best,
    };
  });
}

async function dragGeetestSlider(page, distanceCss) {
  const btn = page.locator('.geetest_slider_button').first();
  await btn.waitFor({ state: 'visible', timeout: 10000 });
  const box = await btn.boundingBox();
  if (!box) throw new Error('slider button no box');
  const track = await page.locator('.geetest_slider_track').boundingBox().catch(() => null);
  const maxTravel = track ? Math.max(30, track.width - 6) : 200;
  const dist = Math.max(20, Math.min(maxTravel, distanceCss));

  const startX = box.x + box.width / 2;
  const startY = box.y + box.height / 2;
  const overshoot = Math.random() * 2.5;
  const target = startX + dist + overshoot;
  const settle = startX + dist;

  await page.mouse.move(startX, startY, { steps: 2 });
  await page.waitForTimeout(50 + Math.random() * 80);
  await page.mouse.down();
  await page.waitForTimeout(40 + Math.random() * 60);

  const steps = 32 + Math.floor(Math.random() * 8);
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    const eased = t * t * (3 - 2 * t);
    const x = startX + (target - startX) * eased;
    const y = startY + Math.sin(t * Math.PI) * (1.2 + Math.random() * 0.8);
    await page.mouse.move(x, y);
    await page.waitForTimeout(10 + Math.random() * 10);
  }
  await page.mouse.move(settle, startY, { steps: 3 });
  await page.waitForTimeout(60 + Math.random() * 80);
  await page.mouse.up();
}

async function isGeetestVisible(page) {
  return page.evaluate(() => {
    const panel = document.querySelector('.geetest_panel_box, .geetest_holder, .geetest_slider');
    if (!panel) return false;
    const r = panel.getBoundingClientRect();
    return r.width > 50 && r.height > 20;
  });
}

async function solveGeetest(page, maxAttempts = 4) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    if (!(await isGeetestVisible(page))) {
      console.log('[geetest] not visible (attempt', attempt, ')');
      return true;
    }
    console.log('[geetest] attempt', attempt);
    await page.screenshot({ path: path.join(OUT, `geetest-before-${attempt}.png`), fullPage: true }).catch(() => {});
    await page.waitForSelector('canvas.geetest_canvas_bg', { timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(600);

    const det = await detectGeetestDistance(page);
    console.log('[geetest] detect', JSON.stringify(det));
    if (!det.ok) throw new Error('geetest detect failed: ' + det.reason);

    const dist = det.distanceCss + (Math.random() * 2 - 1);
    await dragGeetestSlider(page, dist);
    await page.waitForTimeout(1200);

    const state = await page.evaluate(() => {
      const slider = document.querySelector('.geetest_slider');
      const holder = document.querySelector('.geetest_holder');
      const text = document.body.innerText || '';
      return {
        successCls: !!(slider && /geetest_success/.test(slider.className)),
        otp: /enter code|verification code|email verification|account authentication/i.test(text),
        slideText: /slide to complete/i.test(text),
        holderCls: holder ? holder.className : '',
      };
    });
    const still = await isGeetestVisible(page);
    console.log('[geetest] after drag', JSON.stringify(state), 'stillVisible=', still);
    await page.screenshot({ path: path.join(OUT, `geetest-after-${attempt}.png`), fullPage: true }).catch(() => {});

    if (state.successCls || state.otp || !still || !state.slideText) {
      console.log('[geetest] solved');
      await page.waitForTimeout(800);
      return true;
    }

    const refresh = page.locator('.geetest_refresh_1, .geetest_refresh, a[aria-label="Refresh"]').first();
    if (await refresh.count()) {
      await refresh.click({ force: true }).catch(() => {});
      await page.waitForTimeout(900);
    } else {
      const tryAgain = page.getByRole('button', { name: /Try again/i });
      if (await tryAgain.count()) await tryAgain.first().click({ force: true }).catch(() => {});
      await page.waitForTimeout(900);
    }
  }
  return false;
}

async function fillOtpInputs(page, otp) {
  const multi = page.getByRole('textbox', { name: /OTP Input 1/i });
  if (await multi.count()) {
    for (let i = 0; i < otp.length; i++) {
      const box = page.getByRole('textbox', { name: new RegExp(`OTP Input ${i + 1}`, 'i') });
      if (await box.count()) await box.fill(otp[i]);
    }
    return;
  }
  const labeled = page.getByRole('textbox', { name: /code|verification|otp/i });
  if (await labeled.count()) {
    await labeled.first().fill('');
    await labeled.first().type(otp, { delay: 40 });
    return;
  }
  const visible = page.locator(
    'input:not([type=password]):not([type=checkbox]):not([type=hidden]):visible',
  );
  const n = await visible.count();
  if (n === 1) {
    await visible.first().fill('');
    await visible.first().type(otp, { delay: 40 });
    return;
  }
  if (n >= 4 && n <= 8) {
    for (let i = 0; i < Math.min(n, otp.length); i++) {
      await visible.nth(i).fill(otp[i]);
    }
    return;
  }
  await page.keyboard.type(otp, { delay: 40 });
}

async function submitOtpIfPossible(page) {
  for (const name of [/^Submit$/i, /^Confirm$/i, /^Next$/i, /^Continue$/i, /^Verify$/i]) {
    const b = page.getByRole('button', { name });
    if (await b.count()) {
      const dis = await b.first().isDisabled().catch(() => true);
      if (!dis) {
        await b.first().click({ force: true }).catch(() => {});
        return true;
      }
    }
  }
  await page.keyboard.press('Enter').catch(() => {});
  return false;
}

async function pageLooksLikeOtp(page) {
  return page.evaluate(() => {
    const t = document.body.innerText || '';
    return /enter code|verification code|email verification|account authentication|send verification code/i.test(
      t,
    );
  });
}

async function waitOtpAndSubmit(page, email) {
  const started = new Date();
  let solved = false;
  for (let i = 0; i < 90; i++) {
    if (await pageLooksLikeOtp(page)) break;
    if (await isGeetestVisible(page)) {
      if (!solved) {
        const ok = await solveGeetest(page, 5);
        solved = ok;
        if (!ok) {
          await page.screenshot({ path: path.join(OUT, 'geetest-failed.png'), fullPage: true });
          throw new Error('Geetest slide captcha not solved after retries');
        }
        continue;
      }
    }
    if (await page.getByRole('button', { name: /Get code/i }).isVisible().catch(() => false)) {
      if (!(await page.getByRole('textbox', { name: /^Email$/i }).count())) {
        throw new Error('still on phone form');
      }
    }
    await page.waitForTimeout(1000);
  }

  if (!(await pageLooksLikeOtp(page))) {
    await page.screenshot({ path: path.join(OUT, 'otp-wait-timeout.png'), fullPage: true });
    const body = await page.evaluate(() => document.body.innerText.slice(0, 1500));
    throw new Error('OTP page not ready: ' + body.replace(/\n/g, ' | ').slice(0, 400));
  }

  const usedCodes = new Set();
  for (let round = 1; round <= 3; round++) {
    if (isOnMimoPlatform(page.url())) {
      console.log('[otp] already on platform');
      return;
    }
    if (!(await pageLooksLikeOtp(page))) {
      console.log('[otp] no more OTP UI at round', round);
      break;
    }

    let sent = false;
    for (const name of [/^Send$/i, /Send code/i, /Resend/i, /Get code/i]) {
      const b = page.getByRole('button', { name });
      if (await b.count()) {
        const dis = await b.first().isDisabled().catch(() => true);
        if (!dis) {
          console.log('[otp] round', round, 'click', String(name));
          await b.first().click({ force: true }).catch(() => {});
          sent = true;
          await page.waitForTimeout(1200);
          break;
        }
      }
    }

    console.log('[otp] round', round, 'polling tinyhost for', email, 'sent=', sent);
    const pollStart = sent || round > 1 ? new Date() : started;
    const otp = await pollTempMailOTP(
      email,
      Number(process.env.OTP_RETRIES || 40),
      pollStart,
      usedCodes,
    );
    usedCodes.add(otp);
    console.log('[otp] round', round, 'got code');
    await fillOtpInputs(page, otp);
    await submitOtpIfPossible(page);
    await page.waitForTimeout(2500);
    await page.waitForLoadState('domcontentloaded', { timeout: 20000 }).catch(() => {});
    await page
      .screenshot({ path: path.join(OUT, `after-otp-round-${round}.png`), fullPage: true })
      .catch(() => {});

    for (let w = 0; w < 20; w++) {
      const url = page.url();
      if (isOnMimoPlatform(url)) {
        console.log('[otp] landed platform');
        return;
      }
      if (await isGeetestVisible(page)) {
        await solveGeetest(page, 3);
      }
      if (await pageLooksLikeOtp(page)) break;
      await page.waitForTimeout(1000);
    }
  }
}

async function extractApiKey(page) {
  for (let i = 0; i < 30; i++) {
    if (isOnMimoPlatform(page.url())) break;
    if (await pageLooksLikeOtp(page)) {
      throw new Error('still on OTP/auth after rounds: ' + page.url());
    }
    if (await isGeetestVisible(page)) await solveGeetest(page, 2);
    await page.waitForTimeout(1000);
  }

  if (!isOnMimoPlatform(page.url())) {
    await page
      .goto('https://platform.xiaomimimo.com/console/balance', {
        waitUntil: 'domcontentloaded',
        timeout: 45000,
      })
      .catch(() => {});
    await page.waitForTimeout(2500);
  }

  // Prefer English UI if language switch exists
  try {
    const lang = page.getByText(/English|EN|中文/i).first();
    if (await lang.count()) {
      await lang.click({ timeout: 2000 }).catch(() => {});
      await page.getByText(/^English$/i).click({ timeout: 2000 }).catch(() => {});
      await page.waitForTimeout(800);
    }
  } catch {}

  await page.screenshot({ path: path.join(OUT, 'after-otp.png'), fullPage: true });
  console.log('[console] url=', page.url());

  // Dismiss top banners
  for (let i = 0; i < 3; i++) {
    const close = page.locator('.ant-alert-close-icon, button[aria-label="Close"], .anticon-close').first();
    if (await close.count()) await close.click({ force: true }).catch(() => {});
  }

  // Agreement modal: "I agree to use the model..." / 中文协议
  try {
    const agreement = page
      .getByRole('checkbox', { name: /I agree to use the model|同意|协议|使用.*模型/i })
      .or(page.locator('.ant-modal input[type=checkbox], .ant-modal-body input[type=checkbox]'));
    if (await agreement.count()) {
      console.log('[console] checking agreement modal');
      await agreement.first().check({ force: true }).catch(async () => {
        await agreement.first().click({ force: true });
      });
      await page.waitForTimeout(400);
      for (const name of [/^Confirm$/i, /^OK$/i, /^确定$/i, /^确认$/i, /^同意$/i]) {
        const b = page.getByRole('button', { name });
        if (await b.count()) {
          const dis = await b.first().isDisabled().catch(() => false);
          if (!dis) await b.first().click({ force: true }).catch(() => {});
        }
      }
      // fallback: primary button in modal
      const primary = page.locator('.ant-modal .ant-btn-primary:not([disabled])');
      if (await primary.count()) await primary.first().click({ force: true }).catch(() => {});
      await page.waitForTimeout(1000);
    }
  } catch (e) {
    console.log('[console] agreement skip', e.message);
  }

  const invite = process.env.MIMO_INVITE_CODE;
  if (invite) {
    try {
      const btn = page.getByRole('button', { name: /Enter invite code|邀请码/i });
      if (await btn.count()) {
        await btn.click();
        await page.waitForTimeout(500);
        const multi = page.getByRole('textbox', { name: /OTP Input 1/i });
        if (await multi.count()) {
          for (let i = 0; i < invite.length; i++) {
            const box = page.getByRole('textbox', { name: new RegExp(`OTP Input ${i + 1}`, 'i') });
            if (await box.count()) await box.fill(invite[i]);
          }
        } else {
          await page.keyboard.type(invite, { delay: 20 });
        }
        await page
          .getByRole('button', { name: /Redeem|Submit|Confirm|兑换|确认/i })
          .first()
          .click()
          .catch(() => {});
        await page.waitForTimeout(800);
        await page.getByRole('button', { name: /^close$/i }).click().catch(() => {});
      }
    } catch (e) {
      console.log('[invite]', e.message);
    }
  }

  // Navigate to API Keys (EN + ZH)
  let navigated = false;
  for (const name of [/API Keys/i, /API\s*密钥/i, /密钥管理/i, /密钥/i]) {
    const link = page.getByRole('link', { name }).or(page.getByRole('menuitem', { name })).or(page.getByText(name));
    if (await link.count()) {
      console.log('[console] click nav', String(name));
      await link.first().click({ force: true }).catch(() => {});
      await page.waitForTimeout(1500);
      navigated = true;
      break;
    }
  }
  if (!navigated) {
    for (const u of [
      'https://platform.xiaomimimo.com/console/api-keys',
      'https://platform.xiaomimimo.com/#/console/api-keys',
      'https://platform.xiaomimimo.com/console/token',
      'https://platform.xiaomimimo.com/#/console/token',
    ]) {
      console.log('[console] goto', u);
      await page.goto(u, { waitUntil: 'domcontentloaded', timeout: 45000 }).catch(() => {});
      await page.waitForTimeout(2000);
      const t = await page.evaluate(() => document.body.innerText.slice(0, 500));
      if (/API Key|API密钥|创建|密钥/i.test(t)) break;
    }
  }

  // Again: agreement modal may appear on api-keys
  try {
    const boxes = page.locator('.ant-modal input[type=checkbox], input[type=checkbox]');
    const n = await boxes.count();
    for (let i = 0; i < n; i++) {
      const box = boxes.nth(i);
      if (await box.isVisible().catch(() => false)) {
        const checked = await box.isChecked().catch(() => false);
        if (!checked) {
          console.log('[console] check visible checkbox', i);
          await box.check({ force: true }).catch(async () => box.click({ force: true }));
        }
      }
    }
    const primary = page.locator('.ant-modal .ant-btn-primary:not([disabled]), .ant-modal button:not([disabled])');
    // click enabled primary-looking button after checkbox
    for (const name of [/^Confirm$/i, /^OK$/i, /^确定$/i, /^确认$/i, /^创建$/i, /^Create$/i]) {
      const b = page.locator('.ant-modal').getByRole('button', { name });
      if (await b.count()) {
        const dis = await b.first().isDisabled().catch(() => false);
        if (!dis) {
          await b.first().click({ force: true }).catch(() => {});
          await page.waitForTimeout(800);
        }
      }
    }
  } catch {}

  await page.screenshot({ path: path.join(OUT, 'api-keys.png'), fullPage: true });
  const beforeText = await page.evaluate(() => document.body.innerText.slice(0, 2000));
  console.log('[console] api-keys page snippet:', beforeText.replace(/\n/g, ' | ').slice(0, 400));

  // Create API Key
  let created = false;
  for (const name of [
    /^Create API Key$/i,
    /Create API Key/i,
    /Create Key/i,
    /New API Key/i,
    /创建 API Key/i,
    /创建密钥/i,
    /新建密钥/i,
    /创建 API/i,
    /^创建$/i,
    /^Create$/i,
  ]) {
    const b = page.getByRole('button', { name }).or(page.getByText(name));
    if (await b.count()) {
      console.log('[console] click create', String(name));
      await b.first().click({ force: true }).catch(() => {});
      created = true;
      await page.waitForTimeout(1000);
      break;
    }
  }
  if (!created) {
    // try any button containing 密钥/Key
    const any = page.locator('button').filter({ hasText: /API|密钥|Key|创建|Create/i });
    if (await any.count()) {
      console.log('[console] click fallback create-like button');
      await any.first().click({ force: true }).catch(() => {});
      await page.waitForTimeout(1000);
    }
  }

  // Name field
  const nameInput = page
    .getByRole('textbox', { name: /API Key Name|名称|Name/i })
    .or(page.locator('.ant-modal input[type=text], .ant-modal input:not([type])'));
  if (await nameInput.count()) {
    await nameInput.first().fill(`MimoAuto-${Date.now()}`);
  }

  // modal agreement checkbox again (create flow)
  {
    const modalChecks = page.locator('.ant-modal input[type=checkbox]');
    const n = await modalChecks.count();
    for (let i = 0; i < n; i++) {
      const box = modalChecks.nth(i);
      if (await box.isVisible().catch(() => false)) {
        const checked = await box.isChecked().catch(() => false);
        if (!checked) {
          console.log('[console] check create-modal checkbox', i);
          await box.check({ force: true }).catch(async () => box.click({ force: true }));
        }
      }
    }
  }

  for (const name of [/^Confirm$/i, /^Create$/i, /^OK$/i, /^确定$/i, /^确认$/i, /^创建$/i]) {
    const b = page.locator('.ant-modal').getByRole('button', { name }).or(page.getByRole('button', { name }));
    if (await b.count()) {
      // prefer enabled
      const count = await b.count();
      for (let i = 0; i < count; i++) {
        const dis = await b.nth(i).isDisabled().catch(() => false);
        if (!dis && (await b.nth(i).isVisible().catch(() => false))) {
          console.log('[console] confirm', String(name), i);
          await b.nth(i).click({ force: true }).catch(() => {});
          await page.waitForTimeout(500);
        }
      }
    }
  }
  // primary enabled
  const primaryEnabled = page.locator('.ant-modal .ant-btn-primary:not([disabled])');
  if (await primaryEnabled.count()) {
    await primaryEnabled.first().click({ force: true }).catch(() => {});
  }

  await page.waitForTimeout(2500);
  await page.screenshot({ path: path.join(OUT, 'api-keys-after-create.png'), fullPage: true });

  // Extract key from multiple sources
  await page.getByRole('button', { name: /Copy|复制/i }).first().click().catch(() => {});
  await page.waitForTimeout(300);
  const copied = await page
    .evaluate(async () => {
      try {
        return await navigator.clipboard.readText();
      } catch {
        return '';
      }
    })
    .catch(() => '');
  if (copied && /^[A-Za-z0-9_-]{20,}$/.test(copied.trim())) {
    console.log('[console] key from clipboard');
    return copied.trim();
  }

  const body = await page.evaluate(() => document.body.innerText);
  const inputVals = await page.evaluate(() =>
    [...document.querySelectorAll('input, textarea, code, pre')].map((i) => (i.value != null ? i.value : i.textContent || '')).filter(Boolean),
  );
  for (const v of inputVals) {
    const s = String(v).trim();
    if (/^(sk|ak|mi|mimo)[-_A-Za-z0-9]{16,}$/i.test(s) || (/^[A-Za-z0-9_-]{32,}$/.test(s) && !/\s/.test(s))) {
      console.log('[console] key from input/code');
      return s;
    }
  }
  const m =
    body.match(/\b(?:sk|ak|mi|mimo)[-_A-Za-z0-9]{16,}\b/i) ||
    body.match(/\b[A-Za-z0-9_-]{40,}\b/) ||
    body.match(/\b[A-Za-z0-9_-]{32,}\b/);
  if (m) {
    console.log('[console] key from body text');
    return m[0];
  }

  // network: sometimes key in response - dump more context
  await page.screenshot({ path: path.join(OUT, 'api-keys-no-key.png'), fullPage: true });
  throw new Error('API key not found on page\n' + body.slice(0, 1800));
}

(async () => {
  const email = await generateTempMailAddress();
  const pass = password();
  console.log('[start] email=', email, 'proxy=', PROXY, 'region=', REGION);

  const browser = await chromium.launch({
    headless: process.env.HEADLESS !== 'false',
    args: [
      '--no-sandbox',
      '--disable-blink-features=AutomationControlled',
      '--window-size=1280,900',
      '--disable-dev-shm-usage',
    ],
    proxy: { server: PROXY },
  });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    locale: 'en-US',
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
  });
  const page = await context.newPage();

  try {
    await page.goto(REG, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(2000);
    await switchRegionEmail(page);
    await fillForm(page, email, pass);
    await waitOtpAndSubmit(page, email);
    const key = await extractApiKey(page);
    const n = fs.existsSync(path.join(OUT, 'success_keys.txt'))
      ? fs.readFileSync(path.join(OUT, 'success_keys.txt'), 'utf8').split('\n').filter(Boolean).length + 1
      : 1;
    fs.appendFileSync(path.join(OUT, 'success_keys.txt'), `${n}. ${key}\n`);
    fs.appendFileSync(
      path.join(OUT, 'accounts.jsonl'),
      JSON.stringify({ email, password: pass, apiKey: key, at: new Date().toISOString() }) + '\n',
    );
    // Human-safe log (prefix only) + machine-readable this-run result for adapters.
    console.log(JSON.stringify({ status: 'SUCCESS', email, apiKeyPrefix: key.slice(0, 10) + '...' }));
    console.log(
      'RESULT_JSON:' +
        JSON.stringify({
          status: 'SUCCESS',
          email,
          password: pass,
          apiKey: key,
          at: new Date().toISOString(),
        }),
    );
  } catch (e) {
    await page.screenshot({ path: path.join(OUT, 'failed-final.png'), fullPage: true }).catch(() => {});
    fs.appendFileSync(
      path.join(OUT, 'failed_accounts.csv'),
      `${email},${String(e.message || e).replace(/\n/g, ' ')}\n`,
    );
    console.error(
      JSON.stringify({ status: 'FAILED', email, error: String(e.message || e).slice(0, 500) }),
    );
    console.error(
      'RESULT_JSON:' +
        JSON.stringify({
          status: 'FAILED',
          email,
          error: String(e.message || e).slice(0, 500),
        }),
    );
    process.exitCode = 1;
  } finally {
    await browser.close().catch(() => {});
  }
})();
