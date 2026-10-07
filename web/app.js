/* Local Seedance production workspace. All production data comes from the API. */
"use strict";

const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));
const STATUS = {
  queued: "排队中", generating: "生成中", reviewing: "自动质检中", awaiting_review: "历史待审记录",
  paused: "已暂停", failed: "任务失败", budget_blocked: "历史暂停记录", submission_unknown: "提交状态待确认",
  exporting: "导出中", completed: "已完成", accepted: "已通过", rejected: "未通过", pending: "待生成",
  succeeded: "已生成", success: "已生成", ready: "可预览", running: "运行中", cancelled: "已取消"
};
const ACTIVE = new Set(["queued", "generating", "reviewing", "exporting"]);
const PASS_TEXT = {pass: "通过", fail: "未通过", unverified: "尚未确认"};
const store = {
  page: 'board', generationStep: 1, generationInitialized: false, routingReady: false, lastRouteHash: null, boardFocusId: null, navigationEpoch: 0, refreshPromise: null,
  library: {search: '', model: 'all', resolution: 'all', batchId: 'all', selectedId: null, metadata: null, request: 0},
  bootstrap: null, state: {batches: [], jobs: [], summary: {}}, selectedId: null,
  detail: null, filter: "all", search: "", batchId: "all", connected: false,
  polling: false, detailRequest: 0, detailSignature: "", segmentsSignature: "", logsSignature: "", reviewSignature: "",
  previewSegment: null, previewURL: "", estimate: null, estimatePayload: "", submitting: false,
  voiceAsset: null, lastPoll: 0, defaults: {}, actionBusy: false,
  batchSession: 0, formRevision: 0, estimateRequest: 0, estimateBusy: false, voiceRequest: 0, voiceBusy: false,
  scenario: {mode: "scenario", drafts: [], sourceRevision: 0, draftRevision: -1, planner: null, busy: false, request: 0, controller: null}
};
let toastTimer;
let dialogOpener = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#icon-${name}`); svg.append(use); return svg;
}
function moneyAmount(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const amount = Number(value);
  return Number.isFinite(amount) ? amount : null;
}
function money(value, prefix = true) {
  const amount = moneyAmount(value);
  return amount === null ? "待核实" : `${prefix ? "¥" : ""}${amount.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}
function hasUnverifiedCosts(job) {
  return [job.estimated_cny, job.spent_cny, job.reserved_cny].some(value => moneyAmount(value) === null);
}
function jobCostText(job, detailed = false) {
  const unverified = hasUnverifiedCosts(job);
  if (!detailed) return unverified ? "部分费用待核实" : money(job.spent_cny);
  const known = moneyAmount(job.known_spent_cny) ?? moneyAmount(job.spent_cny);
  const spent = unverified ? `${known === null ? "" : `已知已用 ${money(known)} · `}部分费用待核实` : `已用 ${money(job.spent_cny)}`;
  return `预计 ${money(job.estimated_cny)} · ${spent}`;
}
function numeric(value, fallback = 0) { const n = Number(value); return Number.isFinite(n) ? n : fallback; }
function duration(value) { const n = numeric(value); return `${Number.isInteger(n) ? n : n.toFixed(1)} 秒`; }
function shortDate(value, withTime = true) {
  if (!value) return "—";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 19);
  return date.toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", ...(withTime ? {hour: "2-digit", minute: "2-digit", hour12: false} : {})});
}
function statusBadge(status) { return node("span", `status-badge ${String(status || "pending").replace(/[^a-z_]/g, "")}`, STATUS[status] || status || "待生成"); }
function segmentTitle(segment, index = numeric(segment.index)) {
  const names = {"01_greeting_reservation": "迎宾与预订确认", "02_identity_verification": "证件交接与身份核验", "03_order_room_preference": "订单确认与房间安排", "04_keycard_farewell": "房卡制作与送别"};
  return names[segment.title] || segment.title || `第 ${index + 1} 段`;
}
function segmentStatus(segment) {
  if (!segment.review_is_suggestion && segment.review?.decision === "accept") return "accepted";
  if (!segment.review_is_suggestion && segment.review?.decision === "reject") return "rejected";
  return segment.status;
}
function validMediaURL(value) {
  if (typeof value !== "string" || !/^\/api\/media\/[^?#\\]+(?:\?[^#\\]*)?$/.test(value)) return "";
  return value;
}
function jobURL(id, action) { return `/api/jobs/${encodeURIComponent(id)}/${action}`; }
function currentJob() { return store.detail && store.detail.job; }
function currentBatch() { const job = currentJob(); return job && store.state.batches.find(batch => batch.id === job.batch_id); }
function batchLabel(id) { const batch = store.state.batches.find(item => item.id === id); return batch ? batch.name : "历史视频"; }
function setText(selector, value) { $(selector).textContent = value == null ? "—" : String(value); }
function formError(id, message) { const element = $(id); element.textContent = message || ""; element.hidden = !message; }
function toast(message, actionLabel, action) {
  clearTimeout(toastTimer); setText("#toast", message);
  if (actionLabel && typeof action === 'function') {
    const button = node('button', 'toast-action', actionLabel); button.type = 'button';
    button.addEventListener('click', () => { $('#toast').hidden = true; action(); }); $('#toast').append(button);
  }
  $('#toast').hidden = false;
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, actionLabel ? 8000 : 4500);
}

async function api(path, body, options = {}) {
  const config = {method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}, ...options};
  if (body !== undefined) {
    config.headers["X-Studio-Token"] = store.bootstrap?.csrf_token || "";
    if (body instanceof FormData) config.body = body;
    else { config.headers["Content-Type"] = "application/json"; config.body = JSON.stringify(body); }
  }
  let response;
  try { response = await fetch(path, config); } catch (_) { throw new Error("无法连接本地服务。请确认工作台服务正在运行，再重新连接。"); }
  let result;
  try { result = await response.json(); } catch (_) { throw new Error(`本地服务返回了非 JSON 内容（HTTP ${response.status}），请检查服务是否正常启动。`); }
  if (!response.ok) throw new Error(result.error || result.message || `请求失败（HTTP ${response.status}）`);
  return result;
}


function openDialog(id) { const dialog = $(`#${id}`); dialogOpener = document.activeElement; dialog.showModal(); document.body.style.overflow = "hidden"; }
function closeDialog(id) { const dialog = $(`#${id}`); if (dialog) dialog.close(); document.body.style.overflow = ""; }

function sanitizedEnvironment(value) {
  if (Array.isArray(value)) return value.map(sanitizedEnvironment);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).filter(([key]) => !/secret|token|api.?key|password/i.test(key)).map(([key, item]) => [key, sanitizedEnvironment(item)]));
  return value;
}
function openSettings() {
  const list = $("#environment-list"); list.replaceChildren();
  const environment = sanitizedEnvironment(store.bootstrap?.environment || {});
  const settings = store.bootstrap?.settings || {};
  if (settings.profile) environment.profile = settings.profile;
  if (settings.model) environment.model = settings.model;
  ["arkcli", "ffmpeg", "ffprobe"].forEach(key => { if (`${key}_available` in environment) delete environment[key]; });
  const labels = {arkcli: "ArkCLI", arkcli_version: "ArkCLI 版本", arkcli_available: "ArkCLI 可用", authenticated: "认证状态", profile: "生成 Profile", model: "默认模型", ffmpeg: "FFmpeg", ffmpeg_available: "FFmpeg 可用", ffprobe: "FFprobe", ffprobe_available: "FFprobe 可用", automatic_review: "自动质检", server_mode: "运行环境", python: "Python", host: "监听地址", port: "服务端口", root: "工作目录", output_dir: "输出目录", simulation: "模拟环境"};
  if (!Object.keys(environment).length) list.append(node("p", "form-note", "连接本地服务后，可查看环境信息。"));
  Object.entries(environment).forEach(([key, value]) => {
    const row = node("dl", "environment-row"); row.append(node("dt", null, labels[key] || key));
    const detail = node("dd"); detail.append(typeof value === "object" ? node("pre", null, JSON.stringify(value, null, 2)) : node("span", null, typeof value === "boolean" ? value ? "已就绪" : "未就绪" : value)); row.append(detail); list.append(row);
  });
  fillSelect($("#default-model-family"), store.bootstrap?.models.map(model => model.id) || [], store.defaults.model_family, id => store.bootstrap.models.find(model => model.id === id).label);
  syncDefaultModel();
  const model = store.bootstrap?.models.find(item => item.id === store.defaults.model_family);
  $("#default-resolution").value = modelSelection(model, store.defaults.resolution, model?.default_segment_duration).resolution;
  $("#default-concurrency").value = store.defaults.concurrency || 1;
  formError("#settings-error", ""); openDialog("settings-dialog");
}
function syncDefaultModel() {
  const model = store.bootstrap?.models.find(item => item.id === $("#default-model-family").value);
  fillSelect($("#default-resolution"), model?.resolutions || [], modelSelection(model, $("#default-resolution").value, model?.default_segment_duration).resolution);
}
function saveSettings(event) {
  event.preventDefault();
  if (!$("#default-model-family").value) { formError("#settings-error", "模型能力尚未加载，请重新连接后保存。"); return; }
  store.defaults = productionDefaults(store.bootstrap?.models || [], store.bootstrap?.settings || {}, {model_family: $("#default-model-family").value, resolution: $("#default-resolution").value, concurrency: Number($("#default-concurrency").value)});
  try { localStorage.setItem("seedance-studio-defaults", JSON.stringify(store.defaults)); closeDialog("settings-dialog"); toast("默认配置已保存，下次新建批次时生效。"); }
  catch (_) { formError("#settings-error", "浏览器禁止本地存储，配置仅对当前页面有效。"); }
}
