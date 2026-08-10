import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../../src/echoweave/web/index.html", import.meta.url), "utf8");
const app = await readFile(new URL("../../src/echoweave/web/app.js", import.meta.url), "utf8");

const pipelineMarkup = html.match(/<div id="pipelineStages"[\s\S]*?<\/div>\s*<\/div>/)?.[0] || "";
for (const modelName of ["Silero", "Qwen", "VoxCPM", "SoulX"]) {
  assert.ok(!pipelineMarkup.includes(modelName), `pipeline must not claim ${modelName} before handshake`);
}
assert.match(app, /renderRuntimeBackends\(event\.runtime\?\.backends \|\| \{\}\)/);

const htmlIds = [...html.matchAll(/\bid="([A-Za-z][\w:-]*)"/g)].map((match) => match[1]);
assert.equal(new Set(htmlIds).size, htmlIds.length, "HTML ids must be unique");

const selectorIds = [...app.matchAll(/\$\("#([A-Za-z][\w:-]*)"\)/g)].map(
  (match) => match[1],
);
const htmlIdSet = new Set(htmlIds);
for (const id of new Set(selectorIds)) {
  assert.ok(htmlIdSet.has(id), `app.js requires missing HTML id #${id}`);
}

for (const requiredId of [
  "networkQuality",
  "networkRtt",
  "turnLatency",
  "firstTokenLatency",
  "pipelineStages",
  "capabilityList",
  "sessionSequence",
  "recoveryAction",
  "activityLog",
  "transcriptAnnouncements",
]) {
  assert.ok(htmlIdSet.has(requiredId), `observability contract is missing #${requiredId}`);
}

assert.match(html, /SYNTHETIC MEDIA · ECHOWEAVE/);
assert.match(html, /合成身份已披露/);
assert.match(html, /你将与 AI 生成的数字分身对话/);
assert.match(html, /class="hero-cta" href="#session-control-title"/);
assert.ok((html.match(/AI 数字分身/g) || []).length >= 2, "AI identity must remain persistently disclosed");
assert.match(html, /\/styles\.css\?v=0\.2\.0/);
assert.match(html, /\/app\.js\?v=0\.2\.0/);
assert.match(app, /mic-worklet\.js\?v=0\.2\.0/);
assert.match(html, /id="accessTokenInput"[\s\S]*?maxlength="4096"/);
assert.match(html, /id="accessTokenInput"[\s\S]*?autocomplete="off"/);
assert.match(app, /const MAX_SESSION_TOKEN_CHARS = 4096;/);
assert.match(app, /accessTokenInput\.value\.length > MAX_SESSION_TOKEN_CHARS/);
assert.match(app, /requestAnimationFrame\?\.bind\(window\)/);
assert.match(app, /currentAssistantParts\.join\(""\)/);
assert.match(app, /remaining <= 72/);
assert.match(app, /prefers-reduced-motion: reduce/);
assert.match(app, /reducedMotionPreference\?\.matches/);
assert.match(html, /id="activityLog"[^>]+role="log"[^>]+aria-live="off"/);
assert.doesNotMatch(html, /class="latency-grid"[^>]+aria-live/);
assert.doesNotMatch(html, /class="stage-telemetry"[^>]+aria-live/);
assert.doesNotMatch(html, /<(?:script|link)[^>]+(?:src|href)="https?:\/\//i);
