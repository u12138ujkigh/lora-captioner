"use strict";
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

let state = {
  type: "person",
  // 双语缓存：{path: {en, zh, segments:[{en,zh}], mode:"en"|"zh"}}
  bi: {},
  biMode: "en",
  providers: [],
  curProvider: "",
  curModel: "",
  transModel: "",
  files: [],            // {path,name,status,caption,error,duration_ms}
  selected: null,
  filter: "all",
  running: false,
  lastDir: "",
};

/* ===================== 工具 ===================== */
function toast(msg, ms=2200){ const t=$("#toast"); t.textContent=msg; t.classList.add("show"); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("show"),ms); }
const _timeFmt=new Intl.DateTimeFormat(undefined,{hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
function log(kind, msg){
  const box=$("#logBox");
  const div=document.createElement("div");
  const ts=document.createElement("span");
  ts.className="t"; ts.textContent=_timeFmt.format(new Date());
  const body=document.createElement("span");
  body.className="l-"+kind; body.textContent=msg;
  div.appendChild(ts); div.appendChild(body);
  const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<40;
  box.appendChild(div);
  while(box.children.length>600) box.removeChild(box.firstChild);
  if(atBottom) box.scrollTop=box.scrollHeight;   // 用户上翻时不强拉
}
function fmtSize(b){ return b>1048576?(b/1048576).toFixed(1)+"MB":(b/1024).toFixed(0)+"KB"; }

/* ===================== 类型切换 ===================== */
const TYPE_LABEL={person:"人物",clothing:"服装",style:"风格",body:"身材"};
function applyType(type){
  state.type=type;
  $$("#typeCards .typeCard").forEach(x=>{
    const on=x.dataset.type===type;
    x.classList.toggle("active",on);
    x.setAttribute("aria-pressed",on?"true":"false");
  });
  ["person","clothing","style","body"].forEach(t=>{
    const grp=document.querySelector(`section[data-group="${t}"]`);
    if(grp) grp.hidden = t!==type;
  });
  const badge=document.querySelector('section[data-group="type"] [data-role="badge"]');
  if(badge) badge.textContent=TYPE_LABEL[type]||type;
  $("#triggerHint").textContent = type==="style"
    ? "风格 LoRA 可不填触发词，纯内容描述即可"
    : (type==="body"
      ? "身材 LoRA 默认无触发词；填了会拼到前方"
      : "程序自动拼接到每条 caption 开头");
}
$$("#typeCards .typeCard").forEach(c=>c.addEventListener("click",()=>applyType(c.dataset.type)));

/* ===================== 模式切换（放大 / 打标） ===================== */
/* 两个功能各有自己的 UI、自己的批次、自己的按钮。
   切换时整个左栏内容重建，中间网格共用。 */
let _mode = "caption";

const CAPTION_GROUPS = ["type","person","clothing","style","body","naming","trigger","engine","advanced"];
const UPSCALE_GROUPS = ["up-settings"];

function setMode(mode, silent){
  _mode = mode;
  document.body.dataset.mode = mode;

  // 标签
  $$(".modeTab").forEach(t=>{
    const on=t.dataset.mode===mode;
    t.classList.toggle("active",on);
    t.setAttribute("aria-selected",on?"true":"false");
  });

  // 左栏分组显隐
  CAPTION_GROUPS.forEach(g=>{
    const el=document.querySelector(`section[data-group="${g}"]`);
    if(el) el.hidden = (mode!=="caption");
  });
  UPSCALE_GROUPS.forEach(g=>{
    const el=document.querySelector(`section[data-group="${g}"]`);
    if(el) el.hidden = (mode!=="upscale");
  });

  // 顶栏按钮
  const isUp = mode==="upscale";
  $("#btnStart").hidden = isUp;
  $("#btnStop").hidden = isUp;
  $("#btnUpStart").hidden = !isUp;
  $("#btnUpStop").hidden = !isUp;

  // 打标辅助按钮在放大模式下也该隐藏
  ["#btnQueueAdd","#btnAudit","#btnReplace"].forEach(sel=>{
    const el=$(sel); if(el) el.hidden = isUp;
  });

  // 中间区：打标看 fileGrid，放大看 upPanelCenter（自带网格与筛选）
  const grid=$("#fileGrid"), upC=$("#upPanelCenter");
  const gridHead=$("#gridHeader"), kbd=$(".kbdHint");
  if(grid) grid.hidden = isUp;
  if(upC) upC.hidden = !isUp;
  if(gridHead) gridHead.hidden = isUp;
  if(kbd) kbd.hidden = isUp;



  // 清屏按钮语义随模式走
  const clr=$("#btnClear");
  if(clr) clr.textContent = isUp ? "清空" : "清屏";

  if(!silent) log("sys", isUp ? "已切到素材放大" : "已切到打标");
  syncModeUI();
}

function syncModeUI(){
  const isUp=_mode==="upscale";
  syncUpscaleUI();
  if(isUp){
    loadUpBatch();
    // 有目录就自动扫一次，省得再点
    if($("#dirInput").value.trim() && !up.scanned) upScan();
    setTimeout(upLayout, 60);
  }
}

$$(".modeTab").forEach(t=>{
  t.addEventListener("click",()=>{
    if(_mode===t.dataset.mode) return;
    setMode(t.dataset.mode);
  });
});
// 键盘：Ctrl+1 / Ctrl+2 切换
document.addEventListener("keydown",e=>{
  if(!(e.ctrlKey||e.metaKey)) return;
  if(e.key==="1"){ e.preventDefault(); setMode("caption"); }
  if(e.key==="2"){ e.preventDefault(); setMode("upscale"); }
});

/* ===================== 素材放大（独立功能） ===================== */
let _upAvailable = false;
let _upPolling = null;

async function loadUpscaleStatus(){
  const el=$("#upscaleStatus");
  try{
    const r=await fetch("/api/upscale/status"); const d=await r.json();
    _upAvailable=!!d.available;
    el.className = _upAvailable ? "ok" : "bad";
    el.textContent = _upAvailable
      ? `可用 · ${d.unet} / ${d.vae}`
      : `不可用 · ${d.message}`;
    $("#btnUpStart").disabled = !_upAvailable;
  }catch(e){
    _upAvailable=false;
    el.className="bad";
    el.textContent="无法检查放大环境："+e.message;
  }
}

function syncUpscaleUI(){
  const badge=document.querySelector('section[data-group="up-settings"] [data-role="badge"]');
  if(badge){
    badge.textContent = !_upAvailable ? "不可用" : `${$("#upscaleMultiplier").value}×`;
  }
}

$("#upscaleMultiplier").addEventListener("change",syncUpscaleUI);


/* ---- 放大状态 ----
   up.items : [{path,name,width,height,done,out}]  扫描得到
   up.tasks : 运行中的任务（从 batch 接口同步）
   up.filter / up.only : 筛选
*/
const up = { items: [], tasks: [], filter: "all", only: "all",
             scanned: false, outDir: "", selected: null,
             picked: new Set() };   // 勾选的 path；切模式保留，刷新清空

/* ---- 扫描预览 ---- */
async function upScan(){
  const dir=$("#dirInput").value.trim();
  if(!dir){ toast("先选一个文件夹"); return; }
  const btn=$("#btnUpScan");
  btn.disabled=true; btn.textContent="扫描中…";
  try{
    const r=await fetch("/api/upscale/scan",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        directory:dir,
        recursive:$("#optRecursive").checked,
        out_dir:"",
      })});
    const d=await r.json();
    if(d.error){ toast(d.error); log("err", d.error); return; }
    up.items=d.items||[];
    up.outDir=d.out_dir;
    up.scanned=true;
    // 清掉已不在目录里的勾选
    const alive=new Set(up.items.map(i=>i.path));
    [...up.picked].forEach(p=>{ if(!alive.has(p)) up.picked.delete(p); });
    renderUpGrid();
    log("info",`扫描 ${d.total} 张：待处理 ${d.pending}，已完成 ${d.already}`);
    return d;
  }finally{
    btn.disabled=false; btn.textContent="扫描";
  }
}

/* ---- 开始放大：先扫描，再确认 ---- */
async function upStart(){
  if(!_upAvailable){ toast("放大环境不可用"); return; }

  // 有勾选就只处理勾选的；没有就扫全部
  const picked = pickedItems();
  const usePicked = picked.length > 0;

  let d;
  try{ d=await upScan(); }catch(e){ toast("扫描失败："+e.message); return; }
  if(!d) return;

  // 待处理集合
  let pending, already;
  if(usePicked){
    pending = picked.filter(i=>!i.done);
    already = picked.filter(i=>i.done);
  }else{
    pending = up.items.filter(i=>!i.done);
    already = up.items.filter(i=>i.done);
  }

  if(!pending.length && !already.length){
    toast(usePicked ? "勾选的图片都已不存在" : "目录里没有图片");
    return;
  }

  // 只有已放大的：问是否覆盖重跑
  let overwrite = false;
  if(!pending.length && already.length){
    if(!confirm(`勾选的 ${already.length} 张都已放大过。\n\n是否覆盖重跑？`)) return;
    overwrite = true;
    pending = already;
  }else if(already.length){
    // 混合：有新的、也有已放大的
    const msg = `即将放大 ${pending.length} 张${usePicked?"（勾选）":""}\n\n`
      + `源目录：${d.src_dir}\n`
      + `输出到：${d.out_dir}\n`
      + `倍数：${$("#upscaleMultiplier").value}×\n\n`
      + `另有 ${already.length} 张已存在结果。\n`
      + `点「确定」只跑新的；点「取消」先停下，\n`
      + `想覆盖重跑请只勾选那 ${already.length} 张。`;
    if(!confirm(msg)) return;
  }else{
    const msg = `即将放大 ${pending.length} 张${usePicked?"（勾选）":""}\n\n`
      + `源目录：${d.src_dir}\n`
      + `输出到：${d.out_dir}\n`
      + `倍数：${$("#upscaleMultiplier").value}×`;
    if(!confirm(msg)) return;
  }

  const body = {
    directory:$("#dirInput").value.trim(),
    recursive:$("#optRecursive").checked,
    multiplier:parseFloat($("#upscaleMultiplier").value),
    sharpen:$("#optUpscaleSharpen").checked,
    skip_existing: !overwrite,
  };
  // 只处理勾选的（覆盖模式下也要指定）
  if(usePicked) body.only_paths = pending.map(i=>i.path);

  const r=await fetch("/api/upscale/start",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});
  const res=await r.json();
  if(res.error){ toast(res.error); log("err", res.error); return; }

  log("info",`开始放大 ${res.total} 张${overwrite?"（覆盖重跑）":""}`
    + (res.skipped?`（跳过 ${res.skipped}）`:""));
  upSetRunning(true);
  startUpPolling();
}

$("#btnUpStart").addEventListener("click", upStart);

/* ---- 单张重试 / 重跑失败 ---- */
async function upRetry(paths){
  if(!paths || !paths.length){ toast("没有可重试的图片"); return; }
  const r=await fetch("/api/upscale/start",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      directory:$("#dirInput").value.trim(),
      recursive:$("#optRecursive").checked,
      multiplier:parseFloat($("#upscaleMultiplier").value),
      sharpen:$("#optUpscaleSharpen").checked,
      skip_existing:false,
      only_paths:paths,
    })});
  const d=await r.json();
  if(d.error){ toast(d.error); return; }
  log("info",`重试 ${d.total} 张`);
  upSetRunning(true);
  startUpPolling();
}

$("#btnUpRetryFailed").addEventListener("click",()=>{
  const failed=up.items.filter(i=>i.state==="error").map(i=>i.path);
  if(!failed.length){ toast("没有失败的图片"); return; }
  upRetry(failed);
});

/* ---- 网格渲染 ---- */
function upPass(it){
  // 状态筛选
  if(up.filter!=="all"){
    const st=it.state|| (it.done?"done":"pending");
    if(st!==up.filter) return false;
  }
  // 已放大/原图
  if(up.only==="done" && !it.done) return false;
  if(up.only==="orig" && it.done) return false;
  return true;
}

function renderUpGrid(){
  const grid=$("#upGrid");
  if(!grid) return;
  // 把运行中任务的实时状态并进 items
  const live=new Map(up.tasks.map(t=>[t.path,t]));
  up.items.forEach(it=>{
    const t=live.get(it.path);
    if(t){ it.state=t.status; it.live=t; }
    else if(it.done){ it.state="done"; }
    else if(it.state!=="error"){ it.state="pending"; }
  });

  const files=up.items.filter(upPass);

  // 计数
  const doneN=up.items.filter(i=>i.done||i.state==="done").length;
  const errN=up.items.filter(i=>i.state==="error").length;
  const pendN=up.items.length-doneN-errN;
  $("#upCount").textContent = up.items.length
    ? `${files.length} / ${up.items.length} 张` : "";
  $$("#upFilters .fbtn").forEach(b=>{
    const k=b.dataset.uf;
    const el=b.querySelector(".n");
    const n = k==="all"?up.items.length : k==="pending"?pendN : k==="done"?doneN : errN;
    if(el) el.textContent = n>0?n:"";
  });
  $$("#upFilters .fbtn").forEach(b=>{
    const on=b.dataset.uf===up.filter;
    b.classList.toggle("active",on);
    b.setAttribute("aria-pressed",on?"true":"false");
  });
  $$(".upToggle .fbtn").forEach(b=>{
    const on=b.dataset.uo===up.only;
    b.classList.toggle("active",on);
    b.setAttribute("aria-pressed",on?"true":"false");
  });

  // 失败重跑按钮
  const rf=$("#btnUpRetryFailed");
  if(rf) rf.hidden = errN===0;

  grid.innerHTML="";
  grid.style.height="";

  if(!up.items.length){
    grid.innerHTML='<div class="empty-hint"><strong>还没有扫描</strong>'
      + '<span>选好文件夹后点右上角「扫描」，或直接点顶栏「开始放大」</span></div>';
    return;
  }
  if(!files.length){
    grid.innerHTML='<div class="empty-hint"><strong>这个筛选下没有图片</strong>'
      + '<span>换一个筛选条件试试</span></div>';
    return;
  }

  const frag=document.createDocumentFragment();
  files.forEach(it=>{
    const card=document.createElement("div");
    const st=it.state||(it.done?"done":"pending");
    card.className="upCard"
      + (st==="done"?" is-done":st==="error"?" is-error":st==="running"?" is-running":" is-pending")
      + (up.selected===it.path?" selected":"")
      + (up.picked.has(it.path)?" picked":"");
    card.dataset.path=it.path;
    card.tabIndex=0;
    card.title=it.name + (it.live&&it.live.error?"\n错误："+it.live.error:"");

    // 已放大的显示输出图，否则显示原图
    const src = it.done ? `/api/image?path=${encodeURIComponent(it.out||it.path)}`
                        : `/api/image?path=${encodeURIComponent(it.path)}`;
    const w=it.width||1, h=it.height||1;

    const badge = st==="done"?"放大后":st==="running"?"处理中"
      :st==="error"?"失败":"原图";
    const dims = it.live&&it.live.to
      ? `${it.live.from?it.live.from[0]+"×"+it.live.from[1]:"?"} → ${it.live.to[0]}×${it.live.to[1]}`
      : (it.done? "" : `${w}×${h}`);

    card.innerHTML=`<img loading="lazy" decoding="async" width="${w}" height="${h}"
        style="aspect-ratio:${w}/${h}" src="${src}" alt="${it.name}">
      <div class="pick" aria-hidden="true">✓</div>
      <div class="ubadge">${badge}</div>
      <div class="ushield">放大中…</div>
      ${dims?`<div class="udims">${dims}</div>`:""}
      <button class="eye" type="button" title="查看这张的放大对比">◉</button>
      <div class="uname">${it.name}</div>`;
    card.querySelector("img").addEventListener("load",e=>{e.target.style.opacity=1;},{once:true});
    card.querySelector("img").style.opacity=0;
    // 点击整卡 = 选中/取消选中
    card.addEventListener("click",e=>{
      if(e.target.closest(".eye")) return;      // 眼睛按钮自己处理
      upTogglePick(it.path);
    });
    card.addEventListener("keydown",e=>{
      if(e.key==="Enter"||e.key===" "){ e.preventDefault(); upTogglePick(it.path); }
    });
    // 眼睛按钮 = 看对比（不改变选中）
    card.querySelector(".eye").addEventListener("click",e=>{
      e.stopPropagation();
      upSelect(it.path);
    });
    if(st==="error"){
      card.addEventListener("dblclick",()=>upRetry([it.path]));
      card.title += "\n双击重试这张";
    }
    frag.appendChild(card);
  });
  grid.appendChild(frag);
  upLayout();
  renderPickCount();
}

/* 瀑布流布局（与打标网格同一套算法，独立尺寸控制） */
const UP_GAP_X=10, UP_GAP_Y=10, UP_NAME_H=22;
function upLayout(){
  const grid=$("#upGrid");
  if(!grid) return;
  const cards=[...grid.querySelectorAll(".upCard")];
  if(!cards.length) return;

  const avail=grid.clientWidth-24;
  const minCol=parseInt(getComputedStyle(grid).getPropertyValue("--col-w"),10) || 126;
  const cols=Math.max(1, Math.floor((avail+UP_GAP_X)/(minCol+UP_GAP_X)));
  const cardW=Math.floor((avail-UP_GAP_X*(cols-1))/cols);
  if(cardW<=0) return;

  const heights=new Array(cols).fill(0);
  cards.forEach(card=>{
    const it=up.items.find(x=>x.path===card.dataset.path);
    const w=it&&it.width>0?it.width:1, h=it&&it.height>0?it.height:1;
    const imgH=Math.round(cardW*h/w);
    const cardH=imgH+UP_NAME_H+2;
    let col=0;
    for(let i=1;i<cols;i++) if(heights[i]<heights[col]-0.5) col=i;
    card.style.left=(col*(cardW+UP_GAP_X))+"px";
    card.style.top=heights[col]+"px";
    card.style.width=cardW+"px";
    heights[col]+=cardH+UP_GAP_Y;
  });
  grid.style.height=(Math.max(...heights)-UP_GAP_Y+24)+"px";
}

/* 勾选/取消勾选（不打开对比） */
function upTogglePick(path){
  if(up.picked.has(path)) up.picked.delete(path);
  else up.picked.add(path);
  syncPickUI(path);
}

/* 只更新受影响卡片的类与计数，不重渲染整个网格 */
function syncPickUI(path){
  const card=document.querySelector(`.upCard[data-path="${CSS.escape(path)}"]`);
  if(card) card.classList.toggle("picked", up.picked.has(path));
  renderPickCount();
}

function renderPickCount(){
  const n=up.picked.size;
  const el=$("#upSelCount");
  if(el){
    el.textContent=`已选 ${n}`;
    el.classList.toggle("zero", n===0);
  }
  // 开始放大按钮文案随选中数变化
  const b=$("#btnUpStart");
  if(b && !b.disabled){
    b.textContent = n>0 ? `放大选中的 ${n} 张` : "开始放大";
  }
}

function pickedItems(){
  return up.items.filter(i=>up.picked.has(i.path));
}

/* 批量选择：作用于当前筛选下可见的图 */
function upVisibleItems(){
  return up.items.filter(upPass);
}
function upSelAll(){
  upVisibleItems().forEach(i=>up.picked.add(i.path));
  renderUpGrid();
}
function upSelNone(){
  up.picked.clear();
  renderUpGrid();
}
function upSelInv(){
  upVisibleItems().forEach(i=>{
    if(up.picked.has(i.path)) up.picked.delete(i.path);
    else up.picked.add(i.path);
  });
  renderUpGrid();
}

$("#btnUpSelAll") && $("#btnUpSelAll").addEventListener("click",upSelAll);
$("#btnUpSelNone") && $("#btnUpSelNone").addEventListener("click",upSelNone);
$("#btnUpSelInv") && $("#btnUpSelInv").addEventListener("click",upSelInv);

/* 点选：右栏看对比 */
function upSelect(path){
  const it=up.items.find(x=>x.path===path);
  if(!it) return;
  up.selected=path;
  $$(".upCard").forEach(c=>c.classList.toggle("selected",c.dataset.path===path));

  $("#previewEmpty").hidden=true;
  $("#previewBody").classList.add("show");
  $("#pvName").textContent = it.done
    ? `${path}\n→ 已放大：${it.out||""}` : path;

  // 已放大：显示对比；否则只显示原图
  if(it.done){
    cmpSetupUp(it.path, it.out);
  }else{
    const wrap=$("#cmpWrap"), img=$("#previewImg");
    if(wrap) wrap.hidden=true;
    if(img){ img.style.display=""; img.src="/api/image?path="+encodeURIComponent(path); }
  }

  // 状态行（放大模式不显示 caption 相关）
  const ow = it.out_width||0, oh = it.out_height||0;
  const dims = it.done
    ? `<span class="pvState st-ok">状态 <b>已放大</b></span>`
      + `<span><b>${it.width||"?"}×${it.height||"?"}</b> 原图</span>`
      + (ow?`<span><b>${ow}×${oh}</b> 放大后</span>`:`<span>放大后尺寸待刷新</span>`)
    : `<span class="pvState st-pend">状态 <b>待处理</b></span>`
      + `<span><b>${it.width||"?"}×${it.height||"?"}</b> 原图</span>`;
  $("#pvStatus").innerHTML=dims;
}

/* 放大模式的对比：左原图，右放大图 */
function cmpSetupUp(srcPath, outPath){
  const wrap=$("#cmpWrap"), img=$("#previewImg");
  if(!wrap||!outPath){
    if(img){ img.style.display=""; img.src="/api/image?path="+encodeURIComponent(outPath||srcPath); }
    return;
  }
  wrap.hidden=false;
  if(img) img.style.display="none";
  const after=$("#cmpAfter"), before=$("#cmpBefore");
  after.src="/api/image?path="+encodeURIComponent(outPath);
  before.src="/api/image?path="+encodeURIComponent(srcPath);
  wrap.style.setProperty("--x","100%");
}

/* ---- 筛选与尺寸按钮 ---- */
$$("#upFilters .fbtn").forEach(b=>b.addEventListener("click",()=>{
  up.filter=b.dataset.uf; renderUpGrid();
}));
$$(".upToggle .fbtn").forEach(b=>b.addEventListener("click",()=>{
  up.only=b.dataset.uo; renderUpGrid();
}));
$("#btnUpScan").addEventListener("click",()=>upScan());

/* ---- 轮询 ---- */
function upSetRunning(run){
  const b=$("#btnUpStart");
  b.disabled = run || !_upAvailable;
  $("#btnUpStop").disabled = !run;
  if(!run) renderPickCount();      // 恢复「放大选中的 N 张」
  if(run) b.textContent = "放大中…";
}

function startUpPolling(){
  stopUpPolling();
  _upPolling=setInterval(loadUpBatch, 1200);
}
function stopUpPolling(){
  if(_upPolling){ clearInterval(_upPolling); _upPolling=null; }
}

async function loadUpBatch(){
  try{
    const r=await fetch("/api/upscale/batch"); const d=await r.json();
    up.tasks=d.tasks||[];
    renderUpStats(d);
    renderUpGrid();
    if(!d.running){
      upSetRunning(false);
      stopUpPolling();
      upScan();          // 跑完刷新一次扫描结果
    }
  }catch(e){ /* 忽略，下次再试 */ }
}

function renderUpStats(d){
  const head=$("#upStats");
  if(!head) return;
  const st=d.stats||{};
  if(!st.total){ head.hidden=true; return; }
  head.hidden=false;
  head.innerHTML=`总数 <b>${st.total}</b> · 完成 <b>${st.done}</b>`
    + (st.errors?` · 失败 <b style="color:var(--fail)">${st.errors}</b>`:"")
    + (st.elapsed_s?` · ${st.elapsed_s}s`:"")
    + (st.avg_s?` · 平均 ${st.avg_s}s/张`:"");
  if(st.total){
    setProgress(Math.round((st.done+st.errors)/st.total*100));
    $("#statText").textContent = d.running
      ? `放大 ${st.done+st.errors}/${st.total}`
      : `放大完成 ${st.done}/${st.total}`;
  }
}

/* ===================== 文件夹选择器 ===================== */
/* 点「📂」调用服务端的 Windows 原生对话框——浏览器出于安全
   无法拿到本地绝对路径，只能由本机进程弹窗。 */
$("#btnBrowse").addEventListener("click",async()=>{
  const btn=$("#btnBrowse");
  const old=btn.textContent;
  btn.disabled=true; btn.textContent="…";
  try{
    const cur=$("#dirInput").value.trim();
    const r=await fetch("/api/pick-folder?initial="+encodeURIComponent(cur),
                        {method:"GET"});
    const d=await r.json();
    if(d.error){ toast(d.error); log("err", d.error); return; }
    if(d.cancelled){ return; }
    $("#dirInput").value=d.path;
    syncDirBadge();
    await scanDir();
    log("ok",`已选择文件夹：${d.path}`);
  }catch(e){
    toast("打开选择器失败："+e.message);
  }finally{
    btn.disabled=false; btn.textContent=old;
  }
});

/* ===================== Provider ===================== */
async function loadProviders(force){
  try{
    const url=force?"/api/providers?refresh=true":"/api/providers";
    const r=await fetch(url); const d=await r.json();
    state.providers=d.providers||[];
    const sel=$("#providerSel"); sel.innerHTML="";
    if(!state.providers.length){ sel.innerHTML="<option>（未发现可用 Provider）</option>"; return; }
    state.providers.forEach(p=>{
      const o=document.createElement("option");
      o.value=p.provider_id; o.textContent=`${p.label}（${p.models.length} 模型）`;
      sel.appendChild(o);
    });
    // 恢复上次或默认选 dsh:buddy
    const last=state.lastCfg&&state.lastCfg.provider_id;
    const buddy=state.providers.find(p=>p.provider_id==="dsh:buddy");
    const pick=state.providers.find(p=>p.provider_id===last)||buddy||state.providers[0];
    sel.value=pick.provider_id;
    onProviderChange();
  }catch(e){ log("err","Provider 列表加载失败: "+e.message); }
}
function onProviderChange(){
  const pid=$("#providerSel").value;
  const p=state.providers.find(x=>x.provider_id===pid);
  state.curProvider=pid;
  const msel=$("#modelSel"); msel.innerHTML="";
  if(!p){ state.curModel=""; return; }
  const visionModels=p.models.filter(m=>m.vision);
  const nonVision=p.models.filter(m=>!m.vision);
  if(visionModels.length){
    const g=document.createElement("optgroup"); g.label="✓ 可看图";
    visionModels.forEach(m=>{const o=document.createElement("option");o.value=m.id;o.textContent=m.name;g.appendChild(o);});
    msel.appendChild(g);
  }
  if(nonVision.length){
    const g=document.createElement("optgroup"); g.label="✗ 不支持看图";
    nonVision.forEach(m=>{const o=document.createElement("option");o.value=m.id;o.textContent=m.name;g.appendChild(o);});
    msel.appendChild(g);
  }
  // 默认挑上次用过的或第一个看图模型
  const lastModel=state.lastCfg&&state.lastCfg.model;
  const cand=p.models.find(m=>m.id===lastModel)||visionModels[0]||p.models[0];
  if(cand){ msel.value=cand.id; state.curModel=cand.id; }
  $("#modelHint").textContent = p.kind==="ollama" ? "本地 Ollama；模型不支持看图时会返回空" : "DSH 渠道模型，能否看图以渠道名册标注为准";
  fillTransModels(p);
  syncEngineBadge();
}
function fillTransModels(p){
  const tsel=$("#transModelSel"); const cur=tsel.value;
  tsel.innerHTML="<option value=''>（跟打标模型一致）</option>";
  (p.models||[]).forEach(m=>{const o=document.createElement("option");o.value=m.id;o.textContent=m.name;tsel.appendChild(o);});
  tsel.value=cur;
}
$("#providerSel").addEventListener("change",onProviderChange);
$("#modelSel").addEventListener("change",()=>{ state.curModel=$("#modelSel").value; });
$("#btnRefreshProv").addEventListener("click",async()=>{
  const btn=$("#btnRefreshProv"); btn.disabled=true; btn.textContent="⏳ 刷新中…";
  try{ await loadProviders(true); toast("引擎列表已刷新"); }
  finally{ btn.disabled=false; btn.textContent="🔄 刷新引擎列表"; }
});

/* ===================== 目录浏览 ===================== */
async function openBrowser(){
  const b=$("#dirBrowser");
  if(b.style.display==="block"){ b.style.display="none"; return; }
  await browseTo(state.lastDir||"D:\\deepseek");
  b.style.display="block";
}
async function browseTo(path){
  const r=await fetch("/api/browse?path="+encodeURIComponent(path));
  const d=await r.json();
  const b=$("#dirBrowser");
  b.innerHTML="";
  const crumb=document.createElement("div");
  crumb.className="crumb";
  crumb.textContent="📁 "+d.current+"    （点击进入 · 选此目录请点下方按钮）";
  crumb.addEventListener("click",()=>{ $("#dirInput").value=d.current; });
  b.appendChild(crumb);
  if(d.parent){
    const up=document.createElement("div");
    up.className="dirItem"; up.innerHTML="<span>⬆ ..</span>";
    up.addEventListener("click",()=>browseTo(d.parent));
    b.appendChild(up);
  }
  d.dirs.forEach(x=>{
    const it=document.createElement("div");
    it.className="dirItem"; it.innerHTML=`<span>📁 ${x.name}</span>`;
    it.addEventListener("click",()=>browseTo(x.path));
    b.appendChild(it);
  });
  const pick=document.createElement("button");
  pick.className="btn small primary"; pick.style.margin="6px auto"; pick.style.display="block";
  pick.textContent="选定此目录";
  pick.addEventListener("click",()=>{ $("#dirInput").value=d.current; b.style.display="none"; scanDir(); });
  b.appendChild(pick);
}
$("#btnBrowse").addEventListener("click",openBrowser);
$("#dirInput").addEventListener("change",scanDir);

/* ===================== 扫描 ===================== */
async function scanDir(){
  const dir=$("#dirInput").value.trim();
  if(!dir) return;
  const r=await fetch("/api/scan2",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({directory:dir,recursive:$("#optRecursive").checked})});
  const d=await r.json();
  if(d.error){ toast(d.error); return; }
  state.lastDir=dir;
  // 合并已有任务状态（清屏前保留的 done 状态在 grid 上标注）
  const prev=new Map(state.files.map(f=>[f.path,f]));
  state.files=(d.items||[]).map(it=>{
    const p=prev.get(it.path);
    const base={path:it.path,name:it.name,status:it.has_caption?(p&&p.status!=="pending"?p.status:"skipped"):"pending",
      caption:"",error:"",duration_ms:0,size:it.size,
      w:it.width||0,h:it.height||0,has_orig:!!it.has_orig};
    return base;
  });
  renderGrid();
  // 重置进度条（新批次）
  setProgress(0);
  $("#statText").textContent=d.count?`${d.count} 张待命`:"待命";
  syncDirBadge(d.count);
  log("info",`扫描 ${dir}：${d.count} 张图片（${state.files.filter(f=>f.status==="skipped").length} 张已有 caption）`);
}

/* ===================== 网格渲染 ===================== */
function fadeInImg(img){
  if(!img) return;
  if(img.complete && img.naturalWidth) { img.style.opacity=1; return; }
  img.style.opacity=0;
  img.addEventListener("load",()=>{ img.style.opacity=1; },{once:true});
  img.addEventListener("error",()=>{ img.style.opacity=1; });  // 失败也别留空白
}
const STATUS_LABEL={pending:"待处理",upscaling:"放大中",running:"打标中",done:"已完成",
  edited:"已编辑",error:"失败",skipped:"跳过"};
function statusOf(f){
  return state.filter==="all"?true
    :state.filter==="done"?(f.status==="done"||f.status==="edited")
    :f.status===state.filter;
}
/* 瀑布流布局：把卡片逐个放进当前最矮的一列。
   图片宽高来自后端扫描，布局时不需要等图片加载，也不做同步布局读取。 */
const FNAME_H = 22;      // 文件名条高度（20px + 边框）
const GAP_X = 10, GAP_Y = 10;
function layoutMasonry(){
  const grid=$("#fileGrid");
  const cards=[...grid.querySelectorAll(".fileCard")];
  if(!cards.length) return;

  const avail=grid.clientWidth - 24;                 // 减去左右 padding
  const minCol=parseInt(getComputedStyle(grid).getPropertyValue("--col-w"),10) || 126;
  const cols=Math.max(1, Math.floor((avail + GAP_X) / (minCol + GAP_X)));
  const cardW=Math.floor((avail - GAP_X*(cols-1)) / cols);
  if(cardW<=0) return;

  const heights=new Array(cols).fill(0);
  cards.forEach(card=>{
    const f=state.files.find(x=>x.path===card.dataset.path);
    const w=f&&f.w>0?f.w:1, h=f&&f.h>0?f.h:1;
    const imgH=Math.round(cardW * h / w);
    const cardH=imgH + FNAME_H + 2;                  // + 上下边框
    let col=0;
    for(let i=1;i<cols;i++) if(heights[i]<heights[col]-0.5) col=i;   // 最矮列
    card.style.left=(col*(cardW+GAP_X))+"px";
    card.style.top=heights[col]+"px";
    card.style.width=cardW+"px";
    heights[col]+=cardH+GAP_Y;
  });
  grid.style.height=(Math.max(...heights) - GAP_Y + 24)+"px";
}

function renderGrid(){
  const grid=$("#fileGrid");
  grid.innerHTML="";
  grid.style.height="";
  const files=state.files.filter(statusOf);
  $("#gridCount").textContent=state.files.length?`${files.length} / ${state.files.length} 张`:"";
  updateFilterCounts();
  if(!files.length){
    grid.innerHTML=state.files.length
      ? '<div class="empty-hint"><strong>这个筛选下没有文件</strong><span>换一个筛选条件，或点「全部」查看全部图片</span></div>'
      : '<div class="empty-hint"><strong>还没有扫描目录</strong><span>在左侧填入数据集路径，或点目录按钮浏览</span></div>';
    grid.style.height="";
    return;
  }
  const frag=document.createDocumentFragment();
  files.forEach(f=>{
    const card=document.createElement("div");
    card.className=`fileCard st-${f.status}`+(state.selected===f.path?" selected":"");
    card.dataset.path=f.path;
    card.setAttribute("role","option");
    card.setAttribute("aria-selected",state.selected===f.path?"true":"false");
    card.tabIndex=0;
    card.title = f.name + (f.error ? "\n错误：" + f.error : "");
    // 用原图宽高做占位：加载前就按真实比例留出空间，避免瀑布流抖动
    const imgAttrs = f.w>0 && f.h>0
      ? `width="${f.w}" height="${f.h}" style="aspect-ratio:${f.w}/${f.h}"`
      : 'style="aspect-ratio:1/1"';
    card.innerHTML=`<img loading="lazy" decoding="async" ${imgAttrs} src="/api/image?path=${encodeURIComponent(f.path)}" alt="${f.name}">
      <div class="uptag" title="已放大">UP</div>
      <div class="upshield">放大中</div>
      <div class="badge" aria-hidden="true"></div><div class="fname">${f.name}</div>
      <div class="errtag">${f.error||"错误"}</div>`;
    if(isUpscaledFile(f)) card.classList.add("is-upscaled");
    fadeInImg(card.querySelector("img"));
    card.addEventListener("click",()=>selectFile(f.path));
    card.addEventListener("keydown",e=>{
      if(e.key==="Enter"||e.key===" "){ e.preventDefault(); selectFile(f.path); }
    });
    frag.appendChild(card);
  });
  grid.appendChild(frag);
  layoutMasonry();
}
function updateFilterCounts(){
  const n={all:state.files.length,pending:0,done:0,error:0,skipped:0};
  state.files.forEach(f=>{
    if(f.status==="done"||f.status==="edited") n.done++;
    else if(n[f.status]!==undefined) n[f.status]++;
  });
  $$("#filterBtns .fbtn").forEach(b=>{
    const k=b.dataset.f;
    const el=b.querySelector(".n");
    if(el) el.textContent=n[k]!==undefined&&n[k]>0?n[k]:"";
  });
}
function updateCard(path){
  const f=state.files.find(x=>x.path===path); if(!f) return;
  const card=document.querySelector(`.fileCard[data-path="${CSS.escape(path)}"]`);
  if(!card) return;
  card.className=`fileCard st-${f.status}`+(state.selected===path?" selected":"")
    +(isUpscaledFile(f)?" is-upscaled":"");
  card.setAttribute("aria-selected",state.selected===path?"true":"false");
  const et=card.querySelector(".errtag"); if(f.error) et.textContent=f.error;
  if(f.error) card.title=f.name+"\n错误："+f.error;
  if(!statusOf(f)) card.remove();
  updateFilterCounts();
}
$$("#filterBtns .fbtn").forEach(b=>b.addEventListener("click",()=>{
  $$("#filterBtns .fbtn").forEach(x=>{
    const on=x===b;
    x.classList.toggle("active",on);
    x.setAttribute("aria-pressed",on?"true":"false");
  });
  state.filter=b.dataset.f; renderGrid();
}));
$("#btnRescan").addEventListener("click",scanDir);

/* ===================== 词数条 ===================== */
function countWords(text){
  const t=(text||"").trim();
  if(!t) return 0;
  // 含中文：按汉字数 + 英文词数
  const cjk=(t.match(/[\u4e00-\u9fff]/g)||[]).length;
  if(cjk>0){
    const en=t.replace(/[\u4e00-\u9fff]/g," ").trim();
    const enWords=en?en.split(/\s+/).filter(Boolean).length:0;
    return cjk+enWords;
  }
  return t.split(/\s+/).filter(Boolean).length;
}
function updateCapMeta(){
  const box=$("#captionBox");
  const w=countWords(box.value);
  const el=$("#capWords");
  el.textContent=`${w} 词`;
  // 30 词以下偏短，160 词以上偏长（与质检阈值一致）
  el.classList.toggle("over", w>160);
  el.classList.toggle("good", w>=30 && w<=160);
  const dirty=(box.value||"")!==(state.savedCaption||"");
  box.classList.toggle("dirty",dirty);
  $("#capDirty").hidden=!dirty;
}

/* ===================== 预览 ===================== */
async function selectFile(path){
  state.selected=path;
  $$(".fileCard").forEach(c=>c.classList.toggle("selected",c.dataset.path===path));
  const f=state.files.find(x=>x.path===path);
  $("#previewEmpty").hidden=true;
  $("#previewBody").classList.add("show");
  $("#previewImg").src="/api/image?path="+encodeURIComponent(path);
  $("#previewImg").classList.remove("zoomed");
  cmpSetup(path, isUpscaledFile(f));
  $("#pvName").textContent=path;
  // 读 caption
  const r=await fetch("/api/caption?path="+encodeURIComponent(path));
  const d=await r.json();
  if(f){ f.caption=d.caption; }
  $("#captionBox").value=d.caption||"";
  $("#captionBox").disabled=false;
  $("#captionBox").classList.remove("dirty");
  state.savedCaption=d.caption||"";
  $("#capDirty").hidden=true;
  updateCapMeta();
  $("#transBox").classList.remove("show");
  syncBiIndicator();
  const stMap={pending:["待处理","st-pend"],upscaling:["放大中","st-run"],running:["打标中","st-run"],done:["已完成","st-ok"],
    edited:["已编辑","st-edit"],error:["失败","st-err"],skipped:["跳过","st-skip"]};
  const meta=stMap[f.status]||[f.status,""];
  const st=f?`<span class="pvState ${meta[1]}">状态 <b>${meta[0]}</b></span><span><b>${f.duration_ms?(f.duration_ms/1000).toFixed(1)+"s":"—"}</b> 耗时</span>`:"";
  $("#pvStatus").innerHTML=st;
}
$("#previewImg").addEventListener("click",()=>$("#previewImg").classList.toggle("zoomed"));

/* ===================== 放大前后对比 ===================== */
/* 是否可对比：文件名带 _up 后缀，或同目录存在 .orig 备份。
   用命名规则判断而不是依赖运行时状态，重渲染后依然有效。 */
function isUpscaledFile(f){
  if(!f) return false;
  if(f.upscaled) return true;
  const n=(f.name||"").toLowerCase();
  if(/_up\.[a-z0-9]+$/.test(n)) return true;
  if(f.has_orig) return true;
  return false;
}

/* 默认显示放大后的图；鼠标移入后，竖线跟随鼠标，
   线左侧显示原图、右侧显示放大图；离开恢复全放大图。 */
function cmpSetup(path, isUpscaled){
  const wrap=$("#cmpWrap"), img=$("#previewImg");
  if(!isUpscaled){
    wrap.hidden=true; img.style.display="";
    return;
  }
  wrap.hidden=false;
  img.style.display="none";

  const after=$("#cmpAfter"), before=$("#cmpBefore");
  after.src="/api/image?path="+encodeURIComponent(path);
  before.src="/api/upscale/original?path="+encodeURIComponent(path)+"&t="+Date.now();
  wrap.style.setProperty("--x","100%");     // 默认：全显示放大图
}

function cmpReset(){
  const wrap=$("#cmpWrap");
  if(wrap) wrap.style.setProperty("--x","100%");
}

(function bindCmp(){
  const wrap=$("#cmpWrap");
  if(!wrap) return;
  const move=e=>{
    const r=wrap.getBoundingClientRect();
    const x=Math.max(0, Math.min(1, (e.clientX-r.left)/r.width));
    wrap.style.setProperty("--x",(x*100)+"%");
  };
  wrap.addEventListener("mousemove",move);
  wrap.addEventListener("mouseenter",move);
  wrap.addEventListener("mouseleave",()=>wrap.style.setProperty("--x","100%"));
  // 触屏：拖动也能看
  wrap.addEventListener("touchmove",e=>{
    if(!e.touches.length) return;
    const r=wrap.getBoundingClientRect();
    const x=Math.max(0, Math.min(1, (e.touches[0].clientX-r.left)/r.width));
    wrap.style.setProperty("--x",(x*100)+"%");
    e.preventDefault();
  },{passive:false});
  wrap.addEventListener("touchend",()=>wrap.style.setProperty("--x","100%"));
})();

$("#btnSaveCap").addEventListener("click",async()=>{
  if(!state.selected) return;
  const content=$("#captionBox").value;
  const r=await fetch("/api/caption",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:state.selected,content})});
  const d=await r.json();
  if(d.error){ toast("保存失败: "+d.error); return; }
  const f=state.files.find(x=>x.path===state.selected);
  if(f){ f.status="edited"; f.caption=content; updateCard(state.selected); }
  state.savedCaption=content;
  $("#capDirty").hidden=true;
  $("#captionBox").classList.remove("dirty");
  updateCapMeta();
  log("ok",`已保存${d.backup?"（旧文件备份 .bak）":""}: ${state.selected.split("\\").pop()}`);
  toast("已保存"+(d.backup?"，旧文件备份为 .bak":""));
});

function collectParams(extra={}){
  return Object.assign({
    lora_type:state.type,
    provider_id:$("#providerSel").value,
    model:$("#modelSel").value,
    person_name:$("#personName").value.trim(),
    generic_subject:$("#genericSubject").value.trim(),
    trigger_word:$("#triggerWord").value.trim(),
    trigger_position:$("#triggerPosition").value,
    style_hint:$("#styleHint").value.trim(),
    extra_instructions:$("#extraInstr").value.trim(),
  },extra);
}

$("#btnRetag").addEventListener("click",async()=>{
  if(!state.selected) return;
  const btn=$("#btnRetag"); btn.disabled=true; btn.textContent="⏳ 重打中…";
  try{
    const r=await fetch("/api/retag",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(Object.assign({path:state.selected},collectParams()))});
    const d=await r.json();
    if(d.error){ toast("重打失败: "+d.error); log("err","重打失败 "+d.error); return; }
    $("#captionBox").value=d.caption;
    state.savedCaption=d.caption;
    $("#capDirty").hidden=true;
    $("#captionBox").classList.remove("dirty");
    updateCapMeta();
    const f=state.files.find(x=>x.path===state.selected);
    if(f){ f.status="done"; f.caption=d.caption; updateCard(state.selected); }
    log("ok",`单张重打完成${d.backup?"（备份 .bak）":""}: ${state.selected.split("\\").pop()}`);
  }finally{ btn.disabled=false; btn.textContent="🔄 重打"; }
});

/* ===================== 中英双语切换 ===================== */
/*
  once 翻译后缓存 {en, zh, segments}；之后点按钮只切换显示，不再请求。
  编辑中文时按 segments 的词级映射同步增删对应英文。
*/
function biOf(path){ return state.bi[path]; }

async function ensureBi(path, force){
  const cached = state.bi[path];
  if(cached && !force && cached.zh) return cached;

  const text = $("#captionBox").value.trim();
  if(!text){ toast("caption 是空的，没有可翻译的内容"); return null; }

  const btn=$("#btnTrans");
  const oldLabel=btn.textContent;
  btn.disabled=true; btn.textContent="翻译中…";
  try{
    const model=$("#transModelSel").value||$("#modelSel").value;
    const r=await fetch("/api/translate",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        provider_id:$("#providerSel").value, model, text,
        engine:"bing", aligned:true,
      })});
    const d=await r.json();
    if(d.error){ toast("翻译失败："+d.error); return null; }
    const bi={en:text, zh:d.translation, segments:d.segments||[], mode:"zh"};
    state.bi[path]=bi;
    log("ok",`译好并缓存（${d.engine||"bing"}）`);
    return bi;
  }catch(e){
    toast("翻译请求失败："+e.message);
    return null;
  }finally{
    btn.disabled=false; btn.textContent=oldLabel;
  }
}

/* 词级联动：把当前（中文）内容与缓存的 zh 对比，同步到英文 */
function syncEnFromZh(path, newZh){
  const bi=biOf(path);
  if(!bi||!bi.segments||!bi.segments.length) return null;

  const segs=bi.segments;
  // 整句对齐模式：无法精确到词，退回重译提示
  if(segs.length===1 && segs[0].whole) return null;

  const nowParts=newZh.split(/[，,]/).map(x=>x.trim()).filter(Boolean);
  // 用最长公共子序列思路：保留仍然存在的段，删掉消失的段
  const kept=[];
  const pool=nowParts.slice();
  segs.forEach(sg=>{
    const i=pool.findIndex(p=>p===sg.zh||p.includes(sg.zh)||sg.zh.includes(p));
    if(i>=0){ kept.push(sg); pool.splice(i,1); }
  });
  // 新增的中文段（没有对应英文）无法反推，标记出来
  const added=pool;

  const newEn=kept.map(sg=>sg.en).join(", ");
  return {en:newEn, removed:segs.length-kept.length, added:added.length, addedParts:added};
}

$("#btnTrans").addEventListener("click",async()=>{
  if(!state.selected){ toast("先选一张图"); return; }
  const path=state.selected;

  // 已经有缓存：单纯切换显示
  const cached=biOf(path);
  if(cached && cached.zh){
    // 若当前是中文视图，切回英文
    if(cached.mode==="zh"){
      cached.mode="en";
      $("#captionBox").value=cached.en;
      state.savedCaption = $("#captionBox").value;
    }else{
      cached.mode="zh";
      $("#captionBox").value=cached.zh;
    }
    $("#captionBox").classList.remove("dirty");
    $("#capDirty").hidden=true;
    updateCapMeta();
    syncBiIndicator();
    return;
  }

  // 首次：翻译并切到中文
  const bi=await ensureBi(path);
  if(!bi) return;
  bi.mode="zh";
  $("#captionBox").value=bi.zh;
  $("#captionBox").classList.remove("dirty");
  $("#capDirty").hidden=true;
  updateCapMeta();
  syncBiIndicator();
});

/* 双语状态提示 */
function syncBiIndicator(){
  const box=$("#biState");
  if(!box) return;
  const bi=state.selected?biOf(state.selected):null;
  if(!bi||!bi.zh){ box.hidden=true; return; }
  box.hidden=false;
  box.dataset.mode=bi.mode;
  box.textContent = bi.mode==="zh" ? "中文（缓存）" : "English";
}

/* 编辑联动：在中文视图下改内容，英文同步增删 */
function onCaptionEdited(){
  const path=state.selected;
  if(!path) return;
  const bi=biOf(path);
  if(!bi||bi.mode!=="zh") return;

  const newZh=$("#captionBox").value;
  const res=syncEnFromZh(path,newZh);
  if(!res) return;
  bi.zh=newZh;
  bi.en=res.en;
  bi.segments=bi.segments.filter(sg=>res.en.includes(sg.en));

  if(res.removed>0) log("dim",`中文改动：同步删除 ${res.removed} 个英文词段`);
  if(res.added>0) log("warn",`中文新增 ${res.added} 段，英文无法自动对应，建议补译或改英文`);
}

$("#btnOpenImg").addEventListener("click",()=>{
  if(state.selected) window.open("/api/image?path="+encodeURIComponent(state.selected),"_blank");
});

/* ===================== 提示词预览 ===================== */
$("#btnShowPrompt").addEventListener("click",async()=>{
  const p=collectParams();
  const qs=new URLSearchParams({lora_type:p.lora_type,person_name:p.person_name,generic_subject:p.generic_subject,
    trigger_word:p.trigger_word,style_hint:p.style_hint,extra_instructions:p.extra_instructions});
  const r=await fetch("/api/template?"+qs); const d=await r.json();
  $("#promptTextArea").value=d.system_prompt||d.error||"";
  $("#modalOverlay").classList.add("show");
});
$("#btnCloseModal").addEventListener("click",()=>$("#modalOverlay").classList.remove("show"));
$("#modalOverlay").addEventListener("click",e=>{ if(e.target.id==="modalOverlay") $("#modalOverlay").classList.remove("show"); });
$("#btnCopyPrompt").addEventListener("click",()=>{ navigator.clipboard.writeText($("#promptTextArea").value); toast("已复制到剪贴板"); });
$("#btnCopyLog").addEventListener("click",()=>{ navigator.clipboard.writeText($("#logBox").innerText); toast("日志已复制到剪贴板"); });

/* ===================== 批处理 ===================== */
function setRunning(run){
  state.running=run;
  $("#btnStart").disabled=run; $("#btnStop").disabled=!run;
}
$("#btnStart").addEventListener("click",async()=>{
  const dir=$("#dirInput").value.trim();
  if(!dir){ toast("先选一个数据集目录"); return; }
  if(!$("#providerSel").value||!$("#modelSel").value){ toast("先选 Provider 和模型"); return; }
  if(state.type==="person"&&!$("#personName").value.trim()&&!$("#genericSubject").value.trim()&&!$("#triggerWord").value.trim()){
    toast("人物打标至少要填人名、泛化主体、触发词中的一个"); return;
  }
  const trial=$("#optTrial").checked;
  const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      directory:dir, lora_type:state.type,
      provider_id:$("#providerSel").value, model:$("#modelSel").value,
      person_name:$("#personName").value.trim(), generic_subject:$("#genericSubject").value.trim(),
      trigger_word:$("#triggerWord").value.trim(), trigger_position:$("#triggerPosition").value,
      style_hint:$("#styleHint").value.trim(), extra_instructions:$("#extraInstr").value.trim(),
      skip_existing:$("#optSkip").checked, concurrency:+$("#optConcurrency").value,
      recursive:$("#optRecursive").checked, limit:trial?2:0,
      shot_tags:$("#optShotTags").checked,
      folder_name:currentFolderName(), auto_rename:$("#optAutoRename").checked,
      upscale:false,
    })});
  const d=await r.json();
  if(d.error){ toast(d.error); log("err",d.error); return; }
  setRunning(true);
  if(trial){ log("warn","试跑模式：只处理前 2 张"); }
  // 拉取初始状态同步文件
  await syncState();
});
$("#btnStop").addEventListener("click",async()=>{
  await fetch("/api/stop",{method:"POST"});
  log("warn","已请求停止，等正在跑的图片结束");
});
$("#btnClear").addEventListener("click",async()=>{
  if(_mode==="upscale"){
    const r=await fetch("/api/upscale/clear",{method:"POST"});
    const d=await r.json();
    if(d.error){ toast(d.error); return; }
    setProgress(0);
    $("#statText").textContent="待命";
    $("#upStats").hidden=true;
    up.items=[]; up.tasks=[]; up.scanned=false; up.selected=null;
    up.picked.clear();
    renderUpGrid();
    toast("放大记录已清空");
    return;
  }
  if(state.running){ toast("先停止当前批次再清屏"); return; }
  await fetch("/api/clear",{method:"POST"});
  state.files=[]; state.selected=null;
  $("#previewBody").classList.remove("show"); $("#previewEmpty").hidden=false;
  $("#logBox").innerHTML=""; setProgress(0);
  $("#statText").textContent="待命"; $("#gridCount").textContent="";
  renderGrid(); renderQueueBadge();
  resetFlow();
  log("sys","已清屏，可以开始下一批");
});

async function syncState(){
  const r=await fetch("/api/state"); const d=await r.json();
  setRunning(d.running);
  const map=new Map((d.tasks||[]).map(t=>[t.path,t]));
  state.files.forEach(f=>{
    const t=map.get(f.path);
    if(t){ f.status=t.status; f.caption=t.caption; f.error=t.error; f.duration_ms=t.duration_ms; updateCard(f.path); }
  });
  updateProgress(d.stats);
}
function setProgress(pct){
  const bar=$("#progressBar");
  bar.style.width=pct+"%";
  bar.classList.toggle("finished",pct>=100);
  const wrap=$("#progressWrap");
  if(wrap) wrap.setAttribute("aria-valuenow",String(Math.round(pct)));
}
function updateProgress(st){
  if(!st||!st.total){ return; }
  const pct=Math.round((st.done+st.errors)/st.total*100);
  setProgress(pct);
  $("#statText").textContent = st.errors
    ? `${st.done+st.errors}/${st.total}，${st.errors} 张失败`
    : `${st.done+st.errors}/${st.total}`;
}

/* ===================== 质检面板 ===================== */
$("#btnAudit").addEventListener("click",async()=>{
  const dir=$("#dirInput").value.trim();
  if(!dir){ toast("先选一个数据集目录"); return; }
  const ov=$("#auditOverlay"); ov.classList.add("show");
  const body=$("#auditBody");
  body.innerHTML='<div class="empty-hint"><strong>正在检查 caption…</strong><span>图片多的时候会慢一些</span></div>';
  $("#auditSummary").textContent="";
  const r=await fetch("/api/audit",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({directory:dir,recursive:$("#optRecursive").checked,
      lora_type:state.type,trigger_word:$("#triggerWord").value.trim()})});
  const d=await r.json();
  if(d.error){ body.innerHTML=`<div class="empty-hint"><strong>质检没有跑起来</strong><span>${d.error}</span></div>`; return; }
  $("#auditSummary").textContent=`${d.audited}/${d.total_images} 张已打标 · ${d.issue_files} 张待处理`;
  if(!d.issues.length){
    body.innerHTML='<div class="empty-hint"><strong>全部通过</strong><span>这个目录的 caption 没有发现问题</span></div>';
    return;
  }
  body.innerHTML="";
  // 收集全部问题文件路径（供一键重打）
  const allBadPaths=[...new Set(d.issues.flatMap(g=>g.files.map(f=>f.path)))];
  window._auditBadPaths=allBadPaths;   // 供「一键重打全部」用
  const retagBtnAll=$("#btnAuditRetagAll");
  if(allBadPaths.length){
    retagBtnAll.hidden=false;
    retagBtnAll.textContent=`重打全部问题（${allBadPaths.length} 张）`;
  } else retagBtnAll.hidden=true;
  // 长度分布图
  if(d.length_dist&&d.length_dist.hist&&Object.keys(d.length_dist.hist).length){
    const wrap=document.createElement("div");
    wrap.className="lenDistWrap";
    const dist=document.createElement("div");
    dist.className="lenDist";
    const maxv=Math.max(...Object.values(d.length_dist.hist));
    const entries=Object.entries(d.length_dist.hist);
    const axes=document.createElement("div");
    axes.className="lenAxes";
    entries.forEach(([bucket,v])=>{
      const col=document.createElement("div");
      col.className="lenCol";
      col.title=`${bucket} 词：${v} 张`;
      const val=document.createElement("span");
      val.className="v"; val.textContent=v;
      const bar=document.createElement("div");
      bar.className="bar"; bar.style.height=Math.max(4,v/maxv*100)+"%";
      if(!v) bar.classList.add("zero");
      col.appendChild(val); col.appendChild(bar);
      dist.appendChild(col);
      const lb=document.createElement("span");
      lb.textContent=bucket.split("-")[0];
      axes.appendChild(lb);
    });
    const meta=document.createElement("div");
    meta.className="lenDistMeta";
    meta.innerHTML=`<span>词数分布 · 横轴=词数档 / 柱高=张数</span><span>min <b>${d.length_dist.min}</b></span><span>max <b>${d.length_dist.max}</b></span><span>平均 <b>${d.length_dist.avg}</b></span>`;
    wrap.appendChild(dist); wrap.appendChild(axes); wrap.appendChild(meta);
    body.appendChild(wrap);
  }
  d.issues.forEach(g=>{
    const grp=document.createElement("div");
    grp.className="auditGroup";
    const head=document.createElement("div");
    head.className="agHead";
    head.innerHTML=`<span class="arrow" aria-hidden="true">▶</span><span class="label">${g.label}</span><span class="cnt">${g.files.length}</span><button class="btn small danger">重打这组</button>`;
    head.addEventListener("click",e=>{
      if(e.target.tagName==="BUTTON") return;
      grp.classList.toggle("open");
    });
    grp.appendChild(head);
    // 该组重打按钮
    const grpBtn=head.querySelector("button");
    const groupPaths=g.files.map(f=>f.path);
    grpBtn.addEventListener("click",e=>{
      e.stopPropagation();
      runAuditRetag(groupPaths, grpBtn, g.label);
    });
    const files=document.createElement("div");
    files.className="auditFiles";
    g.files.forEach(f=>{
      const row=document.createElement("div");
      row.className="auditFile";
      const tag=f.style_words?("<span class='afTag'>"+f.style_words.join(", ")+"</span>"):"";
      row.innerHTML=`<span class="afName" title="${f.path}">${f.name}</span><span class="afCap">${f.caption||"<i>（无内容）</i>"}</span>${tag}`;
      row.querySelector(".afName").addEventListener("click",()=>{ selectFile(f.path); ov.classList.remove("show"); });
      files.appendChild(row);
    });
    grp.appendChild(files);
    body.appendChild(grp);
  });
});
$("#btnCloseAudit").addEventListener("click",()=>$("#auditOverlay").classList.remove("show"));
$("#auditOverlay").addEventListener("click",e=>{ if(e.target.id==="auditOverlay") e.target.classList.remove("show"); });

/* 质检一键重打 */
async function runAuditRetag(paths, btn, label){
  if(!$("#providerSel").value||!$("#modelSel").value){ toast("先在左侧选 Provider 和模型"); return; }
  const doRetag=async()=>{
    btn.disabled=true; const orig=btn.textContent; btn.textContent="⏳ 重打中…";
    try{
      const p=collectParams();
      const r=await fetch("/api/retag_many",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({paths, lora_type:p.lora_type, provider_id:p.provider_id, model:p.model,
          person_name:p.person_name, generic_subject:p.generic_subject, trigger_word:p.trigger_word,
          trigger_position:p.trigger_position, style_hint:p.style_hint, extra_instructions:p.extra_instructions,
          shot_tags:$("#optShotTags").checked, concurrency:+$("#optConcurrency").value})});
      const d=await r.json();
      if(d.error){ toast(d.error); return; }
      toast(`已提交重打 ${paths.length} 张，进度看主界面日志`);
      log("sys",`质检重打（${label||"全部"}）：${paths.length} 张已入批`);
      $("#auditOverlay").classList.remove("show");
    }finally{ btn.disabled=false; btn.textContent=orig; }
  };
  if(paths.length>10 && !confirm(`将重打 ${paths.length} 张（${label||"全部问题"}），覆盖现有 caption（自动备份 .bak）。继续？`)) return;
  await doRetag();
}
$("#btnAuditRetagAll").addEventListener("click",function(){
  runAuditRetag(window._auditBadPaths||[], this, "全部问题");
});

/* ===================== 批量替换 ===================== */
$("#btnReplace").addEventListener("click",()=>{
  $("#repDir").value=$("#dirInput").value.trim();
  $("#repResult").innerHTML=""; $("#btnRepApply").disabled=true;
  $("#replaceOverlay").classList.add("show");
});
$("#btnCloseReplace").addEventListener("click",()=>$("#replaceOverlay").classList.remove("show"));
$("#replaceOverlay").addEventListener("click",e=>{ if(e.target.id==="replaceOverlay") e.target.classList.remove("show"); });

async function runReplace(dry){
  const find=$("#repFind").value;
  if(!find.trim()){ toast("先填要查找的内容"); return; }
  const body={
    directory:$("#repDir").value.trim()||$("#dirInput").value.trim(),
    recursive:$("#optRecursive").checked,
    find, replace:$("#repWith").value,
    whole_word:$("#repWhole").checked, case_sensitive:$("#repCase").checked,
    dry_run:dry, keep_backup:$("#repBackup").checked,
  };
  const r=await fetch("/api/replace",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});
  const d=await r.json();
  if(d.error){ toast(d.error); return; }
  const box=$("#repResult");
  if(!d.matched){ box.innerHTML='<div class="empty-hint" style="padding:28px 12px"><strong>没有匹配的文件</strong><span>换一个查找词试试</span></div>'; return; }
  box.innerHTML=`<div style="font-size:12.5px;margin-bottom:8px;color:${dry?"var(--warn)":"var(--ok)"}">${dry?`预览：${d.matched} 个文件将被修改`:`已写入 ${d.written} 个文件（原文件备份 .bak）`}</div>`;
  d.hits.forEach(h=>{
    const div=document.createElement("div");
    div.className="repHit";
    div.innerHTML=`<div class="rhName">${h.name}（${h.count} 处）</div>
      <div class="rhBefore">${h.before}</div><div class="rhArrow">↓</div><div class="rhAfter">${h.after}</div>`;
    box.appendChild(div);
  });
  $("#btnRepApply").disabled=!dry;
  if(!dry){ log("ok",`批量替换完成：${d.written} 个文件`); $("#btnRepApply").disabled=true; }
  else log("info",`替换预览：${d.matched} 个文件命中「${find}」`);
}
$("#btnRepPreview").addEventListener("click",()=>runReplace(true));
$("#btnRepApply").addEventListener("click",()=>runReplace(false));

/* ===================== 多目录队列 ===================== */
function collectStartParams(extra={}){
  return Object.assign({
    directory:$("#dirInput").value.trim(),
    lora_type:state.type,
    provider_id:$("#providerSel").value, model:$("#modelSel").value,
    person_name:$("#personName").value.trim(), generic_subject:$("#genericSubject").value.trim(),
    trigger_word:$("#triggerWord").value.trim(), trigger_position:$("#triggerPosition").value,
    style_hint:$("#styleHint").value.trim(), extra_instructions:$("#extraInstr").value.trim(),
    skip_existing:$("#optSkip").checked, concurrency:+$("#optConcurrency").value,
    recursive:$("#optRecursive").checked, limit:$("#optTrial").checked?2:0,
    shot_tags:$("#optShotTags").checked,
    folder_name:currentFolderName(), auto_rename:$("#optAutoRename").checked,
    upscale:false,
  },extra);
}
$("#btnQueueAdd").addEventListener("click",async()=>{
  const dir=$("#dirInput").value.trim();
  if(!dir){ toast("先选一个数据集目录"); return; }
  const r=await fetch("/api/queue/add",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(collectStartParams())});
  const d=await r.json();
  if(d.error){ toast(d.error); return; }
  toast(`已入队（第 ${d.position} 位）`);
  log("info",`目录入队: ${dir}`);
  renderQueueBadge(); loadQueueIfOpen();
});
$("#btnQueueAdd").addEventListener("contextmenu",e=>{ e.preventDefault(); $("#queueOverlay").classList.add("show"); loadQueue(); });
$("#btnQueueAdd").addEventListener("dblclick",()=>{ $("#queueOverlay").classList.add("show"); loadQueue(); });
$("#btnQueueAdd").title="单击：当前目录入队 · 双击/右键：打开队列面板";
$("#btnClear").addEventListener("click",async()=>{ renderQueueBadge(); });

async function loadQueue(){
  const r=await fetch("/api/queue"); const d=await r.json();
  const list=$("#queueList"); list.innerHTML="";
  // 摘要
  const totalImgs=(d.queue||[]).reduce((s,q)=>s+(q.image_count||0),0);
  $("#queueSummary").textContent=d.queue.length?`${d.queue.length} 个目录 · ${totalImgs} 张图`:"";
  // 当前批
  const skipBtn=$("#btnQueueSkipCurrent");
  if(d.running){
    skipBtn.hidden=false;
    const cur=document.createElement("div");
    cur.className="qItem"; cur.style.borderColor="var(--warn)";
    const prog=d.current_stats?`${d.current_stats.done+d.current_stats.errors}/${d.current_stats.total}`:"";
    cur.innerHTML=`<span class="qRun">▶ 处理中 ${prog}</span><span class="qDirWrap"><span class="qDir">${d.current}</span></span>`;
    list.appendChild(cur);
  } else skipBtn.hidden=true;
  if(!d.queue.length&&!d.running){ list.innerHTML='<div class="empty-hint" style="padding:26px 12px"><strong>队列是空的</strong><span>在上方每行填一个目录，或用顶栏的「排队」加入当前目录</span></div>'; }
  d.queue.forEach((q,i)=>{
    const it=document.createElement("div");
    it.className="qItem";
    const metas=[];
    if(q.person_name) metas.push(`名:${q.person_name}`);
    if(q.trigger_word) metas.push(`触发:${q.trigger_word}`);
    if(q.folder_name) metas.push(`文件夹:${q.folder_name}`);
    metas.push(`${q.image_count}张`);
    if(q.skip_existing) metas.push("续跑");
    it.innerHTML=`
      <span class="qOrder"><button data-mv="up" title="上移">▲</button><button data-mv="down" title="下移">▼</button></span>
      <span class="qDirWrap">
        <span class="qDir">#${i+1} ${q.directory}</span>
        <span class="qMeta">${q.lora_type} · ${q.model||""}${metas.length?" · "+metas.join(" · "):""}</span>
      </span>
      <span class="qOrder"><button data-mv="top" title="置顶">⤒</button><button data-mv="bottom" title="置底">⤓</button></span>
      <button class="btn small danger" data-rm="1">移除</button>`;
    it.querySelectorAll("[data-mv]").forEach(b=>b.addEventListener("click",async()=>{
      await fetch("/api/queue/move",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({directory:q.directory,direction:b.dataset.mv})});
      loadQueue(); renderQueueBadge();
    }));
    it.querySelector("[data-rm]").addEventListener("click",async()=>{
      await fetch("/api/queue/remove",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({directory:q.directory})});
      loadQueue(); renderQueueBadge();
    });
    list.appendChild(it);
  });
}
$("#btnQueueSkipCurrent").addEventListener("click",async()=>{
  const r=await fetch("/api/queue/skip_current",{method:"POST"});
  const d=await r.json();
  if(d.error){ toast(d.error); return; }
  toast("已请求跳过，等当前图片跑完就切下一个");
  log("warn","队列：请求跳过当前批次");
});
$("#btnQueueAddMany").addEventListener("click",async()=>{
  const dirs=$("#queueDirs").value.split("\n").map(s=>s.trim()).filter(Boolean);
  if(!dirs.length){ toast("先填目录，每行一个"); return; }
  const params=collectStartParams({directory:dirs[0]});
  const r=await fetch("/api/queue/add_many",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({directories:dirs,params})});
  const d=await r.json();
  toast(`入队 ${d.added.length} 个${d.skipped_dup.length?`，已在队列跳过 ${d.skipped_dup.length} 个`:""}${d.errors.length?`，失败 ${d.errors.length} 个`:""}`);
  log("info",`批量入队 ${d.added.length} 个目录`);
  $("#queueDirs").value="";
  loadQueue(); renderQueueBadge();
});
$("#btnQueueClear").addEventListener("click",async()=>{
  await fetch("/api/queue/clear",{method:"POST"});
  loadQueue(); renderQueueBadge(); toast("队列已清空");
});
$("#btnCloseQueue").addEventListener("click",()=>$("#queueOverlay").classList.remove("show"));
$("#queueOverlay").addEventListener("click",e=>{ if(e.target.id==="queueOverlay") e.target.classList.remove("show"); });
function renderQueueBadge(){
  fetch("/api/queue").then(r=>r.json()).then(d=>{
    const btn=$("#btnQueueAdd");
    btn.textContent=d.queue.length?`排队 ${d.queue.length}`:"排队";
  });
}
function loadQueueIfOpen(){
  if($("#queueOverlay").classList.contains("show")) loadQueue();
  // 队列推进时刷新网格为下一批
  fetch("/api/queue").then(r=>r.json()).then(d=>{
    if(d.current&&d.current!==$("#dirInput").value.trim()){
      $("#dirInput").value=d.current;
      scanDir();
    }
  });
}

/* ===================== SSE ===================== */
let _editingCaption=false;
$("#captionBox").addEventListener("focus",()=>_editingCaption=true);
$("#captionBox").addEventListener("blur",()=>{ _editingCaption=false; });
const es=new EventSource("/api/events");
es.onmessage=ev=>{
  const m=JSON.parse(ev.data);
  switch(m.type){
    case "batch_start":
      log("sys",`批次开始：${m.total} 张，待处理 ${m.pending}，跳过 ${m.total-m.pending}`);
      break;
    case "task_stage":{
      // stage=upscale：卡片进入放大中状态
      if(m.stage==="upscale"){
        const f=state.files.find(x=>x.path===m.path);
        if(f){ f.status="upscaling"; updateCard(m.path); }
        log("info",`${m.name} 放大中…`);
        setFlowStep("up","active");
      }else{
        setFlowStep("up","done");
        setFlowStep("cap","active");
      }
      break;}
    case "task_upscaled":{
      const f=state.files.find(x=>x.path===m.path);
      if(f){
        f.upscaled=true;
        if(m.meta&&m.meta.to) { f.w=m.meta.to[0]; f.h=m.meta.to[1]; }
        updateCard(m.path);
      }
      const to=m.meta&&m.meta.to?`${m.meta.to[0]}×${m.meta.to[1]}`:"";
      log("ok",`${m.name} 放大完成 ${to}`);
      // 正在看这张图：立刻切成对比视图
      if(state.selected===m.path) cmpSetup(m.path, true);
      break;}
    case "task_start":{
      const f=state.files.find(x=>x.path===m.path);
      if(f){ f.status="running"; updateCard(m.path); }
      log("dim",`${m.name}`);
      break;}
    case "task_done":{
      const f=state.files.find(x=>x.path===m.path);
      if(f){ f.status="done"; f.caption=m.caption; f.duration_ms=m.duration_ms; f.error=""; updateCard(m.path); }
      const extras=[]; if(m.backup) extras.push("备份");
      if(m.meta&&m.meta.style_words_removed) extras.push(`剔风格词${m.meta.style_words_removed}`);
      if(m.meta&&m.meta.body_words_removed) extras.push(`剔体型词${m.meta.body_words_removed}`);
      if(m.meta&&m.meta.shot_tag) extras.push("补构图词");
      log("ok",`${m.name}（${(m.duration_ms/1000).toFixed(1)}s${extras.length?" ·"+extras.join("·"):""}）`);
      // 正在编辑时不覆盖输入框（编辑保护）
      if(state.selected===m.path&&!_editingCaption){
        $("#captionBox").value=m.caption;
        state.savedCaption=m.caption;
        $("#capDirty").hidden=true;
        $("#captionBox").classList.remove("dirty");
        updateCapMeta();
        $("#pvStatus").innerHTML=`<span class="pvState st-ok">状态 <b>已完成</b></span><span><b>${(m.duration_ms/1000).toFixed(1)}s</b> 耗时</span>`;
      }
      break;}
    case "task_error":{
      const f=state.files.find(x=>x.path===m.path);
      if(f){ f.status="error"; f.error=m.error; f.duration_ms=m.duration_ms; updateCard(m.path); }
      log("err",`${m.name} 失败：${m.error}`);
      break;}
    case "batch_done":{
      setRunning(false);
      const s=m.stats;
      log(m.cancelled?"warn":"sys",
        `批次结束：成功 ${s.done}/${s.total} · 失败 ${s.errors} · 总耗时 ${s.elapsed_s}s · 平均 ${s.avg_s}s/张 · 成功率 ${s.success_rate}%`);
      setProgress(100);
      setFlowStep("up","done"); setFlowStep("cap","done");
      $("#statText").textContent=`完成 ${s.done}/${s.total} · 成功率 ${s.success_rate}% · ${s.elapsed_s}s`;
      if(m.error) log("err","批处理异常: "+m.error);
      // 试跑跑完自动取消勾选，防忘
      if($("#optTrial").checked){ $("#optTrial").checked=false; log("dim","试跑模式已关闭"); }
      // 完成横幅（有失败时警示色）
      const banner=document.createElement("div");
      banner.className="doneBanner"+(s.errors?" warn":"");
      banner.textContent=s.errors
        ?`批次完成：${s.done} 成功 / ${s.errors} 失败（点失败文件可看错误，单张重打可重试）`
        :`✅ 批次完成：${s.done}/${s.total} · 平均 ${s.avg_s}s/张`;
      const grid=$("#fileGrid");
      grid.parentNode.insertBefore(banner,grid);
      setTimeout(()=>banner.remove(),8000);
      renderQueueBadge();
      break;}
    case "queue_advance":
      setRunning(true);
      log("sys",`队列推进 → ${m.directory}（${m.total} 张，剩 ${m.remaining} 个目录）`);
      toast(`队列下一个：${m.directory.split("\\").pop()}`);
      renderQueueBadge(); loadQueueIfOpen();
      break;
    case "queue_skip":
      log("err",`队列跳过 ${m.directory}: ${m.error}`);
      renderQueueBadge();
      break;
    case "renamed":{
      // 文件夹已重命名：更新目录框并提示重新扫描
      $("#dirInput").value=m.new_path;
      state.files=[]; state.selected=null;
      $("#previewBody").classList.remove("show"); $("#previewEmpty").hidden=false;
      log("ok",`文件夹已自动改名: ${m.old_path.split("\\").pop()} → ${m.target}`);
      toast(`文件夹已改名 → ${m.target}`);
      const oldDir=m.old_path;
      scanDir();
      break;}
    case "rename_failed":
      log("err","自动改名失败: "+m.error);
      break;
  }
};
es.onerror=()=>{ /* SSE 断线自动重连 */ };

/* ===================== 记住配置 ===================== */
async function restoreConfig(){
  try{
    const r=await fetch("/api/config"); state.lastCfg=await r.json();
    const c=state.lastCfg;
    if(c.directory) $("#dirInput").value=c.directory;
    if(c.person_name) $("#personName").value=c.person_name;
    if(c.generic_subject) $("#genericSubject").value=c.generic_subject;
    if(c.trigger_word) $("#triggerWord").value=c.trigger_word;
    if(c.trigger_position) $("#triggerPosition").value=c.trigger_position;
    if(c.style_hint) $("#styleHint").value=c.style_hint;
    if(c.extra_instructions) $("#extraInstr").value=c.extra_instructions;
    if(c.skip_existing!==undefined) $("#optSkip").checked=c.skip_existing;
    if(c.shot_tags!==undefined) $("#optShotTags").checked=c.shot_tags;
    if(c.auto_rename!==undefined) $("#optAutoRename").checked=c.auto_rename;
    if(c.concurrency) { $("#optConcurrency").value=c.concurrency; $("#concVal").textContent=c.concurrency; }
    if(c.recursive!==undefined) $("#optRecursive").checked=c.recursive;
    if(c.lora_type){
      const card=document.querySelector(`.typeCard[data-type="${c.lora_type}"]`);
      if(card) card.click();
    }
  }catch(e){}
}
$("#optConcurrency").addEventListener("input",()=>$("#concVal").textContent=$("#optConcurrency").value);

/* ===================== 日志折叠 + 拖拽调高 ===================== */
$("#logPanel .loghead").addEventListener("click",e=>{
  if(e.target.id==="btnCopyLog") return;
  $("#logPanel").classList.toggle("collapsed");
});
$("#logPanel .loghead").title="点击折叠/展开日志";
(function(){
  const drag=$("#logDrag"), panel=$("#logPanel"), center=$("#centerPanel");
  let startY=0, startH=0;
  drag.addEventListener("mousedown",e=>{
    e.preventDefault();
    startY=e.clientY; startH=panel.getBoundingClientRect().height;
    document.body.style.cursor="ns-resize";
    document.body.style.userSelect="none";
    const move=ev=>{
      const centerRect=center.getBoundingClientRect();
      const delta=ev.clientY-startY;
      const maxH=centerRect.height-160;   // 网格区至少留 160px
      const h=Math.max(34,Math.min(maxH,startH-delta));
      panel.style.height=h+"px";
      if(h<=34) panel.classList.add("collapsed"); else panel.classList.remove("collapsed");
    };
    const up=()=>{
      document.removeEventListener("mousemove",move);
      document.removeEventListener("mouseup",up);
      document.body.style.cursor=""; document.body.style.userSelect="";
    };
    document.addEventListener("mousemove",move);
    document.addEventListener("mouseup",up);
  });
})();

/* ===================== 缩略图大小切换 ===================== */
/* 只改列宽（缩略图大小），图片始终按原图比例完整显示，不做裁切 */
$$(".arBtn").forEach(b=>b.addEventListener("click",()=>{
  $$(".arBtn").forEach(x=>{
    const on=x===b;
    x.classList.toggle("active",on);
    x.setAttribute("aria-pressed",on?"true":"false");
  });
  const ar=b.dataset.ar;
  const grid=$("#fileGrid");
  grid.classList.remove("ar-1-1","ar-4-3","ar-3-4","ar-16-9","ar-9-16","ar-auto");
  if(ar!=="auto") grid.classList.add("ar-"+ar);
  window._curAR=ar;
  try{ localStorage.setItem("captioner_ar",ar); }catch(e){}
  layoutMasonry();
}));

/* 窗口尺寸变化时重排（去抖） */
let _rsT=null;
window.addEventListener("resize",()=>{
  clearTimeout(_rsT);
  _rsT=setTimeout(()=>{ layoutMasonry(); upLayout(); },120);
});

/* ===================== 文件夹命名 ===================== */
function typeNameField(){
  return {person:"personName",clothing:"clothingName",style:"styleName",body:"bodyName"}[state.type]||"personName";
}
function currentFolderName(){
  return $("#"+typeNameField()).value.trim()||$("#renameName").value.trim();
}
// 类型名输入 → 同步到改名框；改名框输入 → 回写类型名（双向，以最后编辑的为准）
function bindNameSync(srcId){
  $("#"+srcId).addEventListener("input",()=>{ $("#renameName").value=$("#"+srcId).value; });
}
["personName","clothingName","styleName","bodyName"].forEach(bindNameSync);
$("#renameName").addEventListener("input",()=>{
  const t=$("#"+typeNameField());
  if(t && document.activeElement!==t) t.value=$("#renameName").value;
  syncNamingBadge();
});
function syncNamingBadge(){
  const badge=document.querySelector('section[data-group="naming"] [data-role="badge"]');
  if(!badge) return;
  const auto=$("#optAutoRename").checked;
  const name=currentFolderName();
  badge.textContent = auto ? (name?`自动 ${name}`:"自动") : "手动";
}
$("#optAutoRename").addEventListener("change",syncNamingBadge);
$("#btnRenameNow").addEventListener("click",async()=>{
  const dir=$("#dirInput").value.trim();
  if(!dir){ toast("先选一个数据集目录"); return; }
  const name=currentFolderName();
  // 先预览
  const pv=await (await fetch(`/api/rename/preview?directory=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}`)).json();
  if(pv.error){ toast(pv.error); return; }
  if(pv.unchanged){ toast(`目录已经是 ${pv.target}，无需改名`); return; }
  if(!confirm(`重命名文件夹：\n  ${pv.current}\n→ ${pv.target}${pv.clash?"\n⚠ 同级已有同名，将自动加后缀":""}\n\n继续？`)) return;
  const r=await fetch("/api/rename/execute",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({directory:dir,name})});
  const d=await r.json();
  if(d.error){ toast(d.error); return; }
  if(d.unchanged){ toast("目录名已经符合规则，不用改"); return; }
  $("#dirInput").value=d.new_path;
  state.files=[];
  log("ok",`文件夹已改名: ${d.old_path.split("\\").pop()} → ${d.target}（路径已失效，任务已清，请重新扫描）`);
  toast(`已改名 → ${d.target}`);
  await scanDir();
});

/* ===================== 键盘导航 ===================== */
document.addEventListener("keydown",e=>{
  const tag=(e.target.tagName||"").toLowerCase();
  const typing = tag==="input"||tag==="textarea"||tag==="select";

  // Ctrl/Cmd+S：保存当前 caption（输入框内也生效）
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){
    e.preventDefault();
    if(state.selected) $("#btnSaveCap").click();
    return;
  }
  // Ctrl/Cmd+O：打开目录浏览
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="o"){
    e.preventDefault();
    openBrowser();
    return;
  }
  // Esc：只关弹窗，不误伤其他状态
  // 放大模式：Ctrl+A 全选当前筛选
  if(_mode==="upscale" && (e.ctrlKey||e.metaKey) && e.key.toLowerCase()==="a"){
    const tag2=(e.target.tagName||"").toLowerCase();
    if(tag2!=="input" && tag2!=="textarea"){
      e.preventDefault(); upSelAll(); return;
    }
  }
  if(e.key==="Escape"){
    let closed=false;
    ["#modalOverlay","#auditOverlay","#replaceOverlay","#queueOverlay"].forEach(sel=>{
      const el=document.querySelector(sel);
      if(el&&el.classList.contains("show")){ el.classList.remove("show"); closed=true; }
    });
    const db=$("#dirBrowser");
    if(db&&db.style.display==="block"){ db.style.display="none"; closed=true; }
    if(closed) return;
  }
  if(typing||!state.selected) return;
  if(e.key==="ArrowLeft"||e.key==="ArrowRight"){
    e.preventDefault();
    const vis=state.files.filter(statusOf);
    const idx=vis.findIndex(f=>f.path===state.selected);
    let ni=idx+(e.key==="ArrowRight"?1:-1);
    if(ni<0) ni=vis.length-1; if(ni>=vis.length) ni=0;
    if(vis[ni]) selectFile(vis[ni].path);
  }
});

/* ===================== 流程指示 ===================== */
function setFlowStep(step, stateName){
  const el=document.querySelector(`.flowStep[data-step="${step}"]`);
  if(!el) return;
  el.classList.remove("active","done");
  if(stateName) el.classList.add(stateName);
}
function resetFlow(){
  ["up","cap"].forEach(k=>setFlowStep(k,null));
}

/* ===================== 分组折叠 ===================== */
$$(".groupHead").forEach(h=>{
  h.addEventListener("click",()=>{
    const grp=h.closest(".group");
    const open=!grp.classList.contains("open");
    grp.classList.toggle("open",open);
    h.setAttribute("aria-expanded",open?"true":"false");
    try{ localStorage.setItem("captioner_g_"+grp.dataset.group,open?"1":"0"); }catch(e){}
  });
});
// 恢复分组展开状态
$$(".group").forEach(grp=>{
  let v=null;
  try{ v=localStorage.getItem("captioner_g_"+grp.dataset.group); }catch(e){}
  if(v!==null){
    const open=v==="1";
    grp.classList.toggle("open",open);
    const h=grp.querySelector(".groupHead");
    if(h) h.setAttribute("aria-expanded",open?"true":"false");
  }
});

/* ===================== 侧栏开关 ===================== */
function togglePanel(id,btn,silent){
  const el=$("#"+id);
  const collapsed=el.classList.toggle("collapsed");
  btn.setAttribute("aria-expanded",collapsed?"false":"true");
  btn.setAttribute("aria-label",(collapsed?"展开":"收起")+(id==="leftPanel"?"配置面板":"预览面板"));
  if(!silent){ try{ localStorage.setItem("captioner_"+id,collapsed?"1":"0"); }catch(e){} }
}
// 恢复上次的折叠状态
try{
  [["leftPanel","#btnToggleLeft"],["rightPanel","#btnToggleRight"]].forEach(([id,sel])=>{
    if(localStorage.getItem("captioner_"+id)==="1") togglePanel(id,$(sel),true);
  });
}catch(e){}
$("#btnToggleLeft").addEventListener("click",e=>togglePanel("leftPanel",e.currentTarget));
$("#btnToggleRight").addEventListener("click",e=>togglePanel("rightPanel",e.currentTarget));

/* ===================== 徽章同步 ===================== */
function syncEngineBadge(){
  const badge=document.querySelector('section[data-group="engine"] [data-role="badge"]');
  if(!badge) return;
  const pid=$("#providerSel").value, mid=$("#modelSel").value;
  badge.textContent = mid || pid || "未选择";
  badge.title = `${pid} / ${mid}`;
}
function syncDirBadge(count){
  const badge=document.querySelector('section[data-group="dir"] [data-role="badge"]');
  if(!badge) return;
  const dir=$("#dirInput").value.trim();
  const leaf = dir ? dir.split(/[\\/]/).filter(Boolean).pop() : "";
  badge.textContent = dir ? (count!=null ? `${leaf} · ${count} 张` : leaf) : "未选择";
  badge.title = dir || "未选择目录";
}
$("#dirInput").addEventListener("input",()=>{
  syncDirBadge();
  if(_mode==="upscale"){ up.scanned=false; up.items=[]; renderUpGrid(); }
});
$("#modelSel").addEventListener("change",syncEngineBadge);
$("#providerSel").addEventListener("change",syncEngineBadge);

/* ===================== 保存保护 ===================== */
window.addEventListener("beforeunload",e=>{
  if(!state.savedCaption) return;
  if(($("#captionBox").value||"")===(state.savedCaption||"")) return;
  e.preventDefault(); e.returnValue="";
});

$("#captionBox").addEventListener("input",()=>{
  updateCapMeta();
  onCaptionEdited();
});

/* 触发词徽章 */
function syncTriggerBadge(){
  const badge=document.querySelector('section[data-group="trigger"] [data-role="badge"]');
  if(!badge) return;
  const w=$("#triggerWord").value.trim();
  badge.textContent=w||"缺省";
  badge.title=w?`触发词：${w}`:"没有触发词";
}
$("#triggerWord").addEventListener("input",syncTriggerBadge);

/* ===================== 窄屏抽屉遮罩 ===================== */
(function(){
  const bd=document.createElement("div");
  bd.id="drawerBackdrop";
  bd.className="drawerBackdrop";
  bd.hidden=true;
  document.body.appendChild(bd);
  function syncBackdrop(){
    const narrow=window.matchMedia("(max-width:1100px)").matches;
    const leftOpen=narrow&&!$("#leftPanel").classList.contains("collapsed");
    const rightOpen=narrow&&!$("#rightPanel").classList.contains("collapsed");
    bd.hidden=!(leftOpen||rightOpen);
  }
  bd.addEventListener("click",()=>{
    if(window.matchMedia("(max-width:1100px)").matches){
      if(!$("#leftPanel").classList.contains("collapsed")) togglePanel("leftPanel",$("#btnToggleLeft"));
      if(!$("#rightPanel").classList.contains("collapsed")) togglePanel("rightPanel",$("#btnToggleRight"));
    }
    syncBackdrop();
  });
  $("#btnToggleLeft").addEventListener("click",syncBackdrop);
  $("#btnToggleRight").addEventListener("click",syncBackdrop);
  window.addEventListener("resize",syncBackdrop);
  syncBackdrop();
})();

/* ===================== 启动 ===================== */
(async function init(){
  log("sys","LoRA Captioner 就绪");
  // 恢复上次的比例选择
  try{
    const savedAR=localStorage.getItem("captioner_ar");
    if(savedAR){
      const btn=document.querySelector(`.arBtn[data-ar="${savedAR}"]`);
      if(btn) btn.click();
    }
  }catch(e){}
  await restoreConfig();
  await loadProviders();
  await loadUpscaleStatus();
  setMode("caption", true);
  applyType(state.type);
  syncDirBadge(); syncEngineBadge(); syncTriggerBadge();
  await loadUpscaleStatus();
  syncUpscaleUI();
  updateCapMeta();
  if($("#dirInput").value) await scanDir();
  renderQueueBadge();
})();