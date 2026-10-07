/* Read-only finished assets. Generation controls remain on the board. */
"use strict";

function libraryAssets() { return store.state.jobs.filter(job => job.status === "completed" && validMediaURL(job.preview_url)); }
function libraryModel(job) { return job.model_family || store.state.batches.find(batch => batch.id === job.batch_id)?.model_family || ""; }
function libraryResolution(job) { return job.resolution || store.state.batches.find(batch => batch.id === job.batch_id)?.resolution || ""; }
function libraryModelLabel(job) { const id = libraryModel(job); return store.bootstrap?.models?.find(model => model.id === id)?.label || id || "模型未记录"; }
function libraryTagValues(value) {
  if (Array.isArray(value)) return value.flatMap(libraryTagValues);
  if (typeof value === "string" && value.trim()) return [value.trim()];
  return [];
}
function libraryTags(job, metadata = null) {
  const dataset = metadata?.spec?.dataset || metadata?.dataset || job.dataset || {};
  return [...new Set([job.task_label, job.task_type, job.objects, job.action_labels, job.dataset_label, job.tags, dataset.task_label, dataset.task_type, dataset.label, dataset.objects, dataset.action_labels, dataset.tags].flatMap(libraryTagValues))];
}
function filteredLibraryAssets() {
  const filters = store.library, search = filters.search.trim().toLowerCase();
  return libraryAssets().filter(job => {
    if (filters.model !== "all" && libraryModel(job) !== filters.model) return false;
    if (filters.resolution !== "all" && libraryResolution(job) !== filters.resolution) return false;
    if (filters.batchId !== "all" && (job.batch_id || "__history__") !== filters.batchId) return false;
    return !search || `${job.title || ""} ${job.id || ""} ${batchLabel(job.batch_id)} ${job.source_scenario || ""} ${libraryTags(job).join(" ")}`.toLowerCase().includes(search);
  });
}
function librarySelectOptions(selector, values, current, allLabel) {
  const select = $(selector), signature = JSON.stringify(values);
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature; select.replaceChildren();
    const all = node("option", null, allLabel); all.value = "all"; select.append(all);
    values.forEach(([value, label]) => { const option = node("option", null, label); option.value = value; select.append(option); });
  }
  select.value = current;
}
function renderLibraryFilters() {
  const assets = libraryAssets();
  const models = [...new Set(assets.map(libraryModel).filter(Boolean))].map(id => [id, store.bootstrap?.models?.find(model => model.id === id)?.label || id]);
  const resolutions = [...new Set(assets.map(libraryResolution).filter(Boolean))].sort().map(value => [value, value]);
  const batches = [...new Set(assets.map(job => job.batch_id || "__history__"))].map(id => [id, id === "__history__" ? "历史视频" : batchLabel(id)]);
  librarySelectOptions("#library-model", models, store.library.model, "全部模型");
  librarySelectOptions("#library-resolution", resolutions, store.library.resolution, "全部分辨率");
  librarySelectOptions("#library-batch", batches, store.library.batchId, "全部批次");
  $("#library-search").value = store.library.search;
}
function clearLibrarySelection() {
  store.library.selectedId = null; store.library.metadata = null; store.library.metadataId = null; store.library.error = ""; store.library.request++;
  $("#library-detail").hidden = true;
  const video = $("#library-video"); video.pause(); video.removeAttribute("src"); video.removeAttribute("poster"); video.dataset.jobId = "";
}
function renderLibrary() {
  renderLibraryFilters();
  const assets = filteredLibraryAssets(), selected = assets.find(job => job.id === store.library.selectedId);
  if (store.library.selectedId && !selected) { clearLibrarySelection(); saveWorkspaceState(); }
  setText("#library-count", `${assets.length} 条成品`);
  const grid = $("#library-grid"), signature = JSON.stringify([assets, store.library.selectedId, store.state.batches.map(batch => [batch.id, batch.name, batch.resolution, batch.model_family])]);
  if (grid.dataset.signature !== signature) {
    grid.dataset.signature = signature; grid.replaceChildren();
    assets.forEach(job => {
      const card = node("button", `library-card${job.id === store.library.selectedId ? " selected" : ""}`); card.type = "button"; card.setAttribute("aria-pressed", String(job.id === store.library.selectedId));
      const media = node("div", "library-card-media"), placeholder = node("span", "library-poster-placeholder"); placeholder.append(icon("film"), node("span", null, "成片预览"));
      const poster = validMediaURL(job.poster_url);
      if (poster) { const image = node("img"); image.src = poster; image.alt = `${job.title || "视频"}的成片缩略图`; image.loading = "lazy"; image.addEventListener("error", () => { media.replaceChildren(placeholder); }); media.append(image); }
      else media.append(placeholder);
      const body = node("div", "library-card-body"), tags = node("div", "library-card-tags");
      libraryTags(job).slice(0, 4).forEach(tag => tags.append(node("span", "library-tag", tag)));
      body.append(node("strong", "library-card-title", job.title || job.id), node("p", "library-card-facts", `${duration(job.duration_seconds)} · ${libraryResolution(job) || "分辨率未记录"} · ${libraryModelLabel(job)}`), node("p", "library-card-batch", `${batchLabel(job.batch_id)} · ${shortDate(job.updated_at || job.created_at)}`), tags);
      card.append(media, body); card.addEventListener("click", () => selectLibraryAsset(job.id)); grid.append(card);
    });
  }
  const empty = $("#library-empty"); empty.hidden = !!assets.length;
  if (!assets.length) empty.replaceChildren(icon("film"), node("strong", null, libraryAssets().length ? "没有匹配的成品" : "还没有可用的完整成片"), node("p", null, libraryAssets().length ? "调整关键词、模型、分辨率或批次筛选后重试。" : "视频完成生成、质检和导出后，会自动出现在这里。"));
  if (!selected) $("#library-detail").hidden = true;
  else if (store.page === "library") renderLibraryDetail(selected);
}
function renderLibraryDetail(job) {
  $("#library-detail").hidden = false; setText("#library-title", job.title || job.id);
  const meta = store.library.metadataId === job.id ? store.library.metadata : null, facts = $("#library-facts"); facts.replaceChildren();
  [duration(job.duration_seconds), libraryResolution(job) || meta?.job?.resolution || "分辨率未记录", libraryModelLabel(job), batchLabel(job.batch_id), `完成于 ${shortDate(job.updated_at || job.created_at)}`].forEach(value => facts.append(node("span", "library-fact", value)));
  const tags = $("#library-tags"); tags.replaceChildren(); libraryTags(job, meta).forEach(tag => tags.append(node("span", "library-tag", tag))); tags.hidden = !tags.childNodes.length;
  const video = $("#library-video"), src = validMediaURL(job.preview_url);
  if (video.dataset.jobId !== job.id || video.dataset.mediaURL !== src) {
    video.pause(); video.removeAttribute("src"); video.removeAttribute("poster"); if (src) video.src = src;
    const poster = validMediaURL(job.poster_url); if (poster) video.poster = poster;
    video.controls = true; video.preload = "metadata"; video.load(); video.dataset.jobId = job.id; video.dataset.mediaURL = src;
  }
  $("#library-download-video").href = jobURL(job.id, "download"); $("#library-download-metadata").href = jobURL(job.id, "metadata");
  $("#library-reuse").disabled = !!store.library.reusing;
  formError("#library-error", store.library.error || "");
}
async function loadLibraryMetadata(id) {
  if (store.library.metadataId === id && store.library.metadata) return store.library.metadata;
  if (store.library.loadingId === id && store.library.loadingToken === store.library.request && store.library.metadataPromise) return store.library.metadataPromise;
  const request = ++store.library.request;
  store.library.loadingId = id; store.library.loadingToken = request; store.library.error = "";
  const promise = (async () => {
    try {
      const metadata = await api(jobURL(id, "metadata"));
      if (store.library.request !== request || store.library.selectedId !== id || store.page !== "library") return null;
      store.library.metadata = metadata; store.library.metadataId = id;
      const job = libraryAssets().find(item => item.id === id); if (job) renderLibraryDetail(job);
      return metadata;
    } catch (error) {
      if (store.library.request === request && store.library.selectedId === id && store.page === "library") { store.library.error = error.message; formError("#library-error", error.message); }
      return null;
    } finally {
      if (store.library.loadingToken === request) { store.library.loadingId = null; store.library.metadataPromise = null; }
    }
  })();
  store.library.metadataPromise = promise; return promise;
}
async function selectLibraryAsset(id) {
  const job = filteredLibraryAssets().find(item => item.id === id); if (!job) return;
  if (store.library.selectedId !== id) clearLibrarySelection();
  store.library.selectedId = id; renderLibrary(); saveWorkspaceState();
  await loadLibraryMetadata(id);
}
async function reuseLibraryAsset() {
  const id = store.library.selectedId; if (!id || store.library.reusing || store.page !== "library") return;
  store.library.reusing = true; $("#library-reuse").disabled = true;
  try {
    const metadata = await loadLibraryMetadata(id);
    if (!metadata || store.library.selectedId !== id || store.page !== "library") return;
    if (!metadata.spec || typeof metadata.spec !== "object" || Array.isArray(metadata.spec)) throw new Error("此记录未包含可复用的场景脚本。");
    await reuseGenerationMetadata(metadata);
  } catch (error) {
    if (store.library.selectedId === id && store.page === "library") { store.library.error = error.message; formError("#library-error", error.message); }
  } finally { store.library.reusing = false; $("#library-reuse").disabled = false; }
}
function setupLibrary() {
  $("#library-search").addEventListener("input", event => { store.library.search = event.target.value; renderLibrary(); saveWorkspaceState(); });
  [["#library-model", "model"], ["#library-resolution", "resolution"], ["#library-batch", "batchId"]].forEach(([selector, key]) => $(selector).addEventListener("change", event => { store.library[key] = event.target.value; renderLibrary(); saveWorkspaceState(); }));
  $("#library-close-detail").addEventListener("click", () => { clearLibrarySelection(); renderLibrary(); saveWorkspaceState(); });
  $("#library-reuse").addEventListener("click", reuseLibraryAsset);
  $("#library-trace").addEventListener("click", () => { const job = libraryAssets().find(item => item.id === store.library.selectedId); if (job) navigatePage("board", {batch: job.batch_id || "__history__", job: job.id}); });
  $("#library-video").addEventListener("error", () => { if (!store.library.selectedId || store.page !== "library") return; store.library.error = "视频暂时无法播放，请刷新成品或下载后查看。"; formError("#library-error", store.library.error); });
}
