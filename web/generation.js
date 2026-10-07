/* Dedicated generation workspace. Requests and drafts survive page navigation. */
"use strict";

function modelSelection(model, resolution, seconds) {
  if (!model) return {resolution: "", segment_duration_seconds: 0};
  return {
    resolution: model.resolutions.includes(resolution) ? resolution : model.default_resolution,
    segment_duration_seconds: model.durations.includes(seconds) ? seconds : model.default_segment_duration
  };
}
function productionDefaults(models, base = {}, saved = {}) {
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) saved = {};
  const model = models.find(item => item.id === saved.model_family) || models.find(item => item.id === base.model_family) || models[0];
  const concurrency = [saved.concurrency, base.concurrency, 1].find(value => Number.isInteger(value) && value >= 1 && value <= 3);
  return {model_family: model?.id || "", ...modelSelection(model, saved.resolution || base.resolution, model?.default_segment_duration), concurrency};
}
function plannedDurations(total, preferred, model) {
  if (!model || !Number.isInteger(preferred) || !model.durations.includes(preferred)) throw new Error("请选择当前模型支持的单段时长。");
  if (!Number.isInteger(total) || total < 60 || total > 600) throw new Error("视频总时长须在 60–600 秒之间。");
  const parts = Array(Math.floor(total / preferred)).fill(preferred), remainder = total % preferred;
  if (remainder) parts.push(remainder);
  const minimum = Math.min(...model.durations);
  if (parts.length > 1 && parts[parts.length - 1] < minimum) {
    const borrowed = minimum - parts[parts.length - 1];
    parts[parts.length - 2] -= borrowed; parts[parts.length - 1] = minimum;
  }
  if (parts.length < 2 || parts.length > 20 || parts.some(value => !model.durations.includes(value))) throw new Error("当前总时长与单段时长无法拆成 2–20 个合法分段，请增大单段时长或缩短总时长。");
  return parts;
}
function validateModelSpec(spec, model) {
  if (!model) throw new Error("模型能力尚未加载，请重新连接本地服务。");
  if (!spec || !Array.isArray(spec.segments) || spec.segments.length < 2 || spec.segments.length > 20) throw new Error("脚本须包含 2–20 个分段。");
  let total = 0;
  spec.segments.forEach((segment, index) => {
    if (!Number.isInteger(segment?.duration) || !model.durations.includes(segment.duration)) throw new Error(`第 ${index + 1} 段的 ${segment?.duration ?? "空"} 秒与 ${model.label} 不兼容，请选择该模型支持的整数时长。`);
    total += segment.duration;
  });
  if (total < 60 || total > 600) throw new Error("脚本总时长须在 60–600 秒之间。");
  return total;
}
function currentModel() { return store.bootstrap?.models.find(item => item.id === $("#batch-model-family").value); }
function fillSelect(select, values, selected, label = value => value) {
  select.replaceChildren();
  values.forEach(value => { const option = node("option", null, label(value)); option.value = String(value); select.append(option); });
  select.value = String(selected);
}
function syncModelControls(preserveInvalid = false) {
  const model = currentModel(), priorResolution = $("#batch-resolution").value, priorDuration = $("#batch-segment-duration").value;
  const choices = modelSelection(model, priorResolution, Number(priorDuration));
  fillSelect($("#batch-resolution"), model?.resolutions || [], choices.resolution);
  fillSelect($("#batch-segment-duration"), model?.durations || [], choices.segment_duration_seconds, value => `${value} 秒`);
  if (preserveInvalid) { retainSelectValue($("#batch-resolution"), priorResolution); retainSelectValue($("#batch-segment-duration"), priorDuration, `${priorDuration} 秒`); }
  setText("#model-capability-note", model ? `${model.label} · 单段 ${Math.min(...model.durations)}–${Math.max(...model.durations)} 秒 · 支持 ${model.resolutions.join(" / ")}` : "模型不可用，请选择当前服务支持的模型。");
  updatePlanningHint();
}

function updatePlanningHint() {
  const hint = $("#segment-planning-hint");
  try {
    if (store.scenario.mode === "legacy") {
      const spec = customSpec() || template()?.spec; const seconds = spec ? validateModelSpec(spec, currentModel()) : 0;
      hint.textContent = spec ? `沿用脚本的 ${spec.segments.length} 个分段，共 ${seconds} 秒。修改总长请编辑 JSON 中的分段时长。` : "选择模板或导入 JSON 后显示脚本时长。";
      setText("#legacy-duration-summary", spec ? `${seconds} 秒 · ${spec.segments.length} 段（按脚本）` : "按脚本分段累加"); hint.classList.remove("invalid"); return !!spec;
    }
    const parts = plannedDurations(Number($("#scenario-duration").value), Number($("#batch-segment-duration").value), currentModel());
    hint.textContent = `每条计划 ${parts.length} 段，共 ${parts.reduce((sum, value) => sum + value, 0)} 秒；末段按剩余时长调整。`; hint.classList.remove("invalid"); return true;
  } catch (error) { hint.textContent = error.message; hint.classList.add("invalid"); setText("#legacy-duration-summary", "脚本时长待修正"); return false; }
}

function readDefaults() {
  const settings = store.bootstrap?.settings || {};
  const base = settings.defaults || settings;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("seedance-studio-defaults") || "{}"); } catch (_) { /* Storage is optional. */ }
  store.defaults = productionDefaults(store.bootstrap?.models || [], base, saved);
  if (store.defaults.model_family) { try { localStorage.setItem("seedance-studio-defaults", JSON.stringify(store.defaults)); } catch (_) { /* Storage is optional. */ } }
}

function template() { return store.bootstrap?.templates.find(item => item.id === $("#template-select").value); }
function specDuration(spec) {
  if (!spec || !Array.isArray(spec.segments) || !spec.segments.length) throw new Error("场景 JSON 必须包含非空 segments 数组。");
  return spec.segments.reduce((sum, segment, index) => { const seconds = Number(segment.duration); if (!Number.isFinite(seconds) || seconds <= 0) throw new Error(`第 ${index + 1} 段的 duration 必须是大于 0 的秒数。`); return sum + seconds; }, 0);
}
function customSpec() {
  const raw = $("#custom-spec").value.trim();
  if (!raw) return null;
  let spec; try { spec = JSON.parse(raw); } catch (_) { throw new Error("场景 JSON 格式不正确，请检查括号、引号与逗号。"); }
  spec.target_duration_seconds = specDuration(spec); return spec;
}
function updateTemplate() {
  updateScriptStatus();
}
function renderScriptReferences(spec, selected) {
  const panel = $("#script-references"), list = $("#reference-images-list"), warnings = $("#reference-images-warnings"), recommendation = $("#reference-resolution-hint");
  panel.hidden = true; recommendation.hidden = true; list.replaceChildren(); warnings.replaceChildren();
  $("#batch-resolution").removeAttribute("aria-describedby");
  if (store.scenario.mode !== "legacy" || spec?.reference_images == null) return;
  const bindings = spec.reference_images;
  if (Array.isArray(bindings) && !bindings.length) return;
  panel.hidden = false;
  const warn = message => warnings.append(node("p", "reference-warning", message));
  if (!Array.isArray(bindings)) { warn("参考图绑定格式不正确，请检查 reference_images。"); return; }
  const catalogue = store.bootstrap?.reference_images || [];
  bindings.forEach(binding => {
    if (!binding || typeof binding.id !== "string" || !binding.id.trim()) { warn("参考图绑定缺少图片 ID，请检查 reference_images。"); return; }
    const asset = catalogue.find(item => item.id === binding.id);
    if (!asset) { warn(`未找到参考图「${binding.id}」，请检查绑定。`); return; }
    const name = asset.name || asset.id;
    if (!/^[a-zA-Z0-9_-]+$/.test(asset.id) || asset.url !== `/api/reference-images/${encodeURIComponent(asset.id)}`) { warn(`参考图「${name}」暂不可预览。`); return; }
    if (!Array.isArray(binding.segment_ids) || !binding.segment_ids.length) { warn(`参考图「${name}」未绑定分段，请检查 segment_ids。`); return; }
    const segments = [], unresolved = [];
    [...new Set(binding.segment_ids)].forEach(id => {
      const matches = spec.segments.map((segment, index) => ({segment, index})).filter(item => item.segment.id === id);
      if (typeof id !== "string" || matches.length !== 1) unresolved.push(String(id));
      else segments.push(matches[0]);
    });
    if (unresolved.length) warn(`参考图「${name}」的分段绑定无效：${unresolved.join("、")}。`);
    if (!segments.length) return;
    const item = node("article", "script-reference"), link = node("a", "reference-thumbnail"), image = node("img"), details = node("div", "reference-details");
    link.href = asset.url; link.target = "_blank"; link.rel = "noopener"; link.setAttribute("aria-label", `查看${name}原图`);
    image.src = asset.url; image.alt = name; image.decoding = "async";
    if (numeric(asset.width) > 0 && numeric(asset.height) > 0) { image.width = Number(asset.width); image.height = Number(asset.height); }
    image.addEventListener("error", () => { link.hidden = true; details.querySelector(".reference-open-note")?.remove(); if (!details.querySelector(".reference-warning")) details.append(node("p", "reference-warning", "参考图暂时无法加载，请稍后重试。")); }, {once: true});
    link.append(image);
    details.append(node("h4", null, name), node("p", "reference-role", asset.purpose === "phone_screen" ? "用途：手机屏幕布局参考" : "用途尚未标明"));
    const labels = segments.sort((a, b) => a.index - b.index).map(({segment, index}) => `第 ${index + 1} 段 · ${(segment.title || segment.id || `分段 ${index + 1}`)}`);
    details.append(node("p", "reference-segments", `仅用于${labels.join("；")}`));
    if (asset.note) details.append(node("p", "reference-note", asset.note));
    details.append(node("span", "reference-open-note", "点击缩略图查看原图"));
    if (asset.purpose !== "phone_screen") warn(`参考图「${name}」尚未标明用途，请检查绑定。`);
    if (asset.purpose === "phone_screen" && (asset.id === "hotel_booking_screen" || selected?.recommended_resolution === "720p" || spec.recommended_resolution === "720p")) recommendation.hidden = false;
    item.append(link, details); list.append(item);
  });
  if (!recommendation.hidden) $("#batch-resolution").setAttribute("aria-describedby", "reference-resolution-hint");
}
function updateScriptStatus() {
  const status = $("#script-status"); status.classList.remove("invalid");
  try {
    const custom = customSpec(), selected = template();
    const spec = custom || selected?.spec;
    const seconds = spec ? (currentModel() ? validateModelSpec(spec, currentModel()) : specDuration(spec)) : numeric(selected?.duration_seconds);
    const count = spec ? spec.segments.length : numeric(selected?.segment_count);
    setText("#template-summary-title", custom ? typeof custom.name === "string" && custom.name.trim() ? custom.name.trim() : "自定义场景脚本" : selected?.name || "暂无可用模板");
    setText("#template-summary-description", custom ? `自定义脚本 · ${count} 个分段 · 总时长按分段时长累加。` : selected?.description || "连续第一视角 · 独立分段 · 末状态承接");
    setText("#template-duration", `${seconds} s / ${count} 段`);
    status.textContent = `${custom ? "自定义脚本" : "所选模板"} · ${count} 段 · 总时长 ${seconds} 秒`;
    renderScriptReferences(spec, custom ? null : selected);
  } catch (error) {
    status.textContent = error.message; status.classList.add("invalid");
    setText("#template-summary-title", "自定义脚本待修正");
    setText("#template-summary-description", error.message);
    setText("#template-duration", "脚本待修正");
    renderScriptReferences(null);
  }
}
const SCENARIO_INPUTS = new Set(["scenario-description", "scenario-task-type", "scenario-duration", "scenario-quantity", "scenario-diversity", "batch-segment-duration"]);
const SCENARIO_EXAMPLES = {
  kitchen: {type: "manipulation", description: "第一视角在厨房台面上，将杯子、碗和餐具分类，移到对应收纳区。保持手部与物体接触清晰，展示抓取、移动、放置的完整过程。", diversity: "改变物体组合、摆放位置和操作顺序；每条保持同一个厨房、第一视角和操作者身份。"},
  warehouse: {type: "manipulation", description: "第一视角仓储机器人在工作台前识别包裹上的类别标识，将不同尺寸的包裹逐一抓取、搬运并放入对应货箱，最后检查分类结果。", diversity: "变化包裹尺寸、货箱位置和初始遮挡情况，确保标签、接触和放置过程可见。"},
  service: {type: "interaction", description: "第一视角服务机器人在接待台与访客沟通，确认访客需要的物品，从台面取出对应物品，递交给访客并确认交接完成。", diversity: "变化访客请求、物品类型和递交方向；保持礼貌沟通、清晰交接和一致空间布局。"}
};

function scenarioInput() {
  return {description: $("#scenario-description").value.trim(), task_type: $("#scenario-task-type").value, duration_seconds: Number($("#scenario-duration").value), quantity: Number($("#scenario-quantity").value), diversity: $("#scenario-diversity").value.trim(), model_family: $("#batch-model-family").value, resolution: $("#batch-resolution").value, segment_duration_seconds: Number($("#batch-segment-duration").value)};
}
function scenarioReady() {
  const state = store.scenario;
  return !state.busy && state.drafts.length > 0 && state.sourceRevision === state.draftRevision && state.drafts.some(draft => draft?.selected) && !generationValidationMessage();
}

function setScenarioStatus(message, warning = false) {
  const status = $("#scenario-status"); status.textContent = message; status.hidden = !message; status.classList.toggle("warning", warning);
}
function updateScenarioState() {
  const state = store.scenario, selected = state.drafts.filter(draft => draft?.selected).length;
  const stale = state.drafts.length > 0 && state.sourceRevision !== state.draftRevision;
  $("#scenario-drafts").hidden = state.mode !== "scenario" || !state.drafts.length;
  $("#scenario-drafts").classList.toggle("stale", stale);
  setText("#drafts-summary", `已选择 ${selected} / ${state.drafts.length} 条 · 展开脚本可编辑每个分段`);
  setText("#selected-production-count", state.mode === "scenario" ? `${selected} 条已选脚本` : `${$("#batch-quantity").value || 0} 条视频`);
  const modelReady = !!currentModel(), planReady = updatePlanningHint();
  $("#expand-scenario").disabled = state.busy || store.submitting || generationWorkspace.submitted || !store.connected || state.mode !== "scenario" || !modelReady || !planReady || store.bootstrap?.scenarios?.available === false;
  $("#expand-scenario").replaceChildren(icon(state.busy ? "clock" : "plus"), document.createTextNode(state.busy ? "正在扩展脚本…" : state.drafts.length ? "重新扩展脚本" : "扩展为多条脚本"));
  const cannotCreate = store.submitting || generationWorkspace.submitted || store.voiceBusy || !store.connected || !modelReady || !!generationValidationMessage() || (state.mode === "scenario" && !scenarioReady());
  $("#estimate-button").disabled = cannotCreate || store.estimateBusy; $("#create-batch").disabled = cannotCreate;
  $("#generation-new").disabled = store.submitting; $("#generation-draft-select").disabled = store.submitting;
  if (generationWorkspace.submitted) $$("input, textarea, select, button", $("#batch-form")).forEach(control => { if (!["generation-back", "generation-next"].includes(control.id)) control.disabled = true; });
  if (stale && !state.busy) setScenarioStatus("模型、场景描述或扩展参数已修改。原草稿仍保留，请重新扩展后生成。", true);
  if (store.bootstrap?.scenarios?.available === false) setScenarioStatus("当前服务尚未启用场景扩展，可切换到「模板与 JSON」创建批次。", true);
  showGenerationNote();
}

function setCreationMode(mode) {
  if (store.submitting) return;
  const state = store.scenario; if (state.mode !== mode) { state.sourceRevision++; invalidateEstimate(); }
  state.mode = mode === "legacy" ? "legacy" : "scenario";
  ["scenario", "legacy"].forEach(name => {
    const active = name === state.mode, tab = $(`#${name}-mode-tab`), pane = $(`#${name}-mode-pane`);
    tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1; pane.hidden = !active;
    $$("input, textarea, select, button", pane).forEach(input => { input.disabled = !active || generationWorkspace.submitted; });
  });
  $$("[data-creation-mode]").forEach(group => { const active = group.dataset.creationMode === state.mode; group.hidden = !active; $$("input, textarea, select, button", group).forEach(control => { control.disabled = !active || generationWorkspace.submitted; }); });
  $$(".draft-editor").forEach((fieldset, index) => { fieldset.disabled = state.mode !== "scenario" || !state.drafts[index]?.selected || generationWorkspace.submitted; });
  $("#legacy-review").hidden = state.mode !== "legacy";
  updateScriptStatus(); updateScenarioState(); renderGenerationSummary();
}

function scenarioChanged() {
  store.scenario.sourceRevision++; formError("#scenario-error", "");
  if (store.scenario.busy) setScenarioStatus("输入已修改，当前扩展结果将被丢弃。等待本次请求结束后可重新扩展。", true);
  else if (!store.scenario.drafts.length) setScenarioStatus("");
  updateScenarioState();
}
function modelChanged() {
  syncModelControls(); scenarioChanged(); renderScenarioDrafts(); updateScriptStatus(); invalidateEstimate(); updateScenarioState(); persistGenerationDraft();
}

async function expandScenario() {
  const state = store.scenario;
  if (state.busy || store.submitting || generationWorkspace.submitted || state.mode !== "scenario") return;
  if (!store.connected) { formError("#scenario-error", "连接本地服务后才能扩展脚本；草稿仍可继续编辑。"); return; }
  formError("#scenario-error", "");
  const input = scenarioInput();
  if (input.description.length < 8 || input.description.length > 4000) { formError("#scenario-error", "请用 8–4000 个字描述任务场景。"); $("#scenario-description").focus(); return; }
  if (!Number.isInteger(input.quantity) || input.quantity < 1 || input.quantity > 6 || ![60, 90, 120, 180].includes(input.duration_seconds) || !["manipulation", "interaction"].includes(input.task_type) || input.diversity.length > 2000) { formError("#scenario-error", "请检查扩展数量（1–6 条）、时长和多样性要求（最多 2000 字）。"); return; }
  try { plannedDurations(input.duration_seconds, input.segment_duration_seconds, currentModel()); }
  catch (error) { formError("#scenario-error", error.message); return; }
  invalidateEstimate();
  const session = store.batchSession, revision = store.formRevision, sourceRevision = state.sourceRevision, request = ++state.request;
  state.busy = true; state.controller = new AbortController(); updateScenarioState();
  setScenarioStatus(store.bootstrap?.simulation ? "正在构建模拟脚本，仅用于验证流程。" : "文本模型正在规划不同脚本，尚未提交视频生成任务…");
  try {
    const result = await api("/api/scenarios/expand", input, {signal: state.controller.signal});
    if (session !== store.batchSession || request !== state.request) return;
    if (revision !== store.formRevision || sourceRevision !== state.sourceRevision || state.mode !== "scenario") { setScenarioStatus("扩展期间配置发生变化，已丢弃这次结果。请按当前配置重新扩展。", true); return; }
    if (!Array.isArray(result.variants) || result.variants.length !== input.quantity) throw new Error("扩展结果的脚本数量不完整，请重试；原草稿已保留。");
    const drafts = result.variants.map(spec => {
      const copy = JSON.parse(JSON.stringify(spec)); validateModelSpec(copy, currentModel());
      if (!copy.dataset || !Array.isArray(copy.dataset.objects) || typeof copy.dataset.variation !== "string" || copy.segments.some(segment => !Array.isArray(segment.required_actions) || !Array.isArray(segment.action_labels))) throw new Error("扩展结果缺少脚本规划字段，请重试；原草稿已保留。");
      return {spec: copy, selected: true};
    });
    state.drafts = drafts; state.draftRevision = sourceRevision; state.planner = result.planner || {};
    renderScenarioDrafts(); invalidateEstimate();
    const simulation = state.planner.simulation === true;
    setScenarioStatus(`${simulation ? "模拟脚本" : "脚本已扩展"} · ${drafts.length} 条${state.planner.model ? ` · ${state.planner.model}` : ""}。请逐条检查动作和状态，再选择需要生成的脚本。`);
    setGenerationStep(2); persistGenerationDraft();
    if (store.page === "create") $("#generation-step-review").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) {
    if (session === store.batchSession && request === state.request) { formError("#scenario-error", `${error.message}${state.drafts.length ? " 原草稿已保留。" : ""}`); setScenarioStatus(""); }
  } finally {
    if (session === store.batchSession && request === state.request) { state.busy = false; state.controller = null; updateScenarioState(); persistGenerationDraft(); }
  }
}
function draftField(label, value, change, options = {}) {
  const field = node("label", `field${options.className ? ` ${options.className}` : ""}`), title = node("span", null, label);
  const input = node(options.choices ? "select" : options.multiline ? "textarea" : "input");
  if (options.choices) {
    fillSelect(input, options.choices, value, seconds => `${seconds} 秒`);
    if (!options.choices.includes(value)) { const invalid = node("option", null, `${value} 秒 · 当前模型不支持`); invalid.value = String(value); invalid.disabled = true; invalid.selected = true; input.prepend(invalid); }
  } else input.value = value ?? "";
  if (options.multiline) input.rows = options.rows || 2;
  else if (!options.choices) input.type = options.number ? "number" : "text";
  if (!options.choices) input.maxLength = options.maxLength || 4000;
  input.addEventListener("input", () => { change(options.number ? Number(input.value) : input.value); invalidateEstimate(); updateScenarioState(); persistGenerationDraft(); });
  field.append(title, input); return field;
}
function lines(value) { return String(value).split(/\n/).map(item => item.trim()).filter(Boolean); }
function tags(value) { return String(value).split(/[,，\n]/).map(item => item.trim()).filter(Boolean); }
function renderScenarioDrafts() {
  const list = $("#drafts-list"); list.replaceChildren();
  store.scenario.drafts.forEach((draft, index) => {
    if (!draft?.spec || !Array.isArray(draft.spec.segments) || !draft.spec.dataset || !Array.isArray(draft.spec.dataset.objects) || draft.spec.segments.some(segment => !segment || !Array.isArray(segment.required_actions) || !Array.isArray(segment.action_labels))) { list.append(node("p", "form-error", `第 ${index + 1} 条草稿结构不完整，原内容已保留。请重新扩展。`)); return; }
    const spec = draft.spec, article = node("article", "scenario-draft"), top = node("div", "draft-top"), selection = node("label", "draft-select");
    const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.disabled = generationWorkspace.submitted; checkbox.checked = draft.selected; checkbox.setAttribute("aria-label", `选择第 ${index + 1} 条脚本`);
    selection.append(checkbox, node("span", "draft-index", String(index + 1).padStart(2, "0")));
    const facts = node("span", "draft-facts"), heading = node("h4", "draft-title", spec.name);
    const updateFacts = () => { const total = spec.segments.reduce((sum, segment) => sum + numeric(segment.duration), 0); spec.target_duration_seconds = total; facts.textContent = `${duration(total)} / ${spec.segments.length} 段`; };
    updateFacts(); top.append(selection, heading, facts);
    const editor = node("fieldset", "draft-editor"); editor.disabled = !draft.selected || generationWorkspace.submitted || store.scenario.mode !== "scenario";
    const intro = node("div", "draft-intro");
    intro.append(draftField("脚本名称", spec.name, value => { spec.name = value; heading.textContent = value || `脚本 ${index + 1}`; }, {maxLength: 120}), draftField("差异设计", spec.dataset.variation, value => { spec.dataset.variation = value; }, {multiline: true, maxLength: 2000}));
    const details = node("details", "draft-details"), summary = node("summary", null, "编辑任务与分段脚本");
    const content = node("div", "draft-detail-content"), taskGrid = node("div", "form-grid");
    taskGrid.append(draftField("任务标签", spec.dataset.task_label, value => { spec.dataset.task_label = value; }, {maxLength: 200}), draftField("涉及物体（逗号分隔）", spec.dataset.objects.join("，"), value => { spec.dataset.objects = tags(value); }, {maxLength: 2000}));
    content.append(taskGrid, draftField("全局画面与连续性要求", spec.global_prompt, value => { spec.global_prompt = value; }, {multiline: true, rows: 3, maxLength: 8000}));
    spec.segments.forEach((segment, segmentIndex) => {
      const section = node("section", "draft-segment"), segmentHeading = node("h5", null, `分段 ${segmentIndex + 1}`), titleGrid = node("div", "draft-segment-title");
      titleGrid.append(draftField("分段名称", segment.title, value => { segment.title = value; }, {maxLength: 200}), draftField("时长（秒）", segment.duration, value => { segment.duration = value; updateFacts(); }, {number: true, choices: currentModel()?.durations || []}));
      const states = node("div", "form-grid");
      states.append(draftField("开始状态", segment.expected_start_state, value => { segment.expected_start_state = value; }, {multiline: true}), draftField("结束状态", segment.expected_end_state, value => { segment.expected_end_state = value; }, {multiline: true}));
      section.append(segmentHeading, titleGrid, draftField("必需动作（每行一项）", segment.required_actions.join("\n"), value => { segment.required_actions = lines(value); }, {multiline: true}), draftField("视频提示词", segment.prompt, value => { segment.prompt = value; }, {multiline: true, rows: 4, maxLength: 8000}), states, draftField("动作标签（逗号分隔）", segment.action_labels.join("，"), value => { segment.action_labels = tags(value); }, {maxLength: 2000}));
      content.append(section);
    });
    details.append(summary, content); editor.append(intro, details); article.append(top, editor); list.append(article);
    checkbox.addEventListener("change", () => { draft.selected = checkbox.checked; editor.disabled = !draft.selected; article.classList.toggle("not-selected", !draft.selected); invalidateEstimate(); updateScenarioState(); persistGenerationDraft(); });
  });
  updateScenarioState();
}
function selectedVariants() {
  if (!scenarioReady()) throw new Error(store.scenario.drafts.length ? "请重新扩展当前场景，并至少选择一条脚本。" : "请先描述场景并扩展脚本。");
  return store.scenario.drafts.filter(draft => draft?.selected).map((draft, index) => {
    const spec = JSON.parse(JSON.stringify(draft.spec)), seconds = validateModelSpec(spec, currentModel()), name = `第 ${index + 1} 条已选脚本`;
    if (typeof spec.name !== "string" || !spec.name.trim() || typeof spec.global_prompt !== "string" || !spec.global_prompt.trim()) throw new Error(`${name}需要填写名称和全局要求。`);
    spec.segments.forEach((segment, segmentIndex) => {
      if (![segment.title, segment.prompt, segment.expected_start_state, segment.expected_end_state].every(value => typeof value === "string" && value.trim()) || !segment.required_actions.length || !segment.action_labels.length) throw new Error(`${name}的第 ${segmentIndex + 1} 段需要完整的名称、提示词、动作与前后状态。`);
    });
    if (!spec.dataset.variation.trim() || !String(spec.dataset.task_label || "").trim() || !spec.dataset.objects.length) throw new Error(`${name}需要填写差异设计、任务标签和涉及物体。`);
    spec.target_duration_seconds = seconds; spec.dataset.annotation_scope = "planned"; return spec;
  });
}
function invalidateEstimate() {
  store.formRevision++; store.estimate = null; store.estimatePayload = "";
  setText("#estimate-total", "生成时自动估算"); setText("#estimate-description", "可提前查看费用，也可直接开始生成。文本扩展费用另计。");
  $("#estimate-jobs").hidden = true; $("#estimate-jobs").replaceChildren();
}
function stopBatchRequests() {
  store.batchSession++; store.estimateRequest++; store.estimateBusy = false; store.voiceRequest++; store.voiceBusy = false;
  store.scenario.request++; store.scenario.controller?.abort(); store.scenario.controller = null; store.scenario.busy = false;
  invalidateEstimate();
}
function openBatch() {
  if (!generationWorkspace.initialized && store.bootstrap) initializeGeneration();
  navigatePage("create");
}

function creationPayload() {
  if (generationWorkspace.submitted) throw new Error("此草稿已经提交。请新建草稿后再生成。");
  if (store.voiceBusy) throw new Error("声音参考仍在上传，请等待校验完成。");
  const model = currentModel();
  if (!model) throw new Error("模型能力尚未加载或草稿模型不可用，请修改模型或重新连接本地服务。");
  const payload = {name: $("#batch-name").value.trim(), model_family: model.id, resolution: $("#batch-resolution").value, segment_duration_seconds: Number($("#batch-segment-duration").value), concurrency: Number($("#batch-concurrency").value), review_mode: "auto"};
  if (!payload.name) throw new Error("请输入批次名称。");
  if (!model.resolutions.includes(payload.resolution)) throw new Error("请选择当前模型支持的输出分辨率。");
  if (!model.durations.includes(payload.segment_duration_seconds)) throw new Error("请选择当前模型支持的单段时长。");
  if (!Number.isInteger(payload.concurrency) || payload.concurrency < 1 || payload.concurrency > 3) throw new Error("并发数须为 1–3 条。");
  if (store.scenario.mode === "scenario") { payload.variants = selectedVariants(); payload.quantity = payload.variants.length; }
  else {
    const spec = customSpec(); if (!spec && !template()?.spec) throw new Error("请选择模板或导入完整场景 JSON。");
    validateModelSpec(spec || template().spec, model);
    payload.quantity = Number($("#batch-quantity").value); if (!Number.isInteger(payload.quantity) || payload.quantity < 1 || payload.quantity > 20) throw new Error("视频数量须为 1–20 条。");
    payload.variations = $("#variations").value.trim();
    if (spec) payload.spec = spec; else payload.template_id = $("#template-select").value;
  }
  if ($("#batch-profile").value.trim()) payload.profile = $("#batch-profile").value.trim();
  if (store.voiceAsset) payload.voice_asset_id = store.voiceAsset.asset_id;
  return payload;
}

async function estimateBudget() {
  if (store.estimateBusy || store.submitting) return;
  const button = $("#estimate-button"); formError("#batch-error", "");
  let payload; try { payload = creationPayload(); } catch (error) { formError("#batch-error", error.message); return; }
  invalidateEstimate();
  const signature = JSON.stringify(payload), session = store.batchSession, revision = store.formRevision, request = ++store.estimateRequest;
  store.estimateBusy = true; button.disabled = true; button.textContent = "正在估算…";
  try {
    const estimate = await api("/api/batches/estimate", payload);
    if (session !== store.batchSession || request !== store.estimateRequest) return;
    if (revision !== store.formRevision || JSON.stringify(creationPayload()) !== signature) { formError("#batch-error", "估算期间配置发生变化，请重新估算。"); return; }
    store.estimate = estimate; store.estimatePayload = signature;
    setText("#estimate-total", money(estimate.estimated_total_cny));
    setText("#estimate-description", `${payload.quantity} 条视频 · 平均每条 ${money(estimate.estimated_per_job_cny)}。仅供费用参考，实际费用以任务记录为准；文本扩展费用另计。`);
    const jobs = $("#estimate-jobs"); jobs.replaceChildren();
    if (Array.isArray(estimate.jobs)) estimate.jobs.forEach((job, index) => { const row = node("div", "estimate-job"); row.append(node("span", null, job.name || `脚本 ${index + 1}`), node("span", null, `${duration(job.duration_seconds)} · ${job.segment_count} 段`), node("strong", null, money(job.estimated_cny))); jobs.append(row); });
    jobs.hidden = !jobs.childElementCount;
  } catch (error) { if (session === store.batchSession && request === store.estimateRequest) { setText("#estimate-total", "暂未取得估价"); setText("#estimate-description", `${error.message} 可直接开始生成，创建时会自动估算。`); } }
  finally { if (session === store.batchSession && request === store.estimateRequest) { store.estimateBusy = false; button.replaceChildren(document.createTextNode("重新估算"), icon("refresh")); updateScenarioState(); } }
}
async function createBatch(event) {
  event.preventDefault(); if (store.submitting || generationWorkspace.submitted) return;
  formError("#batch-error", "");
  if (!store.connected) { formError("#batch-error", "连接本地服务后才能开始生成；草稿仍可继续编辑。"); return; }
  let payload; try { payload = creationPayload(); } catch (error) { formError("#batch-error", error.message); return; }
  const frozenPayload = generationClone(payload), session = store.batchSession;
  const origin = {page: store.page, epoch: store.navigationEpoch || 0};
  persistGenerationDraft(); store.estimateRequest++; store.estimateBusy = false; store.submitting = true;
  const controls = $$("input, textarea, select, button", $("#batch-form")).map(control => [control, control.disabled]); controls.forEach(([control]) => { control.disabled = true; }); updateScenarioState();
  try {
    const result = await api("/api/batches", frozenPayload);
    if (session !== store.batchSession) return;
    generationWorkspace.submitted = true; persistGenerationDraft();
    toast(`批次已创建，${payload.quantity} 条视频已进入生成队列${Number.isFinite(result.batch?.estimated_cny) ? `，预计 ${money(result.batch.estimated_cny)}` : "，费用待核实"}。`);
    try { await onCreationSubmitted(result, frozenPayload, origin); } catch (_) { toast("批次已创建；任务列表暂未刷新，请稍后刷新看板。"); }
  } catch (error) { if (session === store.batchSession) formError("#batch-error", error.message); }
  finally { if (session === store.batchSession) { store.submitting = false; controls.forEach(([control, disabled]) => { control.disabled = disabled; }); setCreationMode(store.scenario.mode); updateScenarioState(); } }
}

function setupVoiceUpload() {
  const section = node("div", "voice-upload"), header = node("div", "voice-upload-header"), intro = node("div");
  intro.append(node("strong", null, "声音参考"), node("p", null, "选填 · WAV / MP3 · 2–30 秒 · 最大 15 MB"));
  const label = node("label", "button button-small file-button"); label.append(icon("upload"), node("span", null, "上传声音"));
  const input = node("input", "visually-hidden"); input.type = "file"; input.accept = ".wav,.mp3,audio/wav,audio/mpeg"; input.id = "voice-file"; label.append(input); header.append(intro, label);
  const status = node("p", "voice-status", "未上传，不使用声音参考。"); status.id = "voice-status"; status.setAttribute("aria-live", "polite");
  const remove = node("button", "text-button", "移除声音参考"); remove.type = "button"; remove.id = "remove-voice"; remove.hidden = true;
  remove.addEventListener("click", () => { store.voiceRequest++; store.voiceBusy = false; store.voiceAsset = null; input.value = ""; input.disabled = false; status.textContent = "未上传，不使用声音参考。"; remove.hidden = true; invalidateEstimate(); updateScenarioState(); persistGenerationDraft(); });
  input.addEventListener("change", async () => {
    const file = input.files[0]; if (!file) return; status.classList.remove("invalid");
    if (!/\.(wav|mp3)$/i.test(file.name) || file.size > 15 * 1024 * 1024) { status.textContent = "请选择 15 MB 以内的 WAV 或 MP3 文件。"; status.classList.add("invalid"); input.value = ""; return; }
    const session = store.batchSession, request = ++store.voiceRequest;
    status.textContent = "正在上传并校验声音…"; input.disabled = true; store.voiceBusy = true; invalidateEstimate(); updateScenarioState();
    const data = new FormData(); data.append("file", file);
    try { const asset = await api("/api/assets/audio", data); if (session !== store.batchSession || request !== store.voiceRequest) return; store.voiceAsset = {asset_id: asset.asset_id, name: asset.name || file.name, duration_seconds: asset.duration_seconds}; status.textContent = `${asset.name || file.name} · ${duration(asset.duration_seconds)} · 已上传`; remove.hidden = false; invalidateEstimate(); }
    catch (error) { if (session === store.batchSession && request === store.voiceRequest) { status.textContent = error.message; status.classList.add("invalid"); } }
    finally { if (session === store.batchSession && request === store.voiceRequest) { input.disabled = false; store.voiceBusy = false; updateScenarioState(); persistGenerationDraft(); } }
  });
  section.append(header, status, remove); ($("#generation-reference-inputs") || $("#creative-inputs")).append(section);
}


const GENERATION_DRAFT_VERSION = 1;
const GENERATION_FIELDS = ["batch-name", "scenario-description", "scenario-task-type", "scenario-duration", "scenario-quantity", "scenario-diversity", "batch-model-family", "batch-resolution", "batch-segment-duration", "batch-concurrency", "batch-quantity", "variations", "template-select", "custom-spec", "batch-profile"];
const generationWorkspace = {initialized: false, environment: null, currentId: null, records: [], sessions: new Map(), storageError: "", storageBlocked: false, submitted: false, reuseNote: "", bound: false, pending: null};
function generationStorageKey() { return `seedance-studio-generation:v${GENERATION_DRAFT_VERSION}:${generationWorkspace.environment || (store.bootstrap?.simulation ? "simulation" : "real")}`; }
function generationClone(value) { return JSON.parse(JSON.stringify(value)); }
function draftSafeCopy(value) {
  if (Array.isArray(value)) return value.map(draftSafeCopy);
  if (value && typeof value === "object") {
    const clean = {};
    Object.entries(value).forEach(([key, item]) => {
      if (/^(?:csrf(?:_token)?|api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|secret|password|content|data|base64|media_bytes)$/i.test(key) || key === "__proto__" || key === "constructor") return;
      clean[key] = draftSafeCopy(item);
    });
    return clean;
  }
  return typeof value === "string" && /^data:(?:audio|image|video)\//i.test(value) ? "" : value;
}
function publicPlanner(planner) {
  if (!planner || typeof planner !== "object") return null;
  const out = {};
  ["model", "simulation", "usage", "response_id", "video_model_family", "video_model", "resolution", "segment_duration_seconds", "estimated_cny", "cost_cny"].forEach(key => { if (planner[key] !== undefined) out[key] = draftSafeCopy(planner[key]); });
  return out;
}
function generationRecord() {
  const fields = {};
  GENERATION_FIELDS.forEach(id => { if ($(`#${id}`)) fields[id] = $(`#${id}`).value; });
  if (fields["custom-spec"]) { try { fields["custom-spec"] = JSON.stringify(draftSafeCopy(JSON.parse(fields["custom-spec"])), null, 2); } catch (_) { /* Keep unfinished JSON editable. */ } }
  return {id: generationWorkspace.currentId, name: fields["batch-name"]?.trim() || "未命名草稿", updated_at: new Date().toISOString(), status: generationWorkspace.submitted ? "submitted" : "draft", step: store.generationStep || 1, fields,
    scenario: {mode: store.scenario.mode, drafts: draftSafeCopy(store.scenario.drafts), source_revision: store.scenario.sourceRevision, draft_revision: store.scenario.draftRevision, planner: publicPlanner(store.scenario.planner)},
    voice_asset: store.voiceAsset?.asset_id ? {asset_id: String(store.voiceAsset.asset_id), name: String(store.voiceAsset.name || "声音参考")} : null, reuse_note: generationWorkspace.reuseNote};
}
function renderDraftPicker() {
  const select = $("#generation-draft-select"); if (!select) return;
  select.replaceChildren();
  generationWorkspace.records.forEach(record => { const option = node("option", null, `${record.name || "需要修复的草稿"}${record.status === "submitted" ? " · 已提交" : ""}`); option.value = record.id; select.append(option); });
  select.value = generationWorkspace.currentId || "";
  if ($("#generation-drafts-control")) $("#generation-drafts-control").hidden = generationWorkspace.records.length < 2;
}
function updateDraftSaveStatus() {
  setText("#draft-save-status", generationWorkspace.storageError ? `仅当前会话保存 · ${generationWorkspace.storageError}。请勿关闭或刷新页面。` : generationWorkspace.submitted ? "已提交归档 · 保留此草稿" : "草稿已自动保存到此浏览器");
}
function persistGenerationDraft() {
  if (!generationWorkspace.initialized || !generationWorkspace.currentId) {
    if (GENERATION_FIELDS.some(id => $(`#${id}`)?.value)) generationWorkspace.pending = generationRecord();
    setText("#draft-save-status", "仅当前会话保存 · 连接后会按服务环境保存草稿。");
    return false;
  }
  const index = generationWorkspace.records.findIndex(record => record.id === generationWorkspace.currentId);
  if (!(generationWorkspace.submitted && index >= 0 && generationWorkspace.records[index].status === "submitted")) {
    const record = generationRecord(); if (index < 0) generationWorkspace.records.push(record); else generationWorkspace.records[index] = record;
  }
  const envelope = {version: GENERATION_DRAFT_VERSION, current_id: generationWorkspace.currentId, drafts: generationWorkspace.records};
  generationWorkspace.sessions.set(generationWorkspace.environment, envelope);
  if (!generationWorkspace.storageBlocked) {
    try { localStorage.setItem(generationStorageKey(), JSON.stringify(envelope)); generationWorkspace.storageError = ""; }
    catch (_) { generationWorkspace.storageError = "浏览器存储不可用或空间已满"; }
  }
  renderDraftPicker(); updateDraftSaveStatus(); return !generationWorkspace.storageError;
}
function retainSelectValue(select, value, label = String(value)) {
  if (!select) return;
  const text = String(value ?? "");
  if (text && !Array.from(select.children || []).some(option => option.value === text)) { const option = node("option", null, `${label} · 需修改`); option.value = text; select.append(option); }
  select.value = text;
}
function refillGenerationCatalog(preserve = false) {
  const model = preserve ? $("#batch-model-family").value : store.defaults.model_family;
  const selectedTemplate = preserve ? $("#template-select").value : store.bootstrap?.templates[0]?.id;
  fillSelect($("#batch-model-family"), store.bootstrap?.models.map(item => item.id) || [], model, id => store.bootstrap.models.find(item => item.id === id)?.label || id);
  if (preserve) retainSelectValue($("#batch-model-family"), model);
  fillSelect($("#template-select"), store.bootstrap?.templates.map(item => item.id) || [], selectedTemplate, id => store.bootstrap.templates.find(item => item.id === id)?.name || id);
  if (preserve && selectedTemplate) retainSelectValue($("#template-select"), selectedTemplate);
  syncModelControls(preserve);
}
function generationValidationMessage() {
  try {
    const model = currentModel(); if (!model) throw new Error("草稿模型已不可用，请修改模型后重新检查脚本。");
    if (!model.resolutions.includes($("#batch-resolution").value)) throw new Error("草稿分辨率与当前模型不兼容，请修改。");
    if (!model.durations.includes(Number($("#batch-segment-duration").value))) throw new Error("草稿单段时长与当前模型不兼容，请修改。");
    if (store.scenario.mode === "scenario") {
      plannedDurations(Number($("#scenario-duration").value), Number($("#batch-segment-duration").value), model);
      store.scenario.drafts.filter(draft => draft?.selected).forEach(draft => {
        validateModelSpec(draft?.spec, model);
        const spec = draft.spec;
        if (!spec.dataset || typeof spec.dataset.variation !== "string" || !Array.isArray(spec.dataset.objects) || spec.segments.some(segment => !Array.isArray(segment.required_actions) || !Array.isArray(segment.action_labels))) throw new Error("草稿规划字段不完整，请重新扩展。");
      });
    } else { const spec = customSpec() || template()?.spec; if (spec) validateModelSpec(spec, model); }
    return "";
  } catch (error) { return `${error.message} 原草稿已保留；可修改参数或重新扩展。`; }
}
function showGenerationNote() {
  const messages = [generationWorkspace.reuseNote, generationValidationMessage()];
  if (generationWorkspace.submitted) messages.push("此草稿已提交并归档。继续制作请新建草稿，或从成品复用配置。");
  const note = $("#generation-reuse-note"); if (note) { note.textContent = messages.filter(Boolean).join(" "); note.hidden = !note.textContent; }
}
function restoreGenerationRecord(record) {
  if (!record || !record.fields || typeof record.fields !== "object" || !record.scenario || !Array.isArray(record.scenario.drafts)) { toast("这份草稿结构不完整，已保留原记录；请选择其他草稿或新建。"); return false; }
  $$("input, textarea, select, button", $("#batch-form")).forEach(control => { control.disabled = false; });
  generationWorkspace.currentId = record.id; generationWorkspace.submitted = record.status === "submitted"; generationWorkspace.reuseNote = typeof record.reuse_note === "string" ? record.reuse_note : "";
  refillGenerationCatalog(false);
  GENERATION_FIELDS.forEach(id => { const control = $(`#${id}`), value = typeof record.fields[id] === "string" ? record.fields[id] : ""; if (!control) return; if (control.tagName === "SELECT") retainSelectValue(control, value); else control.value = value; });
  syncModelControls(true);
  // Select values may no longer exist in the live catalog. Keep them visibly invalid.
  ["scenario-duration", "scenario-task-type", "batch-concurrency"].forEach(id => retainSelectValue($(`#${id}`), record.fields[id]));
  const saved = record.scenario;
  store.scenario.mode = saved.mode === "legacy" ? "legacy" : "scenario";
  store.scenario.drafts = generationClone(saved.drafts); store.scenario.sourceRevision = Number.isInteger(saved.source_revision) ? saved.source_revision : 0; store.scenario.draftRevision = Number.isInteger(saved.draft_revision) ? saved.draft_revision : -1;
  store.scenario.planner = publicPlanner(saved.planner); store.scenario.busy = false; store.scenario.controller = null;
  store.voiceAsset = record.voice_asset && typeof record.voice_asset.asset_id === "string" && record.voice_asset.asset_id ? {asset_id: record.voice_asset.asset_id, name: String(record.voice_asset.name || "声音参考")} : null;
  renderVoiceStatus(); setCreationMode(store.scenario.mode); renderScenarioDrafts(); setGenerationStep(record.step === 2 ? 2 : 1); showGenerationNote(); return true;
}
function resetGenerationFields() {
  $$("input, textarea, select, button", $("#batch-form")).forEach(control => { control.disabled = false; });
  GENERATION_FIELDS.forEach(id => { if ($(`#${id}`)) $(`#${id}`).value = ""; });
  const today = new Date().toLocaleDateString("zh-CN", {month: "2-digit", day: "2-digit"}).replaceAll("/", ".");
  $("#batch-name").value = `具身场景数据 · ${today}`;
  $("#scenario-task-type").value = "manipulation"; $("#scenario-duration").value = String(store.bootstrap?.scenarios?.default_duration || 60); $("#scenario-quantity").value = String(store.bootstrap?.scenarios?.default_quantity || 3); $("#batch-quantity").value = "1";
  refillGenerationCatalog(false); $("#batch-resolution").value = store.defaults.resolution; $("#batch-segment-duration").value = String(store.defaults.segment_duration_seconds); $("#batch-concurrency").value = String(store.defaults.concurrency);
  store.voiceAsset = null; store.scenario.drafts = []; store.scenario.sourceRevision = 0; store.scenario.draftRevision = -1; store.scenario.planner = null;
  generationWorkspace.submitted = false; generationWorkspace.reuseNote = "";
  renderVoiceStatus(); setScenarioStatus(""); formError("#batch-error", ""); formError("#scenario-error", ""); setCreationMode("scenario"); renderScenarioDrafts(); setGenerationStep(1);
}
function newGenerationDraft() {
  if (store.submitting || !store.bootstrap) return;
  persistGenerationDraft(); stopBatchRequests();
  generationWorkspace.currentId = `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
  resetGenerationFields(); persistGenerationDraft(); navigatePage("create");
}
function switchGenerationDraft(id) {
  if (store.submitting || id === generationWorkspace.currentId) return;
  const record = generationWorkspace.records.find(item => item.id === id); if (!record) return;
  persistGenerationDraft(); stopBatchRequests();
  if (restoreGenerationRecord(record)) persistGenerationDraft(); else renderDraftPicker();
}
function initializeGeneration() {
  if (!store.bootstrap) return;
  const pending = !generationWorkspace.initialized ? generationWorkspace.pending : null;
  const environment = store.bootstrap.simulation ? "simulation" : "real";
  if (generationWorkspace.initialized && generationWorkspace.environment === environment) { refillGenerationCatalog(true); renderScenarioDrafts(); updateScriptStatus(); updateScenarioState(); return; }
  if (generationWorkspace.initialized) { persistGenerationDraft(); stopBatchRequests(); }
  generationWorkspace.environment = environment; generationWorkspace.storageError = ""; generationWorkspace.storageBlocked = false; generationWorkspace.records = []; generationWorkspace.currentId = null;
  readDefaults(); let saved = generationWorkspace.sessions.get(environment);
  if (!saved) {
    try { const raw = localStorage.getItem(generationStorageKey()); if (raw) { saved = JSON.parse(raw); if (saved.version !== GENERATION_DRAFT_VERSION || !Array.isArray(saved.drafts)) throw new Error("unsupported draft format"); } }
    catch (_) { generationWorkspace.storageError = "无法读取旧草稿，原存储内容已保留"; generationWorkspace.storageBlocked = true; saved = null; }
  }
  generationWorkspace.initialized = true;
  generationWorkspace.records = Array.isArray(saved?.drafts) ? saved.drafts.filter(record => record && typeof record.id === "string") : [];
  const current = pending ? null : generationWorkspace.records.find(record => record.id === saved?.current_id) || generationWorkspace.records[0];
  if (!current || !restoreGenerationRecord(current)) { generationWorkspace.currentId = `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`; resetGenerationFields(); }
  if (pending) {
    GENERATION_FIELDS.forEach(id => { const control = $(`#${id}`), value = pending.fields[id]; if (!control || !value) return; if (control.tagName === "SELECT") retainSelectValue(control, value); else control.value = value; });
    syncModelControls(true); setCreationMode(pending.scenario.mode); setGenerationStep(1); generationWorkspace.pending = null;
  }
  setText("#planner-cost-note", store.bootstrap.simulation ? "模拟环境：生成演示草稿，不调用文本或视频模型。" : "扩展仅调用文本模型，可能产生文本模型费用；此时不会生成视频。");
  if ($("#simulation-notice")) $("#simulation-notice").hidden = !store.bootstrap.simulation;
  setText("#create-hint", store.bootstrap.simulation ? "模拟环境：仅验证流程，不调用付费模型。" : "点击开始即提交生成，费用自动估算并展示。");
  persistGenerationDraft();
}
function setGenerationStep(step) {
  store.generationStep = step === 2 ? 2 : 1;
  $("#generation-step-input").hidden = store.generationStep !== 1; $("#generation-step-review").hidden = store.generationStep !== 2;
  $$("[data-generation-step]").forEach(button => { const active = Number(button.dataset.generationStep) === store.generationStep; button.classList.toggle("active", active); button.setAttribute("aria-current", active ? "step" : "false"); });
  $("#legacy-review").hidden = store.scenario.mode !== "legacy";
  updateScenarioState(); renderGenerationSummary();
}
function renderGenerationSummary() {
  const selected = store.scenario.drafts.filter(draft => draft?.selected).length;
  const seconds = store.scenario.drafts.filter(draft => draft?.selected && Array.isArray(draft.spec?.segments)).map(draft => draft.spec.segments.reduce((sum, segment) => sum + numeric(segment.duration), 0));
  const timing = store.scenario.mode === "scenario" ? seconds.length ? `脚本时长 ${Math.min(...seconds) === Math.max(...seconds) ? seconds[0] : `${Math.min(...seconds)}–${Math.max(...seconds)}`} 秒 / 条` : `目标 ${$("#scenario-duration").value || "—"} 秒 / 条` : "分段时长按以下脚本";
  setText("#generation-summary", `${currentModel()?.label || "模型待选择"} · ${$("#batch-resolution").value || "分辨率待选择"} · ${store.scenario.mode === "scenario" ? `${selected} 条已选脚本` : `${$("#batch-quantity").value || 0} 条视频`} · ${timing} · 并发 ${$("#batch-concurrency").value || 1}${store.voiceAsset ? " · 使用声音参考" : ""}`);
  if (store.scenario.mode === "legacy") renderLegacyReview();
}
function renderLegacyReview() {
  const panel = $("#legacy-review"); if (!panel) return; panel.replaceChildren();
  try {
    const spec = customSpec() || template()?.spec; if (!spec) throw new Error("请先选择模板或导入 JSON。");
    const seconds = validateModelSpec(spec, currentModel());
    panel.append(node("h3", null, spec.name || "场景脚本"), node("p", "form-note", `${spec.segments.length} 个分段 · 总时长 ${seconds} 秒`));
    if (spec.global_prompt) panel.append(node("p", "legacy-global-prompt", spec.global_prompt));
    spec.segments.forEach((segment, index) => { const section = node("details", "draft-details"), content = node("div", "draft-detail-content"); section.append(node("summary", null, `${index + 1}. ${segment.title || segment.id || "分段"} · ${segment.duration} 秒`)); content.append(node("p", null, segment.prompt || "")); if (segment.expected_start_state) content.append(node("p", null, `开始：${segment.expected_start_state}`)); if (segment.expected_end_state) content.append(node("p", null, `结束：${segment.expected_end_state}`)); section.append(content); panel.append(section); });
  } catch (error) { panel.append(node("p", "form-error", error.message)); }
}
function renderVoiceStatus() {
  if (!$("#voice-status")) return;
  setText("#voice-status", store.voiceAsset ? `${store.voiceAsset.name || "声音参考"} · 已保存素材 ID，提交时由服务校验` : "未上传，不使用声音参考。");
  $("#voice-status").classList.remove("invalid"); $("#remove-voice").hidden = !store.voiceAsset;
  if ($("#voice-file")) { $("#voice-file").value = ""; $("#voice-file").disabled = false; }
}
function reuseGenerationMetadata(meta) {
  if (store.submitting) return;
  if (!meta?.spec || !Array.isArray(meta.spec.segments)) { toast("该成品未提供可复用脚本，请选择其他成品。"); return; }
  if (!generationWorkspace.initialized) initializeGeneration();
  newGenerationDraft();
  $("#batch-name").value = `${meta.spec.name || meta.job?.title || "成品"} · 复用`;
  $("#custom-spec").value = JSON.stringify(draftSafeCopy(meta.spec), null, 2);
  retainSelectValue($("#batch-model-family"), meta.model_family || meta.job?.model_family || store.defaults.model_family);
  syncModelControls();
  retainSelectValue($("#batch-resolution"), meta.job?.resolution || meta.resolution || currentModel()?.default_resolution || "");
  const segmentDuration = meta.job?.segment_duration_seconds || meta.segment_duration_seconds || meta.spec.segments[0]?.duration || currentModel()?.default_segment_duration;
  retainSelectValue($("#batch-segment-duration"), segmentDuration, `${segmentDuration} 秒`);
  $("#batch-profile").value = typeof meta.profile === "string" ? meta.profile : "";
  if (typeof meta.voice_asset_id === "string" && meta.voice_asset_id) { store.voiceAsset = {asset_id: meta.voice_asset_id, name: meta.voice_asset_name || "复用声音参考"}; generationWorkspace.reuseNote = "已保留脚本、参考图绑定与生成参数；提交前请检查。"; }
  else { store.voiceAsset = null; generationWorkspace.reuseNote = "已保留脚本、参考图绑定与生成参数。此成品未提供声音素材 ID；如需声音参考，请重新选择并上传。"; }
  renderVoiceStatus(); setCreationMode("legacy"); updateScriptStatus();
  let step = 1;
  try { creationPayload(); step = 2; } catch (error) { formError("#batch-error", error.message); }
  setGenerationStep(step); persistGenerationDraft(); navigatePage("create");
}
function setupGeneration() {
  if (generationWorkspace.bound) return; generationWorkspace.bound = true;
  setupVoiceUpload();
  $("#batch-model-family").addEventListener("change", modelChanged);
  ["scenario", "legacy"].forEach(mode => {
    const button = $(`#${mode}-mode-tab`);
    button.addEventListener("click", () => { setCreationMode(mode); setGenerationStep(1); persistGenerationDraft(); });
    button.addEventListener("keydown", event => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); const next = event.key === "Home" ? "scenario" : event.key === "End" ? "legacy" : mode === "scenario" ? "legacy" : "scenario"; setCreationMode(next); setGenerationStep(1); persistGenerationDraft(); $(`#${next}-mode-tab`).focus(); } });
  });
  $("#expand-scenario").addEventListener("click", expandScenario);
  $$("[data-scenario-example]").forEach(button => button.addEventListener("click", () => { const example = SCENARIO_EXAMPLES[button.dataset.scenarioExample]; if (!example) return; $("#scenario-description").value = example.description; $("#scenario-task-type").value = example.type; $("#scenario-diversity").value = example.diversity; invalidateEstimate(); scenarioChanged(); persistGenerationDraft(); $("#scenario-description").focus(); }));
  [["#select-all-drafts", true], ["#clear-drafts-selection", false]].forEach(([selector, selected]) => $(selector).addEventListener("click", () => { store.scenario.drafts.forEach(draft => { if (draft) draft.selected = selected; }); renderScenarioDrafts(); invalidateEstimate(); persistGenerationDraft(); }));
  $("#template-select").addEventListener("change", () => { $("#custom-spec").value = ""; updateTemplate(); persistGenerationDraft(); });
  const changed = event => {
    if (generationWorkspace.submitted) return;
    if (event.target.id !== "voice-file" && event.target.id !== "import-spec") invalidateEstimate();
    if (event.target.id === "custom-spec") updateScriptStatus();
    if (SCENARIO_INPUTS.has(event.target.id)) scenarioChanged();
    updateScenarioState(); renderGenerationSummary(); persistGenerationDraft();
  };
  $("#batch-form").addEventListener("input", changed); $("#batch-form").addEventListener("change", changed);
  $("#edit-template").addEventListener("click", () => { const selected = template(); if (!selected?.spec) { formError("#batch-error", "当前模板未提供完整脚本，可从本地导入场景 JSON。"); return; } $("#custom-spec").value = JSON.stringify(selected.spec, null, 2); updateScriptStatus(); invalidateEstimate(); persistGenerationDraft(); });
  $("#import-spec").addEventListener("change", async event => {
    const file = event.target.files[0]; if (!file) return; const session = store.batchSession, revision = store.formRevision;
    try { if (file.size > 2 * 1024 * 1024) throw new Error("场景文件不能超过 2 MB。"); const raw = await file.text(); if (session !== store.batchSession || revision !== store.formRevision || store.scenario.mode !== "legacy") return; const parsed = JSON.parse(raw); specDuration(parsed); $("#custom-spec").value = JSON.stringify(parsed, null, 2); updateScriptStatus(); invalidateEstimate(); updateScenarioState(); persistGenerationDraft(); formError("#batch-error", ""); }
    catch (error) { if (session === store.batchSession) formError("#batch-error", `导入失败：${error.message}`); }
    finally { if (session === store.batchSession) event.target.value = ""; }
  });
  const review = () => {
    formError("#batch-error", "");
    try {
      if (store.scenario.mode === "legacy") { const spec = customSpec() || template()?.spec; if (!spec) throw new Error("请先选择模板或导入 JSON。"); validateModelSpec(spec, currentModel()); }
      else if (!store.scenario.drafts.length) throw new Error("请先扩展场景，再检查脚本。");
      setGenerationStep(2); persistGenerationDraft();
    } catch (error) { formError("#batch-error", error.message); }
  };
  $("#generation-next").addEventListener("click", review); $("#generation-back").addEventListener("click", () => { setGenerationStep(1); persistGenerationDraft(); });
  $$("[data-generation-step]").forEach(button => button.addEventListener("click", () => { if (Number(button.dataset.generationStep) === 2) review(); else { setGenerationStep(1); persistGenerationDraft(); } }));
  $("#generation-new").addEventListener("click", newGenerationDraft);
  $("#generation-save").addEventListener("click", () => { const saved = persistGenerationDraft(); toast(saved ? "草稿已保存到此浏览器。" : "草稿已保留在当前会话，浏览器存储暂不可用。"); });
  $("#generation-draft-select").addEventListener("change", event => switchGenerationDraft(event.target.value));
  $("#estimate-button").addEventListener("click", estimateBudget); $("#batch-form").addEventListener("submit", createBatch);
  window.addEventListener("pagehide", persistGenerationDraft);
}
