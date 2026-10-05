// Browser test for push-to-talk voice input. Loads the LIVE site with this build of the widget swapped in and talks to
// the live backend. Chromium's fake microphone plays a recorded question ("What did he do at ZEISS?").
//
//   cd evals && uv run python voice_study.py   (once: creates the recordings)
//   cd widget && npm i --no-save playwright && npx playwright install chromium && node e2e/voice.e2e.mjs
import { chromium } from "playwright";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "../..");
const bundle = fs.readFileSync(path.join(here, "../dist/twin-widget.js"), "utf8");
const QUESTION = "What did he do at ZEISS?";
const key = crypto.createHash("sha1").update(`en|en-US-Neural2-D|${QUESTION}`).digest("hex").slice(0, 16);
const wav = path.join(root, "evals/results/voice/clean", `${key}.wav`);
if (!fs.existsSync(wav)) throw new Error("recording missing: run evals/voice_study.py once first");

let failures = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  " + detail : ""}`);
};

async function open({ viewport = { width: 1280, height: 800 }, fakeMic = true } = {}) {
  const args = fakeMic
    ? ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${wav}%noloop`]
    : [];
  const browser = await chromium.launch({ args });
  const ctx = await browser.newContext({ viewport });
  if (fakeMic) await ctx.grantPermissions(["microphone"], { origin: "https://ducanhvalentinonguyen.com" });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.route("**/assets/twin-widget-*.js", (r) => r.fulfill({ contentType: "application/javascript", body: bundle }));
  await page.goto("https://ducanhvalentinonguyen.com/", { waitUntil: "load" });
  await page.waitForTimeout(1200);
  await page.locator("css=.fab").click();
  await page.waitForTimeout(500);
  return { browser, page, errors };
}
const $ = (page, sel) => page.locator(`css=${sel}`);

// 1) The happy path: tap, speak, tap, see the transcript and a cited answer.
{
  const { browser, page, errors } = await open();
  check("mic button is visible", await $(page, ".mic").isVisible());
  await $(page, ".mic").click();
  await page.waitForTimeout(400);
  check("recording state shown", (await $(page, ".mic").getAttribute("aria-pressed")) === "true"
    && /Recording/.test(await $(page, ".voice-status").innerText()));
  await page.waitForTimeout(3600); // the question lasts about 2 s, then silence
  const t0 = Date.now();
  await $(page, ".mic").click();
  await page.waitForFunction(() => {
    const host = [...document.querySelectorAll("div")].find((d) => d.shadowRoot?.querySelector(".log"));
    const users = [...host.shadowRoot.querySelectorAll(".msg.user")];
    return users.length && users[users.length - 1].textContent.trim() !== "…";
  }, null, { timeout: 30000 });
  const heard = await $(page, ".msg.user").last().innerText();
  console.log(`      transcript after ${((Date.now() - t0) / 1000).toFixed(1)} s: "${heard}"`);
  check("the transcript replaces the placeholder bubble", /zeiss/i.test(heard));
  await page.waitForSelector(".thumb", { timeout: 60000 }); // the feedback buttons appear once the answer is complete
  const answer = await $(page, ".log .bot").last().innerText();
  check("an answer follows, like for a typed question", answer.length > 40 && !answer.startsWith("…"), answer.slice(0, 70));
  check("with a citation to the source section", (await $(page, ".log .bot").last().locator("css=.cite").count()) > 0);
  check("mic is released afterwards", (await $(page, ".mic").getAttribute("aria-pressed")) === "false");
  await page.screenshot({ path: path.join(here, "shot_voice.png") });
  check("no page errors", errors.length === 0, errors.join(" | "));
  await browser.close();
}

// 2) Tapping twice almost at once is "too short": a friendly message, nothing sent.
{
  const { browser, page } = await open();
  await $(page, ".mic").click();
  await page.waitForTimeout(150);
  await $(page, ".mic").click();
  await page.waitForTimeout(1500);
  check("a too-short recording gets a friendly message", /didn't catch anything/i.test(await $(page, ".log").innerText()));
  check("and nothing is sent", (await $(page, ".msg.user").count()) === 0);
  await browser.close();
}

// 3) Closing the chat while recording discards the recording and releases the microphone.
{
  const { browser, page } = await open();
  await $(page, ".mic").click();
  await page.waitForTimeout(800);
  await $(page, ".close").click();
  await page.waitForTimeout(1200);
  check("closing the chat stops the recording", (await $(page, ".mic").getAttribute("aria-pressed")) === "false");
  check("and sends nothing", (await $(page, ".msg.user").count()) === 0);
  await browser.close();
}

// 4) No microphone or permission: a clear in-chat explanation, no crash.
{
  const { browser, page, errors } = await open({ fakeMic: false });
  await $(page, ".mic").click();
  await page.waitForTimeout(1500);
  check("denied or missing microphone is explained", /can't use the microphone/i.test(await $(page, ".log").innerText()));
  check("and the page stays healthy", errors.length === 0, errors.join(" | "));
  await browser.close();
}

// 5) Phone width: the button is there and the input row still fits.
{
  const { browser, page } = await open({ viewport: { width: 390, height: 800 } });
  const m = await $(page, ".mic").boundingBox();
  const f = await $(page, "form").boundingBox();
  check("mic fits inside the input row on a phone", m && f && m.x >= f.x && m.x + m.width <= f.x + f.width);
  await page.screenshot({ path: path.join(here, "shot_voice_phone.png") });
  await browser.close();
}

console.log(failures ? `\n${failures} check(s) failed` : "\nall checks passed");
process.exit(failures ? 1 : 0);
