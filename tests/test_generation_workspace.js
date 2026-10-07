"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const file = path.join(__dirname, "../web/generation.js");
const source = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
const models = [
  {id: "short", label: "Short", resolutions: ["480p", "720p"], durations: Array.from({length: 12}, (_, i) => i + 4), default_resolution: "480p", default_segment_duration: 15},
  {id: "long", label: "Long", resolutions: ["720p", "1080p"], durations: Array.from({length: 27}, (_, i) => i + 4), default_resolution: "720p", default_segment_duration: 30},
];
const spec = (name = "Kitchen", count = 4, seconds = 15) => ({name, global_prompt: "First person sorting objects", dataset: {variation: "different cups", task_label: "sorting", objects: ["cup"]}, segments: Array.from({length: count}, (_, index) => ({id: `s${index}`, title: `Part ${index}`, duration: seconds, prompt: "Sort and place the object", expected_start_state: "hand empty", expected_end_state: "object placed", required_actions: ["place"], action_labels: ["place"]}))});
function element(value = "", tagName = "DIV") {
  const events = {};
  return {value, tagName, events, disabled: false, hidden: false, children: [], dataset: {}, style: {}, textContent: "", files: [],
    classList: {add() {}, remove() {}, toggle() {}},
    append(...children) { this.children.push(...children); }, prepend(...children) { this.children.unshift(...children); },
    replaceChildren(...children) { this.children = children; }, get childElementCount() { return this.children.length; },
    setAttribute() {}, removeAttribute() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener(name, fn) { (events[name] ||= []).push(fn); }, async fire(name, extra = {}) { for (const fn of events[name] || []) await fn({target: this, preventDefault() {}, ...extra}); },
    focus() {}, scrollIntoView() {}, reportValidity() { return true; }, reset() {},
  };
}
function context(storage = new Map(), simulation = false) {
  const nodes = new Map();
  const $ = selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
  const document = {querySelector: $, querySelectorAll: () => [], createElement: name => element("", name.toUpperCase()), createTextNode: text => ({textContent: text}), addEventListener() {}};
  const state = {bootstrap: {models, templates: [{id: "template", name: "Template", spec: spec()}], reference_images: [], simulation, scenarios: {available: true, default_quantity: 1, default_duration: 60}, settings: {defaults: {model_family: "short", resolution: "720p", concurrency: 1}}}, connected: true, page: "board", generationStep: 1, defaults: {}, batchSession: 0, formRevision: 0, estimateRequest: 0, estimateBusy: false, estimate: null, estimatePayload: "", submitting: false, voiceRequest: 0, voiceBusy: false, voiceAsset: null, scenario: {mode: "scenario", drafts: [], sourceRevision: 0, draftRevision: -1, planner: null, busy: false, request: 0, controller: null}};
  const calls = [], notices = [];
  const ctx = vm.createContext({console, setTimeout, clearTimeout, AbortController, Date, document, window: {addEventListener() {}}, store: state,
    localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
    $, $$: (selector, scope) => scope?.querySelectorAll?.(selector) || [],
    node: (tag, className, text) => Object.assign(element("", tag.toUpperCase()), {className, textContent: text || ""}), icon: () => element(),
    setText: (selector, value) => { $(selector).textContent = String(value); }, formError: (selector, value) => { $(selector).textContent = value; $(selector).hidden = !value; },
    toast: text => notices.push(text), numeric: (x, fallback = 0) => Number.isFinite(Number(x)) ? Number(x) : fallback, duration: x => `${x} 秒`, shortDate: x => x, money: x => x == null ? "待核实" : `¥${Number(x).toFixed(2)}`,
    api: async (url, body) => { calls.push({url, body}); return {jobs: [{id: "job"}], batch: {id: "batch"}}; },
    navigatePage: page => { state.page = page; }, onCreationSubmitted: async () => { state.page = "board"; }, FormData: class {append() {}},
  });
  vm.runInContext(source, ctx);
  const run = (name, ...args) => { const fn = vm.runInContext(`typeof ${name} === 'function' ? ${name} : null`, ctx); assert.equal(typeof fn, "function", `${name} must exist`); return fn(...args); };
  return {ctx, run, $, state, storage, calls, notices};
}
function init(t) { t.$("#scenario-duration").value = "60"; t.$("#scenario-quantity").value = "1"; t.$("#scenario-task-type").value = "manipulation"; t.$("#batch-quantity").value = "1"; t.run("initializeGeneration"); }
function ready(t, name = "Ready") { t.$("#batch-name").value = name; t.$("#scenario-description").value = "First person sorts cups on a kitchen table"; t.state.scenario.drafts = [{selected: true, spec: spec(name)}]; t.state.scenario.draftRevision = t.state.scenario.sourceRevision; }

test("draft storage restores inputs, selected scripts and public metadata without credentials or media bytes", () => {
  const t = context(); init(t); ready(t); t.state.voiceAsset = {asset_id: "voice-1", name: "voice.wav", content: "data:audio/wav;base64,secret"};
  t.state.scenario.planner = {model: "planner", simulation: false, usage: {total_tokens: 20}, api_key: "secret", csrf: "token"};
  t.run("persistGenerationDraft"); const saved = [...t.storage.entries()].find(([key]) => key.includes("generation")); assert.ok(saved); assert.match(saved[0], /real/); assert.doesNotMatch(saved[1], /secret|csrf|api_key|data:audio/);
  const restored = context(t.storage); init(restored); assert.equal(restored.$("#batch-name").value, "Ready"); assert.equal(restored.state.scenario.drafts[0].selected, true); assert.equal(restored.state.voiceAsset.asset_id, "voice-1"); assert.equal(restored.run("scenarioReady"), true);
  const simulated = context(t.storage, true); init(simulated); assert.equal(simulated.state.scenario.drafts.length, 0);
});
test("opening and leaving the generation page preserves the current session and reconnect preserves edits", () => {
  const t = context(); init(t); ready(t); const session = t.state.batchSession; t.run("openBatch"); t.state.page = "library"; t.run("persistGenerationDraft"); t.run("openBatch"); t.run("initializeGeneration");
  assert.equal(t.state.batchSession, session); assert.equal(t.$("#batch-name").value, "Ready"); assert.equal(t.state.scenario.drafts.length, 1);
});
test("new drafts and metadata reuse keep older drafts switchable and preserve custom legacy duration", () => {
  const t = context(); init(t); ready(t, "Keep me"); t.run("persistGenerationDraft"); const oldId = t.$("#generation-draft-select").value;
  t.run("reuseGenerationMetadata", {spec: spec("150 second video", 5, 30), model_family: "long", profile: "my-profile", job: {resolution: "1080p"}});
  assert.equal(t.state.scenario.mode, "legacy"); assert.equal(t.run("creationPayload").spec.target_duration_seconds, 150); assert.equal(t.run("creationPayload").profile, "my-profile"); assert.match(t.$("#generation-reuse-note").textContent, /声音.*重新/);
  t.run("switchGenerationDraft", oldId); assert.equal(t.$("#batch-name").value, "Keep me"); assert.equal(t.state.scenario.drafts[0].spec.name, "Keep me");
});
test("storage failures keep session drafts and report the persistence limitation", () => {
  const t = context(); t.ctx.localStorage.setItem = () => { throw new Error("quota exceeded"); }; init(t); ready(t, "Memory only"); t.run("persistGenerationDraft"); const oldId = t.$("#generation-draft-select").value; t.run("newGenerationDraft"); t.run("switchGenerationDraft", oldId);
  assert.equal(t.$("#batch-name").value, "Memory only"); assert.match(t.$("#draft-save-status").textContent, /仅.*会话|当前会话/);
});
test("an unsupported restored model and segment duration remain visible and cannot submit", () => {
  const t = context(); init(t); ready(t); t.$("#batch-model-family").value = "retired"; t.$("#batch-segment-duration").value = "99"; t.run("persistGenerationDraft");
  const restored = context(t.storage); init(restored); assert.equal(restored.$("#batch-model-family").value, "retired"); assert.equal(restored.$("#batch-segment-duration").value, "99"); assert.equal(restored.run("scenarioReady"), false); assert.throws(() => restored.run("creationPayload"), /模型/); assert.match(restored.$("#generation-reuse-note").textContent, /修改|不兼容|不可用/);
});
test("expansion completes while another page is visible without navigating back", async () => {
  const t = context(); init(t); ready(t); t.state.scenario.drafts = []; let finish; t.ctx.api = () => new Promise(resolve => { finish = resolve; });
  const pending = t.run("expandScenario"); t.state.page = "board"; t.run("persistGenerationDraft"); finish({variants: [spec("Completed elsewhere")], planner: {model: "planner"}}); await pending;
  assert.equal(t.state.page, "board"); assert.equal(t.state.scenario.drafts[0].spec.name, "Completed elsewhere"); t.run("openBatch"); assert.equal(t.state.generationStep, 2);
});
test("late expansion and estimate responses cannot modify a different draft", async () => {
  const t = context(); init(t); ready(t); let finish; t.ctx.api = () => new Promise(resolve => { finish = resolve; });
  const pending = t.run("expandScenario"); t.run("newGenerationDraft"); t.$("#batch-name").value = "New draft"; finish({variants: [spec("Too late")]}); await pending; assert.equal(t.state.scenario.drafts.length, 0); assert.equal(t.$("#batch-name").value, "New draft");
  ready(t); const estimate = t.run("estimateBudget"); t.run("newGenerationDraft"); finish({estimated_total_cny: 99}); await estimate; assert.equal(t.state.estimate, null);
});
test("direct submission needs no estimate, archives the draft and rejects duplicate clicks", async () => {
  const t = context(); init(t); ready(t); await t.run("createBatch", {preventDefault() {}}); await t.run("createBatch", {preventDefault() {}});
  assert.deepEqual(t.calls.map(call => call.url), ["/api/batches"]); assert.equal(t.calls[0].body.review_mode, "auto"); assert.equal("budget_cny" in t.calls[0].body, false); assert.equal(t.state.page, "board");
  const saved = JSON.parse([...t.storage.entries()].find(([key]) => key.includes("generation"))[1]); assert.equal(saved.drafts.find(draft => draft.id === saved.current_id).status, "submitted");
});
test("model capability validation still enforces 2–20 segments and integer durations", () => {
  const t = context(); assert.throws(() => t.run("validateModelSpec", spec("invalid", 4, 30), models[0]), /不兼容/); assert.throws(() => t.run("plannedDurations", 180, 4, models[0]), /20/); assert.equal(t.run("validateModelSpec", spec("valid", 20, 30), models[1]), 600);
});
function realisticSelect(t, selector) {
  const el = t.$(selector); el.tagName = "SELECT"; let value = "";
  Object.defineProperty(el, "value", {get() { return value; }, set(next) { value = el.children.some(child => child.value === String(next)) ? String(next) : ""; }, configurable: true});
  const replace = el.replaceChildren; el.replaceChildren = function (...children) { replace.apply(this, children); value = ""; };
}
test("browser select restoration retains retired model values rather than falling back silently", () => {
  const t = context(); init(t); ready(t); t.$("#batch-model-family").value = "retired"; t.$("#batch-resolution").value = "4K"; t.$("#batch-segment-duration").value = "99"; t.run("persistGenerationDraft");
  const restored = context(t.storage); ["#batch-model-family", "#batch-resolution", "#batch-segment-duration", "#template-select"].forEach(id => realisticSelect(restored, id)); init(restored);
  assert.equal(restored.$("#batch-model-family").value, "retired"); assert.equal(restored.$("#batch-resolution").value, "4K"); assert.equal(restored.$("#batch-segment-duration").value, "99"); assert.throws(() => restored.run("creationPayload"), /模型/);
});
test("unreadable older storage is preserved, while current edits can still be kept in memory", () => {
  const storage = new Map([["seedance-studio-generation:v1:real", "{unfinished"]]); const t = context(storage); init(t); ready(t); t.run("persistGenerationDraft");
  assert.equal(storage.get("seedance-studio-generation:v1:real"), "{unfinished"); assert.match(t.$("#draft-save-status").textContent, /仅当前会话/);
});
test("an audio upload finishing after a draft switch cannot attach to the new draft", async () => {
  const t = context(); init(t); t.run("setupVoiceUpload");
  const find = (element, id) => element.id === id ? element : element.children?.map(child => find(child, id)).find(Boolean);
  const input = find(t.$("#generation-reference-inputs"), "voice-file"); input.files = [{name: "voice.wav", size: 1000}]; let finish; t.ctx.api = () => new Promise(resolve => { finish = resolve; });
  const uploading = input.fire("change"); t.run("newGenerationDraft"); finish({asset_id: "old-audio", name: "voice.wav", duration_seconds: 3}); await uploading;
  assert.equal(t.state.voiceAsset, null); assert.equal(t.state.voiceBusy, false);
});
test("text entered before the first connection survives bootstrap and does not call a model", async () => {
  const t = context(), bootstrap = t.state.bootstrap; t.state.bootstrap = null; t.state.connected = false; t.run("setupGeneration"); t.run("openBatch");
  t.$("#batch-name").value = "Written offline"; t.$("#scenario-description").value = "Keep the offline kitchen scenario description";
  await t.$("#batch-form").fire("input", {target: t.$("#scenario-description")});
  t.state.bootstrap = bootstrap; t.state.connected = true; t.run("initializeGeneration");
  assert.equal(t.$("#batch-name").value, "Written offline"); assert.equal(t.$("#scenario-description").value, "Keep the offline kitchen scenario description"); assert.equal(t.calls.length, 0);
});
test("an unselected invalid script does not block submitting the selected valid scripts", () => {
  const t = context(); init(t); ready(t); t.state.scenario.drafts.push({selected: false, spec: spec("Not selected", 4, 30)});
  assert.equal(t.run("creationPayload").variants.length, 1);
});
test("submission completion receives the original page and navigation epoch", async () => {
  const t = context(); init(t); ready(t); t.state.page = "create"; t.state.navigationEpoch = 4;
  let finish, completion; t.ctx.api = () => new Promise(resolve => { finish = resolve; });
  t.ctx.onCreationSubmitted = async (result, payload, origin) => { completion = {result, payload, origin}; };
  const submitting = t.run("createBatch", {preventDefault() {}});
  t.state.page = "library"; t.state.navigationEpoch = 5; finish({batch: {id: "batch"}, jobs: [{id: "job"}]}); await submitting;
  assert.deepEqual(completion.origin && JSON.parse(JSON.stringify(completion.origin)), {page: "create", epoch: 4});
  assert.equal(t.state.page, "library"); assert.equal(t.state.navigationEpoch, 5);
});
test("valid output reuse opens script review while preserving the previous draft and voice warning", () => {
  const t = context(); init(t); ready(t, "Original"); t.run("persistGenerationDraft"); const originalId = t.$("#generation-draft-select").value;
  t.run("reuseGenerationMetadata", {spec: spec("Reusable", 5, 30), model_family: "long", job: {resolution: "1080p"}});
  assert.equal(t.state.generationStep, 2); assert.equal(t.run("creationPayload").spec.target_duration_seconds, 150); assert.match(t.$("#generation-reuse-note").textContent, /声音.*重新/);
  t.run("switchGenerationDraft", originalId); assert.equal(t.$("#batch-name").value, "Original");
});
test("incompatible output reuse stays in configuration and shows the validation error without rewriting the script", () => {
  const t = context(); init(t);
  t.run("reuseGenerationMetadata", {spec: spec("Needs correction", 4, 30), model_family: "short", job: {resolution: "4K"}});
  assert.equal(t.state.generationStep, 1); assert.match(t.$("#batch-error").textContent, /分辨率/); assert.equal(t.$("#batch-resolution").value, "4K");
  assert.equal(JSON.parse(t.$("#custom-spec").value).segments[0].duration, 30); assert.match(t.$("#generation-reuse-note").textContent, /声音.*重新/);
});
