#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const DEFAULT_SELECTORS = [
  "article",
  "main",
  ".lake-content",
  ".ne-viewer-body",
  ".yuque-doc-content",
  ".doc-reader",
  ".doc-content",
  ".markdown-body",
  "[role='main']",
];

function parseArgs(argv) {
  const args = {
    maxChars: 24000,
    headless: false,
    waitMs: 3000,
    interactiveLogin: false,
    loginTimeoutMs: 180000,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--url") args.url = argv[++i];
    else if (arg === "--profile") args.profile = argv[++i];
    else if (arg === "--max-chars") args.maxChars = Number(argv[++i]);
    else if (arg === "--headless") args.headless = true;
    else if (arg === "--wait-ms") args.waitMs = Number(argv[++i]);
    else if (arg === "--interactive-login") args.interactiveLogin = true;
    else if (arg === "--login-timeout-ms") args.loginTimeoutMs = Number(argv[++i]);
    else if (arg === "--help") args.help = true;
    else throw new Error(`unknown argument: ${arg}`);
  }
  return args;
}

function usage() {
  return `Usage: node scripts/browser_reader.mjs --url <url> --profile <profile-dir> [--max-chars 24000] [--headless] [--interactive-login]`;
}

function normalizeText(text) {
  return String(text || "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/[ \t]+/g, " ")
    .replace(/\n[ \t]+/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function capText(text, maxChars) {
  const normalized = normalizeText(text);
  if (!maxChars || normalized.length <= maxChars) {
    return { text: normalized, clipped: false };
  }
  const headChars = Math.max(1, Math.floor(maxChars * 0.72));
  const tailChars = Math.max(1, maxChars - headChars);
  return {
    text: `${normalized.slice(0, headChars).trimEnd()}\n\n[... content clipped by browser-reader to save tokens ...]\n\n${normalized.slice(-tailChars).trimStart()}`,
    clipped: true,
  };
}

function looksLikeAuthWall(url, title, text) {
  const parsed = new URL(url);
  const joined = `${title}\n${text}`.toLowerCase();
  const loginWords = ["login", "signin", "sign in", "登录", "登陆", "授权", "认证"];
  if (["/login", "/signin", "/passport"].some((part) => parsed.pathname.toLowerCase().includes(part))) {
    return true;
  }
  return text.length < 500 && loginWords.some((word) => joined.includes(word));
}

async function importPlaywright() {
  try {
    return await import("playwright");
  } catch (error) {
    throw new Error("Playwright is not installed. Run `npm install playwright` in this project before using browser mode.");
  }
}

async function launchPersistentBrowser(chromium, profileDir, args) {
  const options = {
    headless: args.headless,
    viewport: { width: 1365, height: 900 },
  };
  try {
    return await chromium.launchPersistentContext(profileDir, {
      ...options,
      channel: "chrome",
    });
  } catch (error) {
    if (String(error?.message || "").toLowerCase().includes("chrome")) {
      return await chromium.launchPersistentContext(profileDir, options);
    }
    throw error;
  }
}

async function extractPage(page, maxChars) {
  const payload = await page.evaluate((selectors) => {
    const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const textFor = (selector) => Array.from(document.querySelectorAll(selector))
      .map((node) => node.innerText || node.textContent || "")
      .map((text) => text.trim())
      .filter((text) => text.length > 80)
      .join("\n\n");
    const candidates = selectors
      .map((selector) => ({ selector, text: textFor(selector) }))
      .filter((item) => item.text.length > 100)
      .sort((a, b) => b.text.length - a.text.length);
    const bodyText = document.body ? document.body.innerText || "" : "";
    const meta = {};
    for (const node of Array.from(document.querySelectorAll("meta"))) {
      const key = node.getAttribute("property") || node.getAttribute("name");
      const value = node.getAttribute("content");
      if (key && value && ["og:title", "article:author", "author", "description"].includes(key)) {
        meta[key] = value;
      }
    }
    return {
      title: document.title || normalize(meta["og:title"]),
      url: location.href,
      candidateSelector: candidates[0] ? candidates[0].selector : "",
      candidateText: candidates[0] ? candidates[0].text : "",
      bodyText,
      metadata: meta,
    };
  }, DEFAULT_SELECTORS);

  const chosenText = payload.candidateText || payload.bodyText || "";
  const capped = capText(chosenText, maxChars);
  const authWall = looksLikeAuthWall(payload.url, payload.title, capped.text);
  return {
    payload,
    capped,
    authWall,
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    console.log(usage());
    return;
  }
  if (!args.url || !args.profile) {
    throw new Error(usage());
  }

  const profileDir = path.resolve(args.profile);
  fs.mkdirSync(profileDir, { recursive: true });

  const { chromium } = await importPlaywright();
  const context = await launchPersistentBrowser(chromium, profileDir, args);

  let page;
  try {
    page = context.pages()[0] || await context.newPage();
    await page.goto(args.url, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(args.waitMs);

    let { payload, capped, authWall } = await extractPage(page, args.maxChars);
    let loginWaited = false;
    if (authWall && args.interactiveLogin && !args.headless) {
      loginWaited = true;
      const deadline = Date.now() + args.loginTimeoutMs;
      while (Date.now() < deadline) {
        await page.waitForTimeout(3000);
        const extracted = await extractPage(page, args.maxChars);
        payload = extracted.payload;
        capped = extracted.capped;
        authWall = extracted.authWall;
        if (!authWall) {
          break;
        }
      }
    }
    const output = {
      title: payload.title || payload.url,
      url: payload.url,
      read_quality: authWall ? "blocked" : "browser",
      strategy: payload.candidateSelector ? `playwright_persistent_profile:${payload.candidateSelector}` : "playwright_persistent_profile:body",
      token_policy: `max_chars=${args.maxChars}; ${capped.clipped ? "clipped_head_tail" : "full_within_budget"}`,
      content: capped.text || "读取结果为空。",
      metadata: {
        ...payload.metadata,
        profile_dir: profileDir,
        final_url: payload.url,
        selected_selector: payload.candidateSelector,
        body_length: payload.bodyText.length,
        interactive_login: args.interactiveLogin,
        login_waited: loginWaited,
      },
      errors: authWall ? ["Page still appears to require login or authorization in the browser profile. Open the profile once and log in."] : [],
    };
    console.log(JSON.stringify(output, null, 2));
  } finally {
    await context.close();
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ error: error.message }, null, 2));
  process.exit(1);
});
