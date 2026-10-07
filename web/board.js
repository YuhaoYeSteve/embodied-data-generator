/* Batch-focused production board. State and requests are shared through app.js. */
"use strict";

const BOARD_ATTENTION = new Set(["paused", "failed", "budget_blocked", "submission_unknown", "awaiting_review"]);
function boardBatchId(job) { return job.batch_id || "__history__"; }
function boardBatchName(id) { return store.state.batches.find(batch => batch.id === id)?.name || "历史视频"; }
function filteredJobs() {
  const search = store.search.trim().toLowerCase();
  return store.state.jobs.filter(job => {
    if (store.batchId !== "all" && boardBatchId(job) !== store.batchId) return false;
    if (search && !`${job.title || ""} ${job.id || ""} ${boardBatchName(boardBatchId(job))}`.toLowerCase().includes(search)) return false;
    if (store.filter === "running") return ACTIVE.has(job.status);
    if (store.filter === "attention") return BOARD_ATTENTION.has(job.status);
    if (store.filter === "review") return job.status === "reviewing";
    if (store.filter === "completed") return job.status === "completed";
    return true;
  });
}
function boardGroups() {
  const visible = filteredJobs(), matching = new Set(visible.map(job => job.id));
  const groups = new Map(store.state.batches.map(batch => [batch.id, {id: batch.id, batch, allJobs: [], jobs: []}]));
  store.state.jobs.forEach(job => {
    const id = boardBatchId(job);
    if (!groups.has(id)) groups.set(id, {id, batch: {id, name: boardBatchName(id)}, allJobs: [], jobs: []});
    groups.get(id).allJobs.push(job);
    if (matching.has(job.id)) groups.get(id).jobs.push(job);
  });
  return Array.from(groups.values()).filter(group => group.jobs.length || (!group.allJobs.length && store.filter === "all" && (store.batchId === "all" || store.batchId === group.id) && (!store.search.trim() || String(group.batch.name || "").toLowerCase().includes(store.search.trim().toLowerCase())))).map(group => {
    const counts = {queued: 0, generating: 0, reviewing: 0, exporting: 0, completed: 0, attention: 0};
    group.allJobs.forEach(job => {
      if (Object.hasOwn(counts, job.status)) counts[job.status]++;
      else if (BOARD_ATTENTION.has(job.status)) counts.attention++;
    });
    return {...group, counts, total: group.allJobs.length};
  });
}
function renderMetrics() {
  const jobs = store.state.jobs, summary = store.state.summary || {};
  setText("#metric-total", jobs.length); setText("#metric-running", jobs.filter(job => ACTIVE.has(job.status)).length); setText("#metric-review", jobs.filter(job => job.status === "reviewing").length);
  const spent = "spent_cny" in summary ? summary.spent_cny : "total_spent_cny" in summary ? summary.total_spent_cny : jobs.reduce((sum, job) => sum + (moneyAmount(job.spent_cny) ?? 0), 0);
  const unverified = moneyAmount(spent) === null || ("reserved_cny" in summary && moneyAmount(summary.reserved_cny) === null) || jobs.some(hasUnverifiedCosts);
  const known = moneyAmount(summary.known_spent_cny) ?? jobs.reduce((sum, job) => sum + (moneyAmount(job.known_spent_cny) ?? moneyAmount(job.spent_cny) ?? 0), 0);
  setText("#metric-cost", unverified ? "待核实" : money(spent, false));
  $("#metric-cost").title = unverified ? `已知已用 ${money(known)}；部分费用待核实。` : "已记录的生成费用";
  if ($("#outputs-dot")) $("#outputs-dot").hidden = !jobs.some(job => job.status === "completed" && validMediaURL(job.preview_url));
}
function renderBatchFilter() {
  const select = $("#board-batch-filter"); if (!select) return;
  const batches = store.state.batches.map(batch => ({id: batch.id, name: batch.name}));
  if (store.state.jobs.some(job => !job.batch_id)) batches.push({id: "__history__", name: "历史视频"});
  const signature = JSON.stringify(batches);
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature; select.replaceChildren();
    const all = node("option", null, "全部批次"); all.value = "all"; select.append(all);
    batches.forEach(batch => { const option = node("option", null, batch.name || batch.id); option.value = batch.id; select.append(option); });
  }
  if (store.batchId !== "all" && !batches.some(batch => batch.id === store.batchId)) store.batchId = "all";
  select.value = store.batchId;
}
function clearBoardSelection() {
  store.selectedId = null; store.detail = null; store.detailRequest++; store.detailSignature = ""; store.segmentsSignature = ""; store.logsSignature = ""; store.reviewSignature = ""; store.previewSegment = null; store.previewURL = "";
  const video = $("#preview-video");
  if (video) { video.pause(); video.removeAttribute("src"); video.removeAttribute("poster"); video.hidden = true; }
  if ($("#board-detail")) $("#board-detail").hidden = true;
}
function renderBoard() {
  renderMetrics(); renderBatchFilter();
  $("#board-search").value = store.search; $("#board-filter").value = store.filter;
  const groups = boardGroups();
  const matchingJob = groups.flatMap(group => group.jobs).find(job => job.id === store.selectedId);
  if (store.selectedId && !matchingJob) { clearBoardSelection(); saveWorkspaceState(); }
  if (!groups.some(group => group.id === store.boardFocusId)) store.boardFocusId = matchingJob ? boardBatchId(matchingJob) : groups[0]?.id || null;
  if (matchingJob && boardBatchId(matchingJob) !== store.boardFocusId) { clearBoardSelection(); saveWorkspaceState(); }
  const list = $("#batch-list"), signature = JSON.stringify([groups, store.boardFocusId]);
  if (list.dataset.signature !== signature) {
    list.dataset.signature = signature; list.replaceChildren();
    groups.forEach(group => {
      const row = node("button", `batch-row${group.id === store.boardFocusId ? " selected" : ""}`); row.type = "button"; row.setAttribute("aria-pressed", String(group.id === store.boardFocusId));
      const main = node("div", "batch-row-main"); main.append(node("strong", null, group.batch.name || "历史视频"), node("span", null, `${group.total} 条视频 · ${shortDate(group.batch.created_at)}`));
      const stages = node("div", "batch-row-stages");
      [["queued", "排队"], ["generating", "生成"], ["reviewing", "质检"], ["exporting", "合成"], ["completed", "完成"], ["attention", "暂停 / 异常"]].forEach(([key, label]) => stages.append(node("span", `batch-stage ${key}${group.counts[key] ? " has-items" : ""}`, `${label} ${group.counts[key]}`)));
      const aside = node("div", "batch-row-aside"); aside.append(node("strong", null, `完成 ${group.counts.completed} / ${group.total}`), node("span", null, group.jobs.length === group.total ? "查看批次" : `匹配 ${group.jobs.length} 条`));
      row.append(main, stages, aside); row.addEventListener("click", () => selectBatch(group.id)); list.append(row);
    });
  }
  const empty = $("#board-empty"); empty.hidden = !!groups.length;
  if (!groups.length) {
    empty.replaceChildren(icon(store.state.jobs.length ? "search" : "grid"), node("strong", null, store.state.jobs.length ? "没有匹配的批次或任务" : "还没有生产批次"), node("p", null, store.state.jobs.length ? "调整关键词、状态或批次筛选后重试。" : "在视频生成页确认脚本并开始生成后，可在这里跟踪进度。"));
    const button = node("button", "button", store.state.jobs.length ? "清除筛选" : "前往生成中心"); button.type = "button";
    button.addEventListener("click", store.state.jobs.length ? resetFilters : () => navigatePage("create")); empty.append(button);
  }
  const focus = groups.find(group => group.id === store.boardFocusId);
  if ($("#batch-focus")) $("#batch-focus").hidden = !focus;
  if ($("#task-count")) setText("#task-count", focus?.jobs.length || 0);
  setText("#batch-focus-title", focus ? focus.batch.name || "历史视频" : "批次任务");
  setText("#batch-focus-meta", focus ? `${focus.jobs.length} 条${focus.jobs.length === focus.total ? "视频" : `匹配视频 / 共 ${focus.total} 条`} · 点击视频查看分段、质检和运行记录` : "选择批次后查看视频任务");
  const jobs = $("#batch-jobs"), jobsSignature = JSON.stringify([focus?.jobs || [], store.selectedId]);
  if (jobs.dataset.signature !== jobsSignature) {
    jobs.dataset.signature = jobsSignature; jobs.replaceChildren();
    (focus?.jobs || []).forEach(job => {
      const row = node("button", `batch-job-row${job.id === store.selectedId ? " selected" : ""}`); row.type = "button"; row.setAttribute("aria-pressed", String(job.id === store.selectedId));
      const main = node("div", "batch-job-main"); main.append(node("strong", null, job.title || job.id), node("span", null, `${duration(job.duration_seconds)} · ${job.resolution || focus.batch.resolution || "分辨率未记录"}${job.read_only ? " · 历史只读" : ""}`));
      const cost = node("span", "batch-job-cost", jobCostText(job)); cost.title = jobCostText(job, true);
      row.append(main, statusBadge(job.status), cost, icon("arrow")); row.addEventListener("click", () => selectJob(job.id)); jobs.append(row);
    });
  }
  if (!store.selectedId) showDetailEmpty();
  else if (store.page === "board" && store.detail) renderDetail();
}
function renderJobs() { renderBoard(); }
function selectBatch(id) {
  if (store.boardFocusId !== id && store.selectedId) clearBoardSelection();
  store.boardFocusId = id; renderBoard(); saveWorkspaceState();
}
function setFilter(filter) { store.filter = ["all", "running", "attention", "completed", "review"].includes(filter) ? filter : "all"; renderBoard(); saveWorkspaceState(); }
function resetFilters() { store.batchId = "all"; store.search = ""; setFilter("all"); }
function showDetailEmpty() {
  $("#board-detail").hidden = !store.selectedId; $("#detail-content").hidden = true; $("#detail-empty").hidden = false;
  $("#preview-video").pause();
}
async function selectJob(id) {
  const job = filteredJobs().find(item => item.id === id); if (!job) return;
  const changed = store.selectedId !== id;
  if (changed) clearBoardSelection();
  store.selectedId = id; store.boardFocusId = boardBatchId(job); renderBoard(); saveWorkspaceState();
  $("#board-detail").hidden = false;
  if (changed || !store.detail) {
    showDetailEmpty(); const heading = $("h2", $("#detail-empty")), description = $("p", $("#detail-empty"));
    if (heading) heading.textContent = "正在读取任务详情";
    if (description) description.textContent = job.title || job.id;
  }
  await loadDetail(id, changed);
}
async function loadDetail(id, force = false) {
  const request = ++store.detailRequest;
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (request !== store.detailRequest || store.selectedId !== id || store.page !== "board") return;
    store.detail = {job: result.job || result, segments: Array.isArray(result.segments) ? result.segments : [], logs: Array.isArray(result.logs) ? result.logs : []};
    store.detailSignature = JSON.stringify(store.state.jobs.find(job => job.id === id)); renderDetail(force);
  } catch (error) {
    if (request !== store.detailRequest || store.selectedId !== id || store.page !== "board") return;
    if (!store.detail) {
      showDetailEmpty(); const heading = $("h2", $("#detail-empty")), description = $("p", $("#detail-empty"));
      if (heading) heading.textContent = "暂时无法读取任务详情"; if (description) description.textContent = error.message;
    } else formError("#job-error", error.message);
  }
}
function renderDetail(force = false) {
  const job = currentJob(); if (!job) { showDetailEmpty(); return; }
  const batch = currentBatch();
  $("#board-detail").hidden = false; $("#detail-empty").hidden = true; $("#detail-content").hidden = false;
  setText("#detail-title", job.title || job.id); setText("#detail-batch", batch?.name || "历史视频记录"); $("#read-only-label").hidden = !job.read_only;
  $("#detail-status").replaceWith(Object.assign(statusBadge(job.status), {id: "detail-status"}));
  setText("#detail-duration", duration(job.duration_seconds)); setText("#detail-resolution", job.resolution || batch?.resolution || "—"); setText("#detail-cost", jobCostText(job, true));
  $("#download-metadata").href = jobURL(job.id, "metadata");
  const previewURL = validMediaURL(job.preview_url); $("#download-video").hidden = !previewURL;
  if (previewURL) $("#download-video").href = jobURL(job.id, "download"); else $("#download-video").removeAttribute("href");
  $("#board-result").hidden = job.status !== "completed" || !previewURL;
  formError("#job-error", job.error || ""); setText("#detail-progress", `${Math.round(numeric(job.progress))}%`); setText("#detail-completed-seconds", `${numeric(job.completed_seconds)} / ${numeric(job.duration_seconds)} 秒`); setText("#segment-count", job.segment_count || store.detail.segments.length);
  const phases = {completed: "全部分段已完成，成片与记录可下载。", paused: "已暂停后续提交，已提交任务仍继续查询。", awaiting_review: "历史待审状态；审核记录可在下方查看。", budget_blocked: "历史暂停状态，可恢复后继续生产。", submission_unknown: "提交结果尚未确认，为避免重复计费不会自动重发。", failed: "任务已停止，请查看自动质检结果和失败原因。", reviewing: "正在自动检查画面、动作和声音；不确定结果会自动复查。", exporting: "所有分段已通过，正在导出完整视频。", queued: "任务已入队，等待可用并发位置。", generating: "逐段生成中，已完成的片段可提前预览。"};
  setText("#detail-phase", phases[job.status] || STATUS[job.status] || "等待任务进度。"); renderPreview(); renderActions(); renderSegments(force); renderLogs();
}
function renderPreview() {
  const job = currentJob(); if (!job) return;
  const segment = store.previewSegment !== null ? store.detail.segments.find(item => numeric(item.index) === store.previewSegment) : null;
  const src = validMediaURL(segment ? segment.video_url : job.preview_url), video = $("#preview-video");
  if (store.previewURL !== src || video.dataset.jobId !== job.id) {
    video.pause(); video.removeAttribute("src"); video.removeAttribute("poster"); if (src) video.src = src;
    const poster = validMediaURL(segment ? segment.poster_url : job.poster_url); if (poster) video.poster = poster;
    video.load(); store.previewURL = src; video.dataset.jobId = job.id;
  }
  video.hidden = !src; $("#video-placeholder").hidden = !!src; $("#preview-label").hidden = !src;
  setText("#preview-label", segment ? `分段 ${numeric(segment.index) + 1} / ${duration(segment.duration)}` : `完整预览 / ${duration(job.duration_seconds)}`); $("#show-full-preview").hidden = store.previewSegment === null;
  if (!src) { setText("#preview-empty-title", segment ? "该分段尚无可用视频" : job.status === "failed" ? "本次生成未完成" : "视频正在路上"); setText("#preview-empty-description", segment ? "生成完成后，可在这里预览并查看自动质检结果。" : "已生成的分段可以在下方单独预览。自动质检通过后导出成片。"); }
}
function selectSegment(index) { store.previewSegment = index; renderPreview(); renderTimeline(); $("#video-frame").scrollIntoView({behavior: "smooth", block: "center"}); }
function renderTimeline() {
  const timeline = $("#segment-timeline"); timeline.replaceChildren(); const segments = store.detail.segments;
  if (!segments.length) { const fill = node("button"); fill.type = "button"; fill.disabled = true; fill.setAttribute("aria-label", "等待分段数据"); timeline.append(fill); return; }
  segments.forEach((segment, order) => {
    const index = numeric(segment.index, order), status = segmentStatus(segment);
    const button = node("button", `${String(status || "pending").replace(/[^a-z_]/g, "")}${store.previewSegment === index ? " selected" : ""}`); button.type = "button"; button.style.flexGrow = numeric(segment.duration, 1);
    const label = `第 ${index + 1} 段：${segmentTitle(segment, index)}，${STATUS[status] || status || "待生成"}`; button.title = label; button.setAttribute("aria-label", label); button.addEventListener("click", () => selectSegment(index)); timeline.append(button);
  });
}
function boardAllowedActions(job) {
  if (!job || job.read_only) return [];
  const actions = [];
  if (job.can_pause === true || (job.can_pause === undefined && ACTIVE.has(job.status))) actions.push(["pause", "暂停", "pause"]);
  if (job.can_resume) actions.push(["resume", "恢复生产", "play"]);
  if (job.can_retry) actions.push(["retry", "重试当前分段", "refresh"]);
  return actions;
}
function renderActions() {
  const actions = $("#job-actions"); actions.replaceChildren();
  boardAllowedActions(currentJob()).forEach(([action, label, iconName]) => { const button = node("button", "text-button"); button.type = "button"; button.append(icon(iconName), node("span", null, label)); button.disabled = store.actionBusy; button.addEventListener("click", () => jobAction(action)); actions.append(button); });
}
async function jobAction(action) {
  const job = currentJob(); if (!job || store.actionBusy || store.page !== "board" || !boardAllowedActions(job).some(item => item[0] === action)) return;
  store.actionBusy = true; renderActions(); formError("#job-error", "");
  try { await api(jobURL(job.id, action), {}); toast({pause: "已暂停后续提交，已提交任务继续查询。", resume: "已恢复生产。", retry: "已加入重试队列，新候选费用将计入任务记录。"}[action]); await refreshState(true); }
  catch (error) { if (store.selectedId === job.id && store.page === "board") formError("#job-error", error.message); else toast(error.message); }
  finally { store.actionBusy = false; renderActions(); }
}

function reviewSource(review) {
  const raw = `${review?.source || ""} ${review?.mode || ""} ${review?.review_mode || ""} ${review?.reviewer || ""}`.toLowerCase();
  if (review?.automatic || /auto|model|ai|自动/.test(raw)) return "自动质检";
  if (/human|manual|人工/.test(raw)) return "人工质检";
  return "质检记录";
}
function valueText(value) {
  if (value == null || value === "") return "未记录";
  if (Array.isArray(value)) return value.map(valueText).join("；");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
function renderSegments(force) {
  const signature = JSON.stringify(store.detail.segments);
  if (!force && store.segmentsSignature === signature) return;
  store.segmentsSignature = signature; renderTimeline();
  const list = $("#segments-list"); list.replaceChildren();
  if (!store.detail.segments.length) { list.append(node("p", "logs-empty", "分段信息将在任务初始化后显示。")); return; }
  store.detail.segments.forEach((segment, order) => {
    const index = numeric(segment.index, order), row = node("article", "segment-row");
    const number = node("div", "segment-number", String(index + 1).padStart(2, "0"));
    const info = node("div", "segment-info"); info.append(node("h3", null, segmentTitle(segment, index)));
    const metadata = node("p"); metadata.append(node("span", null, duration(segment.duration)), node("span", null, money(segment.cost_cny)));
    if (segment.task_id) { const id = node("span", null, `ID …${String(segment.task_id).slice(-8)}`); id.title = segment.task_id; metadata.append(id); }
    info.append(metadata); const actions = node("div", "segment-actions"); actions.append(statusBadge(segmentStatus(segment)));
    if (validMediaURL(segment.video_url)) { const preview = node("button", "text-button", "预览"); preview.type = "button"; preview.addEventListener("click", () => selectSegment(index)); actions.append(preview); }
    if (validMediaURL(segment.contact_url)) { const contact = node("a", "text-button", "关键帧"); contact.href = validMediaURL(segment.contact_url); contact.target = "_blank"; contact.rel = "noopener"; actions.append(contact); }
    row.append(number, info, actions);
    if (segment.review && typeof segment.review === "object") {
      const review = segment.review, summary = node("div", "segment-review-summary");
      const details = node("details");
      details.append(node("summary", null, `${reviewSource(review)} · ${segment.review_is_suggestion ? "检查记录" : review.decision === "accept" || review.accepted === true ? "已通过" : review.decision === "reject" || review.accepted === false ? "未通过" : "查看结果"}`));
      const content = node("div", "segment-review-detail");
      [["visual_continuity", "画面连续"], ["action_integrity", "动作完整"], ["audio_consistency", "声音一致"]].forEach(([key, label]) => { if (review[key]) content.append(node("p", null, `${label}：${PASS_TEXT[review[key]] || valueText(review[key])}`)); });
      if (review.observed_end_state) content.append(node("p", null, `实测末状态：${valueText(review.observed_end_state)}`));
      if (review.evidence) content.append(node("p", null, `检查依据：${valueText(review.evidence)}`));
      if (review.reason) content.append(node("p", null, valueText(review.reason)));
      if (Array.isArray(review.warnings) && review.warnings.length) content.append(node("p", null, `提示：${valueText(review.warnings)}`));
      if (!content.childNodes.length) content.append(node("p", null, "完整结果请下载任务记录查看。"));
      details.append(content); summary.append(details); row.append(summary);
    }
    list.append(row);
  });
}

function renderLogs() {
  const logs = store.detail.logs || [], signature = JSON.stringify(logs);
  if (store.logsSignature === signature) return; store.logsSignature = signature;
  const list = $("#logs-list"); list.replaceChildren();
  if (!logs.length) { list.append(node("p", "logs-empty", "尚无运行记录。完整元数据可通过上方「记录」下载。")); return; }
  logs.slice().reverse().forEach(log => {
    const row = node("div", "log-row");
    if (typeof log === "string") row.append(node("time", null, "—"), node("p", null, log));
    else row.append(node("time", null, shortDate(log.created_at || log.timestamp || log.time || log.at)), node("p", null, log.message || log.detail || log.event || valueText(log)));
    list.append(row);
  });
}

function switchTab(name) {
  ["segments", "logs"].forEach(tab => {
    const active = name === tab; $(`#${tab}-tab`).classList.toggle("active", active); $(`#${tab}-tab`).setAttribute("aria-selected", String(active)); $(`#${tab}-tab`).tabIndex = active ? 0 : -1; $(`#${tab}-pane`).hidden = !active;
  });
}

function setupBoard() {
  $("#board-search").addEventListener("input", event => { store.search = event.target.value; renderBoard(); saveWorkspaceState(); });
  $("#board-filter").addEventListener("change", event => setFilter(event.target.value));
  $("#board-batch-filter").addEventListener("change", event => { store.batchId = event.target.value; store.boardFocusId = store.batchId === "all" ? null : store.batchId; renderBoard(); saveWorkspaceState(); });
  $("#board-close-detail").addEventListener("click", () => { clearBoardSelection(); renderBoard(); saveWorkspaceState(); });
  $("#board-result").addEventListener("click", () => { const job = currentJob(); if (job?.status === "completed" && validMediaURL(job.preview_url)) navigatePage("library", {job: job.id}); });
  $("#show-full-preview").addEventListener("click", () => { store.previewSegment = null; renderPreview(); renderTimeline(); });
  ["segments", "logs"].forEach(name => $("#" + name + "-tab").addEventListener("click", () => switchTab(name)));
  $("#preview-video").addEventListener("error", () => { if (!store.previewURL) return; $("#preview-video").hidden = true; $("#video-placeholder").hidden = false; setText("#preview-empty-title", "视频暂时无法播放"); setText("#preview-empty-description", "媒体文件可能暂时不可用，请刷新任务后重试。"); });
}
