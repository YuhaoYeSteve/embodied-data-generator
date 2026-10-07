"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Load the five page modules in browser order; boot.js owns startup and is intentionally excluded.
const sources = ["app.js", "generation.js", "board.js", "library.js", "navigation.js"].map(file => ({file, source: fs.readFileSync(path.join(__dirname, "../web", file), "utf8")}));
function loadModules(context) {
  for (const {file, source} of sources) vm.runInContext(source, context, {filename: file});
}
const context = vm.createContext({console, setTimeout, clearTimeout, URLSearchParams});
loadModules(context);
function call(name, ...args) {
  const fn = vm.runInContext(`typeof ${name} === "function" ? ${name} : null`, context);
  assert.equal(typeof fn, "function", `${name} must be implemented`);
  return JSON.parse(JSON.stringify(fn(...args)));
}
const catalog = [
  {id: "short-model", label: "Short model", resolutions: ["480p", "720p"], durations: Array.from({length: 12}, (_, i) => i + 4), default_resolution: "480p", default_segment_duration: 15, max_reference_duration: 15},
  {id: "long-model", label: "Long model", resolutions: ["720p", "1080p"], durations: Array.from({length: 27}, (_, i) => i + 4), default_resolution: "720p", default_segment_duration: 30, max_reference_duration: 30},
];

test("legacy saved settings retain valid preferences without restoring manual review or budgets", () => {
  const settings = call("productionDefaults", catalog, {model_family: "long-model", concurrency: 1}, {resolution: "1080p", concurrency: 2, review_mode: "manual", budget_cny: 0.01});
  assert.deepEqual(settings, {model_family: "long-model", resolution: "1080p", segment_duration_seconds: 30, concurrency: 2});
});

test("unknown stored models and unsupported resolutions use current server defaults", () => {
  assert.deepEqual(call("productionDefaults", catalog, {model_family: "short-model"}, {model_family: "retired", resolution: "4K", concurrency: 99}), {
    model_family: "short-model", resolution: "480p", segment_duration_seconds: 15, concurrency: 1,
  });
});

test("changing model preserves a compatible resolution and replaces incompatible segment length", () => {
  assert.deepEqual(call("modelSelection", catalog[0], "720p", 30), {resolution: "720p", segment_duration_seconds: 15});
  assert.deepEqual(call("modelSelection", catalog[1], "480p", 15), {resolution: "720p", segment_duration_seconds: 15});
});

test("duration planning retains the requested total while borrowing for a short final segment", () => {
  assert.deepEqual(call("plannedDurations", 60, 29, catalog[1]), [29, 27, 4]);
  assert.deepEqual(call("plannedDurations", 60, 15, catalog[0]), [15, 15, 15, 15]);
  assert.deepEqual(call("plannedDurations", 60, 30, catalog[1]), [30, 30]);
});

test("unsupported model duration and plans exceeding twenty segments are blocked", () => {
  assert.throws(() => call("plannedDurations", 60, 30, catalog[0]), /单段时长/);
  assert.throws(() => call("plannedDurations", 180, 4, catalog[0]), /20/);
  assert.throws(() => call("plannedDurations", 60, 7.5, catalog[0]), /单段时长/);
});

test("JSON scripts cannot submit thirty-second segments to a fifteen-second model", () => {
  const spec = {segments: [{duration: 30}, {duration: 30}]};
  assert.throws(() => call("validateModelSpec", spec, catalog[0]), /第 1 段.*30/);
  assert.equal(call("validateModelSpec", spec, catalog[1]), 60);
});

test("script validation rejects fractional durations and out-of-range totals", () => {
  assert.throws(() => call("validateModelSpec", {segments: [{duration: 14.5}, {duration: 14.5}, {duration: 15}, {duration: 16}]}, catalog[1]), /第 1 段/);
  assert.throws(() => call("validateModelSpec", {segments: [{duration: 4}, {duration: 4}]}, catalog[0]), /60.*600/);
});

function pageContext(extra = {}) {
  const storage = new Map();
  const page = vm.createContext({console, setTimeout, clearTimeout, URLSearchParams, AbortController,
    document: documentFixture(), models: catalog,
    localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
    window: {location: {hash: ""}, history: {replaceState() {}, pushState() {}}, addEventListener() {}},
    ...extra});
  loadModules(page);
  vm.runInContext("store.bootstrap = {models, templates: [], reference_images: []}; store.connected = true;", page);
  return page;
}

test("creation payload uses selected capabilities and never sends old budget or manual settings", () => {
  const values = {"#batch-name": "New batch", "#batch-resolution": "720p", "#batch-model-family": "short-model", "#batch-segment-duration": "15", "#batch-concurrency": "2", "#batch-quantity": "1", "#batch-profile": ""};
  const page = pageContext({document: {querySelector: selector => selector === "#batch-form" ? {reportValidity: () => true} : {value: values[selector] || ""}}});
  page.models = catalog;
  vm.runInContext("store.bootstrap = {models}; store.scenario.mode = 'legacy'; customSpec = () => ({segments: Array(4).fill({duration: 15})});", page);
  const payload = JSON.parse(JSON.stringify(vm.runInContext("creationPayload()", page)));
  assert.equal(payload.model_family, "short-model");
  assert.equal(payload.segment_duration_seconds, 15);
  assert.equal(payload.resolution, "720p");
  assert.equal(payload.review_mode, "auto");
  assert.equal("budget_cny" in payload, false);
  assert.equal("model" in payload, false);
});

test("one click creates without a previous estimate and freezes the submitted payload", async () => {
  const calls = [], submissions = [], document = documentFixture();
  const page = pageContext({document, calls, submissions});
  let finish;
  page.pendingCreation = new Promise(resolve => { finish = resolve; });
  vm.runInContext(`
    const draftPayload = {name: "Direct start", model_family: "short-model", quantity: 1, spec: {segments: [{duration: 15}]}};
    creationPayload = () => draftPayload;
    toast = () => {}; updateScenarioState = () => {}; setCreationMode = () => {};
    onCreationSubmitted = async (result, payload) => submissions.push({result, payload});
    api = async (url, payload) => { calls.push({url, payload}); return pendingCreation; };
    store.estimate = null; store.estimatePayload = "";
  `, page);
  const request = vm.runInContext("createBatch({preventDefault() {}})", page);
  await vm.runInContext("createBatch({preventDefault() {}})", page);
  vm.runInContext('draftPayload.name = "Later edit"; draftPayload.spec.segments[0].duration = 30;', page);
  finish({batch: {id: "batch-created", estimated_cny: 7}, jobs: [{id: "created", batch_id: "batch-created"}]});
  await request;
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/batches");
  assert.equal(calls[0].payload.name, "Direct start");
  assert.equal(calls[0].payload.spec.segments[0].duration, 15);
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].result.jobs[0].id, "created");
  assert.equal(submissions[0].payload.name, "Direct start");
  assert.equal(vm.runInContext("generationWorkspace.submitted", page), true);
});

function element(value = "", tagName = "DIV") {
  return {value, tagName, disabled: false, hidden: false, open: true, children: [], dataset: {}, style: {}, textContent: "", attributes: {},
    classList: {add() {}, remove() {}, toggle() {}},
    append(...children) { this.children.push(...children); },
    prepend(...children) { this.children.unshift(...children); },
    replaceChildren(...children) { this.children = children; },
    get childNodes() { return this.children; }, get childElementCount() { return this.children.length; },
    setAttribute(key, value) { this.attributes[key] = value; }, removeAttribute(key) { delete this.attributes[key]; },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, replaceWith(replacement) { this.className = replacement.className; this.textContent = replacement.textContent; this.children = replacement.children; },
    pause() {}, load() {}, focus() {}, scrollIntoView() {}, reportValidity() { return true; },
  };
}
function documentFixture(values = {}) {
  const defaults = {"#batch-name": "New batch", "#batch-model-family": "short-model", "#batch-resolution": "720p", "#batch-segment-duration": "15", "#batch-concurrency": "2", "#batch-quantity": "1", "#batch-profile": "", "#scenario-duration": "60", "#scenario-quantity": "1", "#scenario-task-type": "manipulation", "#scenario-description": "First person sorts cups on a kitchen table", "#custom-spec": JSON.stringify({segments: Array.from({length: 4}, () => ({duration: 15}))})};
  const elements = new Map(Object.entries({...defaults, ...values}).map(([key, value]) => [key, element(value)]));
  return {elements,
    querySelector(selector) { if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector); },
    querySelectorAll() { return []; },
    createElement: name => element("", name.toUpperCase()),
    createElementNS: (_, name) => element("", name.toUpperCase()),
    createTextNode: text => ({textContent: text}),
  };
}

test("switching models narrows choices and invalidates existing drafts without rewriting their timeline", () => {
  const document = documentFixture({"#batch-model-family": "short-model", "#batch-resolution": "1080p", "#batch-segment-duration": "30", "#scenario-duration": "60"});
  const page = pageContext({document, models: catalog});
  vm.runInContext(`
    store.bootstrap = {models};
    store.scenario.drafts = [{selected: true, spec: {segments: [{duration: 30}, {duration: 30}]}}];
    store.scenario.sourceRevision = 2; store.scenario.draftRevision = 2;
    renderScenarioDrafts = () => {}; updateScriptStatus = () => {}; updateScenarioState = () => {};
    modelChanged();
  `, page);
  assert.equal(document.querySelector("#batch-resolution").value, "480p");
  assert.equal(document.querySelector("#batch-segment-duration").value, "15");
  assert.equal(document.querySelector("#batch-segment-duration").children.some(option => option.value === "30"), false);
  assert.equal(vm.runInContext("scenarioReady()", page), false);
  assert.equal(vm.runInContext("store.scenario.drafts[0].spec.segments[0].duration", page), 30);
});

test("an optional estimate failure leaves direct creation available", async () => {
  const document = documentFixture(), calls = [], submissions = [];
  const page = pageContext({document, calls, submissions});
  vm.runInContext(`
    store.scenario.mode = "legacy";
    toast = () => {};
    onCreationSubmitted = async (result, payload) => submissions.push({result, payload});
    api = async (url, payload) => { calls.push(url); if (url.endsWith("estimate")) throw new Error("estimate unavailable"); return {jobs: [{id: "after-estimate-error"}]}; };
  `, page);
  await vm.runInContext("estimateBudget()", page);
  assert.equal(document.querySelector("#create-batch").disabled, false);
  assert.match(document.querySelector("#estimate-description").textContent, /estimate unavailable.*可直接开始生成/);
  await vm.runInContext("createBatch({preventDefault() {}})", page);
  assert.deepEqual(calls, ["/api/batches/estimate", "/api/batches"]);
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].result.jobs[0].id, "after-estimate-error");
});

test("an expansion response from before a model change cannot replace saved drafts", async () => {
  const document = documentFixture(), page = pageContext({document, AbortController, models: catalog});
  let finish;
  page.pending = new Promise(resolve => { finish = resolve; });
  vm.runInContext(`
    store.bootstrap = {models}; store.scenario.drafts = [{selected: true, spec: {name: "Original draft"}}];
    scenarioInput = () => ({description: "A complete object sorting task", task_type: "manipulation", duration_seconds: 60, quantity: 1, diversity: "", segment_duration_seconds: 30});
    currentModel = () => models[1]; updateScenarioState = () => {};
    api = () => pending;
  `, page);
  const work = vm.runInContext("expandScenario()", page);
  vm.runInContext("store.scenario.sourceRevision++; store.formRevision++;", page);
  finish({variants: [{name: "Late draft"}]});
  await work;
  assert.equal(vm.runInContext("store.scenario.drafts[0].spec.name", page), "Original draft");
  assert.match(document.querySelector("#scenario-status").textContent, /丢弃/);
});

test("unknown monetary values never format as zero while genuine zero remains explicit", () => {
  for (const value of [null, undefined, "", "   ", "invalid", NaN, Infinity, -Infinity, false, true, [], {}]) {
    assert.match(call("money", value), /^(待核实|—)$/, `unexpected currency for ${String(value)}`);
  }
  assert.equal(call("money", 0), "¥0.00");
  assert.equal(call("money", "0"), "¥0.00");
  assert.equal(call("money", 1.25), "¥1.25");
});

test("batch task rows and details disclose unknown costs and distinguish the known portion", () => {
  const document = documentFixture();
  const page = pageContext({document});
  vm.runInContext(`
    const unknownJob = {id: "unknown-price", batch_id: "batch", title: "Unknown cost", status: "generating", duration_seconds: 60, estimated_cny: null, spent_cny: 0, reserved_cny: null, known_spent_cny: 1.25};
    store.state.batches = [{id: "batch", name: "Unknown cost batch"}]; store.state.jobs = [unknownJob]; store.selectedId = unknownJob.id; store.boardFocusId = "batch"; store.detail = {job: unknownJob, segments: [], logs: []};
    renderPreview = () => {}; renderActions = () => {}; renderSegments = () => {}; renderLogs = () => {};
    renderJobs(); renderDetail();
  `, page);
  const row = document.querySelector("#batch-jobs").children[0];
  const cost = row.children.find(child => child.className === "batch-job-cost");
  assert.match(cost.textContent, /部分费用待核实/);
  const detail = document.querySelector("#detail-cost").textContent;
  assert.match(detail, /部分费用待核实/);
  assert.match(detail, /已知.*¥1\.25/);
  assert.doesNotMatch(detail, /预计 ¥0\.00|已用 ¥0\.00/);
});

test("an unknown aggregate does not fall back to a misleading zero sum", () => {
  const document = documentFixture(), page = pageContext({document});
  vm.runInContext(`
    store.state.jobs = [{status: "generating", estimated_cny: null, spent_cny: 0, reserved_cny: null, known_spent_cny: 1.25}];
    store.state.summary = {spent_cny: null, reserved_cny: null, known_spent_cny: 1.25};
    renderMetrics();
  `, page);
  assert.match(document.querySelector("#metric-cost").textContent, /待核实|—/);
});
