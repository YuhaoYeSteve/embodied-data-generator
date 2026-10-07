"use strict";
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=['app.js','navigation.js'].map(file=>fs.readFileSync(path.join(__dirname,'../web',file),'utf8')).join('\n');
function context(){
  assert.match(source,/function parseWorkspaceRoute\(/,'shared page router must exist');
  const elements=new Map();
  const element=()=>({hidden:false,disabled:false,textContent:'',dataset:{},classList:{add(){},remove(){},toggle(){}},setAttribute(){},removeAttribute(){},pause(){this.paused=true;},load(){},addEventListener(){},focus(){},scrollIntoView(){}});
  const document={hidden:false,activeElement:null,body:{style:{}},querySelector(s){if(!elements.has(s))elements.set(s,element());return elements.get(s);},querySelectorAll(){return[];},addEventListener(){},createElement:element};
  const window={location:{hash:'#/board',pathname:'/',search:''},addEventListener(){},history:{pushState(_,__,hash){window.location.hash=hash;},replaceState(_,__,hash){window.location.hash=hash;}}};
  const storage=new Map();
  const page=vm.createContext({document,window,location:window.location,history:window.history,URLSearchParams,URL,console,setTimeout,clearTimeout,localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)}});
  vm.runInContext(source,page);
  vm.runInContext('renderBoard=()=>{};renderLibrary=()=>{};renderMetrics=()=>{};loadDetail=async()=>{};selectLibraryAsset=async()=>{};persistGenerationDraft=()=>{store.savedForNavigation=(store.savedForNavigation||0)+1;};setGenerationStep=()=>{};',page);
  return page;
}
function value(page,code){return JSON.parse(JSON.stringify(vm.runInContext(code,page)));}
test('routes have separate pages and reject unknown destinations',()=>{
  const p=context();
  assert.deepEqual(value(p,"parseWorkspaceRoute('#/create')"),{page:'create',params:{}});
  assert.deepEqual(value(p,"parseWorkspaceRoute('#/library?job=a%2Fb&untrusted=yes')"),{page:'library',params:{job:'a/b'}});
  assert.equal(value(p,"parseWorkspaceRoute('#/unknown?job=x')").page,'board');
});
test('ordinary page navigation preserves independent filters and saves a departing draft',()=>{
  const p=context();
  vm.runInContext("store.routingReady=true;store.search='warehouse';store.filter='attention';store.batchId='batch-a';store.library.search='hotel';navigatePage('library');",p);
  assert.equal(value(p,'store.search'),'warehouse');
  assert.equal(value(p,'store.filter'),'attention');
  assert.equal(value(p,'store.library.search'),'hotel');
  vm.runInContext("navigatePage('create');navigatePage('board');",p);
  assert.equal(value(p,'store.savedForNavigation'),1);
});
test('explicit asset and batch links focus targets without leaking filters across pages',()=>{
  const p=context();
  vm.runInContext("store.routingReady=true;store.search='keep';store.library.search='old';store.library.model='unknown';navigatePage('library',{job:'video-b'});",p);
  assert.equal(value(p,'store.library.selectedId'),'video-b');
  assert.equal(value(p,'store.library.search'),'');
  assert.equal(value(p,'store.library.model'),'all');
  assert.equal(value(p,'store.search'),'keep');
  vm.runInContext("navigatePage('board',{batch:'batch-2',job:'video-2'});",p);
  assert.equal(value(p,'store.boardFocusId'),'batch-2');
  assert.equal(value(p,'store.batchId'),'batch-2');
  assert.equal(value(p,'store.selectedId'),'video-2');
});
test('saved workspace state excludes credentials, media and generation content',()=>{
  const p=context();
  vm.runInContext("store.bootstrap={simulation:true,csrf_token:'private'};store.scenario.drafts=[{spec:{global_prompt:'private draft'}}];store.detail={raw:'private media'};saveWorkspaceState();",p);
  const saved=value(p,"JSON.parse(localStorage.getItem(workspaceStorageKey()))");
  assert.equal(saved.version,1);
  assert.equal(JSON.stringify(saved).includes('private'),false);
});
test('a different source job never inherits the previous segment preview',()=>{
  const p=context();
  vm.runInContext("store.selectedId='old';store.previewSegment=2;store.previewURL='/api/media/old/segment-2';navigatePage('board',{batch:'next',job:'new'});",p);
  assert.equal(value(p,'store.previewSegment'),null);
  assert.equal(value(p,'store.previewURL'),'');
});
test('background refresh does not select a job or take over the creation page',async()=>{
  const p=context();
  vm.runInContext("store.page='create';store.bootstrap={};store.selectedId='video';api=async()=>({jobs:[{id:'video',status:'generating'}],batches:[],summary:{}});loadDetail=async()=>{throw Error('hidden page detail must not load');};",p);
  await vm.runInContext('refreshState(true)',p);
  assert.equal(value(p,'store.page'),'create');
  assert.equal(value(p,'store.selectedId'),'video');
});
test('submission waits for an older poll and obtains a fresh state before focusing',async()=>{
  const p=context();let finish; p.pendingPoll=new Promise(resolve=>{finish=resolve;});
  vm.runInContext("store.bootstrap={};store.page='create';store.polling=true;store.refreshPromise=pendingPoll;store.state={jobs:[{id:'old'}],batches:[],summary:{}};store.apiReads=0;api=async()=>{store.apiReads++;return {jobs:[{id:'new',batch_id:'new-batch',status:'queued'}],batches:[{id:'new-batch'}],summary:{}};};toast=()=>{};",p);
  const task=vm.runInContext("onCreationSubmitted({batch:{id:'new-batch'},jobs:[{id:'new',batch_id:'new-batch'}]},{quantity:1},{page:'create',epoch:0})",p);
  vm.runInContext('store.polling=false;',p);finish();await task;
  assert.equal(value(p,'store.apiReads'),1);
  assert.equal(value(p,'store.selectedId'),'new');
  assert.equal(value(p,'store.state.jobs.some(job=>job.id==="new")'),true);
});
test('late submission completion respects a newer navigation intent',async()=>{
  const p=context();
  vm.runInContext("store.page='library';store.navigationEpoch=3;store.bootstrap={};api=async()=>({jobs:[{id:'new',status:'queued'}],batches:[],summary:{}});toast=()=>{};",p);
  await vm.runInContext("onCreationSubmitted({batch:{id:'batch'},jobs:[{id:'new',batch_id:'batch'}]},{quantity:1},{page:'create',epoch:2})",p);
  assert.equal(value(p,'store.page'),'library');
});
