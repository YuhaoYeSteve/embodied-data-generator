"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.style = {}; this.attributes = {}; this.handlers = {}; this.value = ""; this.hidden = false; this.paused = 0; this.loads = 0; this.className = ""; this._text = ""; this.classList = {add() {}, remove() {}, toggle() {}}; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent || "").join(""); }
  get childNodes() { return this.children; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; if (key === "src") this.src = ""; }
  addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
  dispatch(name) { return Promise.all((this.handlers[name] || []).map(fn => fn({target: this, preventDefault() {}}))); }
  querySelector(selector) { return this.children.find(child => child.tagName?.toLowerCase() === selector) || null; }
  querySelectorAll() { return []; }
  replaceWith(replacement) { Object.assign(this, replacement); }
  pause() { this.paused++; }
  load() { this.loads++; }
  scrollIntoView() {}
}
function fixture() {
  const elements = new Map(), calls = [], routes = [], reused = [];
  const $ = selector => { if (!elements.has(selector)) elements.set(selector, new Element(selector.includes("video") ? "video" : "div")); return elements.get(selector); };
  const store = {page: "board", state: {jobs: [], batches: [], summary: {}}, bootstrap: {models: []}, filter: "all", search: "", batchId: "all", selectedId: null, detail: null, detailRequest: 0, detailSignature: "", previewSegment: null, previewURL: "", library: {search: "", model: "all", resolution: "all", batchId: "all", selectedId: null, metadata: null, request: 0}};
  const node = (tag, cls, text) => { const e = new Element(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const numeric = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const context = vm.createContext({console, setTimeout, clearTimeout, store, $, $$: () => [], node, icon: () => node("svg"), numeric,
    STATUS: {generating: "生成中", completed: "已完成", paused: "已暂停"}, ACTIVE: new Set(["queued", "generating", "reviewing", "exporting"]), PASS_TEXT: {},
    validMediaURL: value => typeof value === "string" && /^\/api\/media\/[^?#\\]+(?:\?[^#\\]*)?$/.test(value) ? value : "",
    jobURL: (id, action) => `/api/jobs/${encodeURIComponent(id)}/${action}`,
    currentJob: () => store.detail?.job, currentBatch: () => store.state.batches.find(batch => batch.id === store.detail?.job?.batch_id),
    batchLabel: id => store.state.batches.find(batch => batch.id === id)?.name || "历史视频",
    setText: (selector, value) => { $(selector).textContent = value == null ? "—" : value; },
    formError: (selector, message) => { $(selector).textContent = message || ""; $(selector).hidden = !message; },
    statusBadge: status => node("span", `status-badge ${status}`, status), segmentStatus: segment => segment.status, segmentTitle: segment => segment.title,
    duration: value => `${numeric(value)} 秒`, shortDate: () => "10-07 12:00", money: value => value == null ? "待核实" : `¥${Number(value).toFixed(2)}`,
    moneyAmount: value => value == null ? null : Number(value), hasUnverifiedCosts: job => job.estimated_cny == null || job.spent_cny == null || job.reserved_cny == null,
    jobCostText: job => job.estimated_cny == null ? "部分费用待核实" : `¥${job.spent_cny || 0}`,
    saveWorkspaceState() {}, persistGenerationDraft() {}, toast() {}, connection() {}, refreshState: async () => {},
    navigatePage: (page, params) => { routes.push({page, params}); store.page = page; },
    reuseGenerationMetadata: async meta => { reused.push(meta); store.page = "create"; },
    api: async (url, body) => { calls.push({url, body}); return {job: {id: url.split("/")[3]}, segments: [], logs: []}; },
  });
  for (const file of ["board.js", "library.js"]) {
    const target = path.join(__dirname, "../web", file);
    if (fs.existsSync(target)) vm.runInContext(fs.readFileSync(target, "utf8"), context, {filename: file});
  }
  const fn = name => { const value = vm.runInContext(`typeof ${name} === "function" ? ${name} : undefined`, context); assert.equal(typeof value, "function", `${name} must be implemented`); return value; };
  return {store, elements, $, context, calls, routes, reused, fn};
}
const job = (id, status, batch_id = "b1", extra = {}) => ({id, status, batch_id, title: `视频 ${id}`, duration_seconds: 60, estimated_cny: null, spent_cny: null, reserved_cny: null, ...extra});
const plain = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };

test("queued tasks are distinct from actively generating tasks in a batch", () => {
  const f=fixture();f.store.state.batches=[{id:'b1',name:'阶段检查'}];
  f.store.state.jobs=[job('q','queued'),job('g','generating')];
  const group=f.fn('boardGroups')()[0];
  assert.equal(group.counts.queued,1);assert.equal(group.counts.generating,1);
});

test("board groups a batch by real concurrent task states without selecting a player", () => {
  const f = fixture();
  f.store.state.batches = [{id: "b1", name: "杯碗整理"}, {id: "b2", name: "交接"}];
  f.store.state.jobs = [job("a", "generating"), job("b", "reviewing"), job("c", "completed"), job("d", "failed", "b2")];
  const groups = f.fn("boardGroups")();
  assert.equal(groups.length, 2);
  assert.equal(groups[0].counts.generating, 1); assert.equal(groups[0].counts.reviewing, 1); assert.equal(groups[0].counts.completed, 1);
  f.$("#batch-focus").hidden = true; f.fn("renderBoard")();
  assert.equal(f.$("#batch-focus").hidden, false); assert.equal(f.$("#task-count").textContent, "3");
  assert.equal(f.$("#batch-list").children.length, 2); assert.equal(f.$("#batch-jobs").children.length, 3);
  assert.equal(f.store.selectedId, null); assert.equal(f.$("#board-detail").hidden, true); assert.equal(f.calls.length, 0);
});

test("running excludes paused and unknown submissions while attention includes them", () => {
  const f = fixture(); f.store.state.jobs = [job("live", "generating"), job("paused", "paused"), job("unknown", "submission_unknown"), job("failed", "failed")];
  f.store.filter = "running"; assert.deepEqual(plain(f.fn("filteredJobs")().map(item => item.id)), ["live"]);
  f.store.filter = "attention"; assert.deepEqual(plain(f.fn("filteredJobs")().map(item => item.id)), ["paused", "unknown", "failed"]);
});

test("board filtering clears hidden task context and preserves independent library filters", async () => {
  const f = fixture(); f.store.state.jobs = [job("a", "generating"), job("b", "completed")]; f.store.library.search = "独立";
  f.store.selectedId = "a"; f.store.detail = {job: f.store.state.jobs[0], segments: []}; f.$("#preview-video").src = "/api/media/old";
  f.fn("setupBoard")(); f.$("#board-filter").value = "completed"; await f.$("#board-filter").dispatch("change");
  assert.equal(f.store.filter, "completed"); assert.equal(f.store.selectedId, null); assert.equal(f.store.detail, null);
  assert.ok(f.$("#preview-video").paused > 0); assert.equal(f.$("#board-detail").hidden, true);
  assert.equal(f.store.page, "board"); assert.equal(f.store.library.search, "独立"); assert.equal(f.routes.length, 0);
});

test("library contains only completed full videos and can search safe dataset labels", () => {
  const f = fixture(); f.store.page = "library";
  f.store.state.jobs = [job("good", "completed", "b1", {preview_url: "/api/media/full.mp4", task_label: "杯碗归位", action_labels: ["抓取"], model_family: "m1", resolution: "720p"}), job("partial", "generating", "b1", {preview_url: "/api/media/partial.mp4"}), job("missing", "completed"), job("unsafe", "completed", "b1", {preview_url: "https://invalid.test/full.mp4"})];
  assert.deepEqual(plain(f.fn("libraryAssets")().map(item => item.id)), ["good"]);
  f.store.state.jobs[0].source_scenario = '仓储工作台上的器具整理';
  f.store.library.search = '仓储工作台'; assert.equal(f.fn('filteredLibraryAssets')().length, 1);
  f.store.library.search = "抓取"; assert.equal(f.fn("filteredLibraryAssets")().length, 1);
  f.fn("renderLibrary")(); assert.equal(f.$("#library-grid").children.length, 1); assert.equal(f.calls.length, 0);
  const card = f.$("#library-grid").children[0]; assert.doesNotMatch(card.textContent, /暂停|重试|进度/); assert.match(card.textContent, /杯碗归位/);
});

test("metadata loads lazily once per selection and late A cannot overwrite B", async () => {
  const f = fixture(), a = deferred(), b = deferred(); f.store.page = "library";
  f.store.state.jobs = [job("a", "completed", "b1", {preview_url: "/api/media/a.mp4"}), job("b", "completed", "b1", {preview_url: "/api/media/b.mp4"})];
  f.context.api = (url, body) => { f.calls.push({url, body}); return url.includes("/a/") ? a.promise : b.promise; };
  const first = f.fn("selectLibraryAsset")("a"); const second = f.fn("selectLibraryAsset")("b");
  b.resolve({job: {id: "b"}, spec: {name: "B"}}); await second;
  a.resolve({job: {id: "a"}, spec: {name: "A"}}); await first;
  assert.equal(f.store.library.selectedId, "b"); assert.equal(f.store.library.metadata.spec.name, "B"); assert.match(f.$("#library-title").textContent, /b/);
  f.fn("renderLibrary")(); f.fn("renderLibrary")(); assert.equal(f.calls.length, 2); assert.ok(f.calls.every(call => call.body === undefined));
});

test("a library response after leaving the page does not reopen the detail or navigate", async () => {
  const f = fixture(), pending = deferred(); f.store.page = "library"; f.store.state.jobs = [job("a", "completed", "b1", {preview_url: "/api/media/a.mp4"})];
  f.context.api = () => pending.promise;
  const request = f.fn("selectLibraryAsset")("a"); f.store.page = "create"; f.$("#library-detail").hidden = true;
  pending.resolve({job: {id: "a"}, spec: {name: "late"}}); await request;
  assert.equal(f.store.page, "create"); assert.equal(f.$("#library-detail").hidden, true); assert.equal(f.routes.length, 0);
});

test("trace opens the source batch and reuse reads metadata without submitting a job", async () => {
  const f = fixture(); f.store.page = "library"; f.store.state.jobs = [job("a/b", "completed", "batch", {preview_url: "/api/media/a.mp4"})];
  f.context.api = async (url, body) => { f.calls.push({url, body}); return {job: {id: "a/b"}, spec: {name: "复用脚本"}}; };
  f.fn("setupLibrary")(); await f.fn("selectLibraryAsset")("a/b"); await f.$("#library-trace").dispatch("click");
  assert.deepEqual(plain(f.routes[0]), {page: "board", params: {batch: "batch", job: "a/b"}});
  f.store.page = "library"; await f.$("#library-reuse").dispatch("click");
  assert.equal(f.reused.length, 1); assert.equal(f.reused[0].spec.name, "复用脚本");
  assert.ok(f.calls.every(call => call.url === "/api/jobs/a%2Fb/metadata" && call.body === undefined));
  assert.equal(f.$("#library-download-video").href, "/api/jobs/a%2Fb/download");
});

test("failed reuse keeps the current generation draft intact", async () => {
  const f = fixture(); f.store.page = "library"; f.store.state.jobs = [job("a", "completed", "b1", {preview_url: "/api/media/a.mp4"})];
  f.store.generationDraft = {name: "Existing draft"}; f.context.api = async () => { throw new Error("读取失败"); };
  f.fn("setupLibrary")(); await f.fn("selectLibraryAsset")("a"); await f.$("#library-reuse").dispatch("click");
  assert.equal(f.reused.length, 0); assert.equal(f.store.generationDraft.name, "Existing draft"); assert.equal(f.store.page, "library"); assert.match(f.$("#library-error").textContent, /读取失败/);
});

test("read-only history never sends production controls", async () => {
  const f = fixture(); f.store.detail = {job: job("archive", "paused", null, {read_only: true, can_resume: true}), segments: []};
  f.fn("renderActions")(); assert.equal(f.$("#job-actions").children.length, 0);
  await f.fn("jobAction")("resume"); assert.equal(f.calls.length, 0);
});


test("board detail fetch cannot revive a task hidden by a later filter", async () => {
  const f = fixture(), pending = deferred(); f.store.state.jobs = [job("a", "generating"), job("b", "completed")];
  f.context.api = () => pending.promise;
  const selection = f.fn("selectJob")("a"); f.fn("setFilter")("completed");
  pending.resolve({job: job("a", "generating"), segments: []}); await selection;
  assert.equal(f.store.selectedId, null); assert.equal(f.store.detail, null); assert.equal(f.$("#board-detail").hidden, true);
});

test("failed thumbnails replace the image with a usable placeholder", async () => {
  const f = fixture(); f.store.page = "library";
  f.store.state.jobs = [job("a", "completed", "b1", {preview_url: "/api/media/a.mp4", poster_url: "/api/media/a.jpg"})];
  f.fn("renderLibrary")(); const media = f.$("#library-grid").children[0].children[0];
  assert.equal(media.children[0].tagName, "IMG"); await media.children[0].dispatch("error");
  assert.equal(media.children[0].className, "library-poster-placeholder"); assert.match(media.textContent, /成片预览/);
});
