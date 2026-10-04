import { chromium } from "playwright";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

// Browser test: loads the LIVE site with this build of the widget swapped in (talks to the live backend).
//   cd widget && npm i --no-save playwright && npx playwright install chromium && node e2e/widget.e2e.mjs
const here = path.dirname(fileURLToPath(import.meta.url));
const WIDGET = "https://twin-agent-api-7yjacf5bma-ey.a.run.app/widget.js";
const bundle = fs.readFileSync(path.join(here, "../dist/twin-widget.js"), "utf8");
// Records what the page does with WebAudio, without needing to hear it.
const spy = () => {
  window.__audio = { created: 0, oscillators: 0, state: null };
  const Orig = window.AudioContext;
  window.AudioContext = class extends Orig {
    constructor(...a) { super(...a); window.__audio.created++; }
    createOscillator() { window.__audio.oscillators++; return super.createOscillator(); }
    get state2() { return super.state; }
  };
};
async function run(label, args, steps) {
  const browser = await chromium.launch({ args });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await ctx.newPage();
  await page.addInitScript(spy);
  await page.route(WIDGET, (r) => r.fulfill({ contentType: "application/javascript", body: bundle }));
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto("https://ducanhvalentinonguyen.com/", { waitUntil: "load" });
  await steps(page);
  console.log(label, "| page errors:", errors.length ? errors : "none");
  return { browser, page };
}
const info = async (page) => page.evaluate(() => {
  const host = [...document.querySelectorAll("div")].find((d) => d.shadowRoot && d.shadowRoot.querySelector(".fab"));
  const fab = host.shadowRoot.querySelector(".fab");
  return { fab: fab.textContent, seen: fab.classList.contains("seen"), dotShown: getComputedStyle(fab.querySelector(".dot")).display !== "none", oscillators: window.__audio.oscillators };
});

// 1) Default policy: autoplay blocked, so no sound until the first user gesture.
let { browser, page } = await run("default autoplay policy", [], async (page) => {
  await page.waitForTimeout(2500);
  console.log("  after load, before any click:", await info(page));
  await page.mouse.click(600, 400);
  await page.waitForTimeout(800);
  console.log("  after first click anywhere  :", await info(page));
  await page.mouse.click(620, 420);
  await page.waitForTimeout(800);
  console.log("  after second click (no repeat):", (await info(page)).oscillators, "oscillators total");
  await page.screenshot({ path: path.join(here, "shot_closed.png") });
});
await browser.close();

// 2) Autoplay allowed: chime on arrival; muted: silent; opening the chat clears the dot.
({ browser, page } = await run("autoplay allowed", ["--autoplay-policy=no-user-gesture-required"], async (page) => {
  await page.waitForTimeout(2500);
  console.log("  chime on arrival:", (await info(page)).oscillators, "oscillators");
}));
await browser.close();

({ browser, page } = await run("muted visitor", ["--autoplay-policy=no-user-gesture-required"], async (page) => {
  await page.evaluate(() => localStorage.setItem("twin-muted", "1"));
  await page.reload({ waitUntil: "load" });
  await page.waitForTimeout(2500);
  console.log("  oscillators when muted:", (await info(page)).oscillators);
}));
await browser.close();

// 3) Real browser smoke test of the widget against the live backend: open, ask, citation, thumbs.
({ browser, page } = await run("open and ask", ["--autoplay-policy=no-user-gesture-required"], async (page) => {
  await page.waitForTimeout(1500);
  const sh = (sel) => page.locator(`css=${sel}`);
  await sh(".fab").click();
  console.log("  dot cleared after opening:", !(await info(page)).dotShown);
  await sh("input").fill("What did he do at ZEISS?");
  await sh("input").press("Enter");
  await page.waitForSelector(".cite", { timeout: 40000 });
  await page.waitForSelector(".thumb", { timeout: 10000 });
  const text = await sh(".log .bot:last-child").innerText();
  console.log("  answer shown:", text.slice(0, 110).replace(/\n/g, " "), "...");
  console.log("  citation chips:", await sh(".log .bot:last-child .cite").count(), "| thumbs:", await sh(".thumb").count());
  await sh(".log .bot:last-child .cite").first().click();
  await page.waitForTimeout(900);
  console.log("  scrolled to section, url hash:", new URL(page.url()).hash);
  await page.screenshot({ path: path.join(here, "shot_chat.png") });
}));
await browser.close();
