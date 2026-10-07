/* Page routing and shared connection lifecycle. No production action on navigation. */
"use strict";

const WORKSPACE_PAGES = ['create', 'board', 'library'];
const WORKSPACE_FILTERS = ['all', 'running', 'attention', 'completed'];
const WORKSPACE_TEXT = {
  create: ['生成中心', '先定义任务与视频参数，再集中检查脚本。'],
  board: ['流程看板', '按批次跟进生成、自动质检与异常处理。'],
  library: ['成品库', '浏览完整视频，下载结果，或复用脚本继续创作。']
};

function safeWorkspaceText(value, fallback = '', limit = 500) {
  return typeof value === 'string' ? value.replace(/[\u0000-\u001f]/g, '').slice(0, limit) : fallback;
}
function parseWorkspaceRoute(hash = '') {
  const raw = String(hash).replace(/^#\/?/, ''), separator = raw.indexOf('?');
  const name = separator < 0 ? raw : raw.slice(0, separator);
  if (!WORKSPACE_PAGES.includes(name)) return {page: 'board', params: {}};
  const query = new URLSearchParams(separator < 0 ? '' : raw.slice(separator + 1));
  const params = {};
  const keys = name === 'board' ? ['batch', 'job', 'scope', 'filter', 'q'] : name === 'library' ? ['job', 'scope', 'model', 'resolution', 'q'] : [];
  keys.forEach(key => { if (query.has(key)) params[key] = safeWorkspaceText(query.get(key)); });
  return {page: name, params};
}
function workspaceStorageKey() { return `seedance-workspaces-v1:${store.bootstrap?.simulation ? 'simulation' : 'real'}`; }
function workspaceSnapshot() {
  return {version: 1, page: store.page,
    board: {filter: store.filter, search: store.search, batchId: store.batchId, focusId: store.boardFocusId, selectedId: store.selectedId},
    library: {search: store.library.search, model: store.library.model, resolution: store.library.resolution, batchId: store.library.batchId, selectedId: store.library.selectedId}};
}
function currentWorkspaceHash() {
  const query = new URLSearchParams();
  function add(key, value, defaultValue = '') { if (value != null && value !== defaultValue && value !== '') query.set(key, String(value)); }
  if (store.page === 'board') {
    add('batch', store.boardFocusId); add('job', store.selectedId);
    add('scope', store.batchId, 'all'); add('filter', store.filter, 'all'); add('q', store.search);
  } else if (store.page === 'library') {
    add('job', store.library.selectedId); add('scope', store.library.batchId, 'all');
    add('model', store.library.model, 'all'); add('resolution', store.library.resolution, 'all'); add('q', store.library.search);
  }
  return `#/${store.page}${query.toString() ? '?' + query : ''}`;
}
function saveWorkspaceState() {
  try { localStorage.setItem(workspaceStorageKey(), JSON.stringify(workspaceSnapshot())); } catch (_) { /* View preferences are optional; drafts report their own storage state. */ }
  if (store.routingReady && typeof window !== 'undefined' && window.history) {
    const hash = currentWorkspaceHash();
    window.history.replaceState(null, '', hash); store.lastRouteHash = hash;
  }
}
function restoreWorkspaceState() {
  let data;
  try { data = JSON.parse(localStorage.getItem(workspaceStorageKey()) || 'null'); } catch (_) { return; }
  if (!data || data.version !== 1 || typeof data !== 'object') return;
  const board = data.board || {}, library = data.library || {};
  store.filter = WORKSPACE_FILTERS.includes(board.filter) ? board.filter : 'all';
  store.search = safeWorkspaceText(board.search); store.batchId = safeWorkspaceText(board.batchId, 'all', 200) || 'all';
  store.boardFocusId = safeWorkspaceText(board.focusId, '', 200) || null;
  store.selectedId = safeWorkspaceText(board.selectedId, '', 200) || null;
  store.library.search = safeWorkspaceText(library.search);
  for (const key of ['model', 'resolution', 'batchId']) store.library[key] = safeWorkspaceText(library[key], 'all', 200) || 'all';
  store.library.selectedId = safeWorkspaceText(library.selectedId, '', 200) || null;
  if (!window.location.hash && WORKSPACE_PAGES.includes(data.page)) store.page = data.page;
}

function pauseWorkspaceMedia(page) {
  const video = $(page === 'library' ? '#library-video' : '#preview-video');
  if (video && typeof video.pause === 'function') video.pause();
}
function showWorkspacePage(page) {
  for (const name of WORKSPACE_PAGES) {
    const panel = $(`#page-${name}`); if (panel) panel.hidden = name !== page;
    const nav = $(`#nav-${name}`); if (!nav) continue;
    nav.classList.toggle('active', name === page);
    if (name === page) nav.setAttribute('aria-current', 'page'); else nav.removeAttribute('aria-current');
  }
  setText('#workspace-title', WORKSPACE_TEXT[page][0]);
  setText('#workspace-description', WORKSPACE_TEXT[page][1]);
  if ($('#new-batch')) $('#new-batch').hidden = page === 'create';
  const actions = $('.header-actions'); if (actions) actions.hidden = page === 'create';
  if (page === 'board') renderBoard();
  else if (page === 'library') renderLibrary();
  else if (store.generationInitialized) setGenerationStep(store.generationStep || 1);
}
function applyWorkspaceRoute(route, fromHistory = false) {
  const page = WORKSPACE_PAGES.includes(route.page) ? route.page : 'board';
  const params = route.params || {}, oldPage = store.page, oldJobId = store.selectedId;
  if (oldPage === 'create' && page !== 'create' && typeof persistGenerationDraft === 'function') persistGenerationDraft();
  if (oldPage !== page) {
    pauseWorkspaceMedia(oldPage);
    if (oldPage === 'library') store.library.request++;
    if (oldPage === 'board') store.detailRequest++;
  }
  store.page = page;
  if (page === 'board') {
    if (fromHistory) {
      store.filter = WORKSPACE_FILTERS.includes(params.filter) ? params.filter : 'all';
      store.search = params.q || ''; store.batchId = params.scope || 'all';
      store.selectedId = params.job || null;
      store.boardFocusId = params.batch || store.state.jobs.find(job => job.id === store.selectedId)?.batch_id || null;
    } else if (params.batch || params.job) {
      const target = store.state.jobs.find(job => job.id === params.job);
      store.boardFocusId = params.batch || target?.batch_id || null;
      store.batchId = store.boardFocusId || 'all'; store.filter = 'all'; store.search = '';
      store.selectedId = params.job || null;
    }
    if (store.detail && store.detail.job?.id !== store.selectedId) { store.detail = null; store.detailSignature = ''; }
    if (oldJobId !== store.selectedId) {
      pauseWorkspaceMedia('board'); store.detailRequest++;
      store.previewSegment = null; store.previewURL = ''; store.detail = null;
      store.detailSignature = ''; store.segmentsSignature = ''; store.logsSignature = '';
    }
  } else if (page === 'library') {
    if (fromHistory) {
      store.library.search = params.q || ''; store.library.model = params.model || 'all';
      store.library.resolution = params.resolution || 'all'; store.library.batchId = params.scope || 'all';
      store.library.selectedId = params.job || null;
    } else if (params.job) {
      store.library.search = ''; store.library.model = 'all'; store.library.resolution = 'all'; store.library.batchId = 'all';
      store.library.selectedId = params.job; store.library.metadata = null;
    }
  }
  showWorkspacePage(page);
  if (store.connected && page === 'board' && store.selectedId) loadDetail(store.selectedId, true);
  if (store.connected && page === 'library' && store.library.selectedId) selectLibraryAsset(store.library.selectedId);
  saveWorkspaceState();
}
function navigatePage(page, params = {}) {
  store.navigationEpoch = (store.navigationEpoch || 0) + 1;
  if (!WORKSPACE_PAGES.includes(page)) page = 'board';
  const oldHash = currentWorkspaceHash();
  // Preserve the old route before pushing the new one, so Back restores its filters.
  if (store.routingReady && window.history) window.history.replaceState(null, '', oldHash);
  const wasReady = store.routingReady; store.routingReady = false;
  applyWorkspaceRoute({page, params});
  store.routingReady = wasReady;
  const hash = currentWorkspaceHash();
  if (wasReady && window.history && hash !== oldHash) window.history.pushState(null, '', hash);
  store.lastRouteHash = hash; saveWorkspaceState();
}
function setupRouting() {
  store.routingReady = true;
  const sync = () => {
    const hash = window.location.hash;
    if (hash === store.lastRouteHash) return;
    store.navigationEpoch = (store.navigationEpoch || 0) + 1;
    store.lastRouteHash = hash;
    applyWorkspaceRoute(parseWorkspaceRoute(hash), true);
  };
  window.addEventListener('hashchange', sync); window.addEventListener('popstate', sync);
  $$('[data-page]').forEach(button => button.addEventListener('click', () => navigatePage(button.dataset.page)));
  const first = parseWorkspaceRoute(window.location.hash);
  store.page = first.page; store.initialRoute = window.location.hash ? first : null;
  showWorkspacePage(first.page);
}

function connection(ok, error) {
  store.connected = ok;
  $('#connection-alert').hidden = ok;
  $('#connection-dot').className = `status-dot ${ok ? 'green' : 'red'}`;
  setText('#connection-text', ok ? '本地服务已连接' : '连接已中断');
  setText('#last-updated', ok ? `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}` : '保留已读取内容');
  if (!ok) setText('#connection-message', error || '请启动本地服务后重连。仍可编辑本机草稿。');
  if ($('#new-batch')) $('#new-batch').disabled = false;
  if (store.generationInitialized && typeof updateScenarioState === 'function') updateScenarioState();
}
async function bootstrap() {
  if (store.bootstrapping) return;
  store.bootstrapping = true;
  try {
    const data = await api('/api/bootstrap');
    data.templates = Array.isArray(data.templates) ? data.templates : [];
    data.reference_images = Array.isArray(data.reference_images) ? data.reference_images : [];
    data.models = (Array.isArray(data.models) ? data.models : []).filter(model => model && typeof model.id === 'string' && Array.isArray(model.resolutions) && model.resolutions.includes(model.default_resolution) && Array.isArray(model.durations) && model.durations.length && model.durations.every(value => Number.isInteger(value) && value > 0) && model.durations.includes(model.default_segment_duration));
    store.bootstrap = data;
    if (!store.workspaceRestored) { restoreWorkspaceState(); store.workspaceRestored = true; }
    initializeGeneration(); store.generationInitialized = true;
    $('#simulation-notice').hidden = !data.simulation;
    connection(true);
    await refreshState(true);
    if (store.initialRoute) { const initial = store.initialRoute; store.initialRoute = null; applyWorkspaceRoute(initial, true); }
    else showWorkspacePage(store.page);
    saveWorkspaceState();
  } catch (error) { connection(false, error.message); }
  finally { store.bootstrapping = false; }
}
async function refreshState(force = false) {
  if (store.polling) {
    const pending = store.refreshPromise;
    if (force && pending) { await pending; return refreshState(false); }
    return pending;
  }
  if (!store.bootstrap) { if (force) await bootstrap(); return; }
  store.polling = true; $('#refresh').classList.add('refreshing');
  const request = (async () => { try {
    const data = await api('/api/state');
    store.state = {batches: Array.isArray(data.batches) ? data.batches : [], jobs: Array.isArray(data.jobs) ? data.jobs : [], summary: data.summary || {}};
    store.lastPoll = Date.now(); connection(true);
    if (store.selectedId && !store.state.jobs.some(job => job.id === store.selectedId)) {
      store.selectedId = null; store.detail = null; store.detailSignature = ''; store.detailRequest++;
    }
    if (store.page === 'board') {
      renderBoard();
      const selected = store.state.jobs.find(job => job.id === store.selectedId);
      if (selected && (force || JSON.stringify(selected) !== store.detailSignature || ACTIVE.has(selected.status))) await loadDetail(selected.id, force);
    } else if (store.page === 'library') renderLibrary();
  } catch (error) { connection(false, error.message); }
  finally { store.polling = false; store.refreshPromise = null; $('#refresh').classList.remove('refreshing'); }
  })();
  store.refreshPromise = request;
  return request;
}
async function onCreationSubmitted(result, payload, origin = {page: 'create', epoch: store.navigationEpoch || 0}) {
  await refreshState(true);
  const first = result.jobs?.[0];
  // The create response is authoritative even if the following read was offline.
  for (const job of result.jobs || []) if (!store.state.jobs.some(item => item.id === job.id)) store.state.jobs.unshift(job);
  if (result.batch && !store.state.batches.some(item => item.id === result.batch.id)) store.state.batches.unshift(result.batch);
  const focus = () => navigatePage('board', {batch: result.batch?.id || first?.batch_id, job: first?.id});
  const message = `批次已创建，${result.jobs?.length || payload.quantity} 条视频已进入自动生产队列。`;
  if (store.page === origin.page && origin.page === 'create' && (store.navigationEpoch || 0) === origin.epoch) {
    focus(); toast(message);
  } else {
    if (store.page === 'board') renderBoard(); else if (store.page === 'library') renderLibrary();
    toast(message, '查看批次', focus);
  }
}
function initializeStudio() {
  if (store.initialized) return;
  store.initialized = true;
  setupGeneration(); setupBoard(); setupLibrary(); setupRouting();
  $('#new-batch').addEventListener('click', () => openBatch());
  $('#refresh').addEventListener('click', () => refreshState(true));
  $('#reconnect').addEventListener('click', () => bootstrap());
  $('#open-settings').addEventListener('click', openSettings);
  $('#settings-form').addEventListener('submit', saveSettings);
  $('#default-model-family').addEventListener('change', syncDefaultModel);
  $$('[data-close]').forEach(button => button.addEventListener('click', () => closeDialog(button.dataset.close)));
  const settings = $('#settings-dialog');
  if (settings) settings.addEventListener('close', () => { document.body.style.overflow = ''; if (dialogOpener?.isConnected) dialogOpener.focus(); });
  const skip = $('.skip-link');
  if (skip) skip.addEventListener('click', event => { event.preventDefault(); const main = $('#workspace-main'); main.tabIndex = -1; main.focus(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshState(true); });
  window.addEventListener('online', () => bootstrap());
  window.addEventListener('pagehide', () => { persistGenerationDraft(); saveWorkspaceState(); });
  bootstrap();
  setInterval(() => { if (!document.hidden) refreshState(); }, 3000);
}
