const $ = (selector) => document.querySelector(selector);
const state = { file: null, jobs: JSON.parse(localStorage.getItem("tg-jobs") || "[]"), timers: new Map() };
const terminal = new Set(["succeeded", "failed", "deduplicated", "discarded", "conflict"]);
const statusLabel = { queued:"排队中", running:"处理中", succeeded:"已完成", failed:"失败", deduplicated:"已去重", conflict:"待处理", discarded:"已丢弃" };
const stepLabel = { validate:"文件校验", extract:"解析文本", dedup:"内容去重", conflict_check:"冲突检查", commit_artifacts:"保存产物", chunk:"文本分块", embed:"生成向量", index:"写入索引", publish:"发布文档" };

function toast(message, error=false){ const el=document.createElement("div"); el.className=`toast${error?" error":""}`; el.textContent=message; $("#toasts").append(el); setTimeout(()=>el.remove(),4500); }
async function api(path, options={}){ const response=await fetch(path,options); if(!response.ok){ let detail=`HTTP ${response.status}`; try{detail=(await response.json()).detail||detail}catch{} throw new Error(detail); } return response.json(); }
function persist(){ localStorage.setItem("tg-jobs",JSON.stringify(state.jobs.slice(0,30))); }
function formatTime(value){ if(!value)return "—"; return new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(value)); }
function escapeHtml(value=""){ const div=document.createElement("div"); div.textContent=value; return div.innerHTML; }

function renderJobs(){
  const list=$("#jobs-list"), empty=$("#jobs-empty"); list.innerHTML=""; empty.hidden=state.jobs.length>0;
  state.jobs.forEach(job=>{
    const row=document.createElement("div"); row.className="job-row";
    row.innerHTML=`<span class="job-icon">▧</span><div class="job-name"><strong>${escapeHtml(job.filename||"PDF 文档")}</strong><small>${escapeHtml(job.id)}</small></div><span class="step">${escapeHtml(stepLabel[job.current_step]||job.current_step||"等待处理")}</span><span class="status ${job.status}">${statusLabel[job.status]||job.status}</span><button class="job-action">${job.document_id?"查看":"刷新"}</button>`;
    row.querySelector("button").onclick=()=>job.document_id?openDocument(job.document_id):refreshJob(job.id);
    list.append(row);
  });
  $("#metric-jobs").textContent=state.jobs.length;
  $("#metric-success").textContent=state.jobs.filter(j=>["succeeded","deduplicated"].includes(j.status)).length;
}

async function refreshHealth(){
  try{
    const data=await api("/health"); const ok=data.status==="ok";
    $("#metric-health").textContent=ok?"运行正常":"服务降级"; $("#sidebar-status").textContent=ok?"服务在线":"服务降级"; $("#sidebar-dot").classList.toggle("ok",ok);
    $("#metric-qdrant").textContent=data.dependencies.qdrant?.status||"—";
    const grid=$("#health-grid"); grid.innerHTML="";
    Object.entries(data.dependencies).forEach(([name,dep])=>{ const card=document.createElement("div"); card.className="health-card"; card.innerHTML=`<div class="health-card-top"><h3>${escapeHtml(name)}</h3><i class="health-dot ${dep.status}"></i></div><p>${dep.latency_ms!=null?`${dep.latency_ms} ms`:escapeHtml(dep.detail||dep.status)}</p>`; grid.append(card); });
  }catch(error){ $("#metric-health").textContent="连接失败"; $("#sidebar-status").textContent="服务离线"; toast(`健康检查失败：${error.message}`,true); }
}

async function upload(){
  if(!state.file)return; const button=$("#upload-button"); button.disabled=true; button.querySelector("span").textContent="正在上传…";
  try{
    const form=new FormData(); form.append("source_type","file"); form.append("file",state.file);
    const result=await api("/v1/ingest/jobs",{method:"POST",body:form});
    state.jobs.unshift({id:result.job_id,status:result.status,filename:state.file.name,created_at:new Date().toISOString()}); persist(); renderJobs(); pollJob(result.job_id); toast("文件已提交，正在解析入库");
    state.file=null; $("#selected-file").hidden=true; $("#file-input").value="";
  }catch(error){toast(`上传失败：${error.message}`,true)}finally{button.disabled=!state.file; button.querySelector("span").textContent="开始解析入库";}
}
async function refreshJob(id){
  try{ const data=await api(`/v1/ingest/jobs/${id}`); const old=state.jobs.find(j=>j.id===id)||{}; Object.assign(old,data,{filename:old.filename}); if(!state.jobs.includes(old))state.jobs.unshift(old); persist(); renderJobs(); if(data.status==="failed")toast(`${old.filename||"文档"}：${data.error_message||"入库失败"}`,true); return data; }catch(error){toast(`任务刷新失败：${error.message}`,true)}
}
function pollJob(id){ if(state.timers.has(id))return; const tick=async()=>{const job=await refreshJob(id); if(!job||terminal.has(job.status)){clearInterval(state.timers.get(id));state.timers.delete(id);return;} }; tick(); state.timers.set(id,setInterval(tick,1800)); }

async function openDocument(id){
  switchView("documents"); $("#document-id").value=id; const target=$("#document-result"); target.innerHTML='<div class="loading">正在读取文档与分块…</div>';
  try{
    const [doc,chunks,artifacts]=await Promise.all([api(`/v1/documents/${id}`),api(`/v1/documents/${id}/chunks`),api(`/v1/documents/${id}/artifacts`)]);
    target.innerHTML=`<div class="doc-card"><div class="panel-header"><div><p class="kicker">${escapeHtml(doc.status)}</p><h2>${escapeHtml(doc.original_filename||"未命名文档")}</h2></div><span class="format-tag">${chunks.length} CHUNKS</span></div><div class="doc-meta"><div class="meta-box"><span>文档 ID</span><strong>${escapeHtml(doc.id)}</strong></div><div class="meta-box"><span>页数</span><strong>${doc.metadata?.page_count??"—"}</strong></div><div class="meta-box"><span>产物文件</span><strong>${artifacts.files.length}</strong></div></div><div class="chunks">${chunks.map(c=>`<article class="chunk"><div class="chunk-head"><span>CHUNK ${c.chunk_index+1}</span><span>PAGE ${c.page_no??"—"} · ${c.token_count} TOKENS</span></div><p>${escapeHtml(c.text)}</p></article>`).join("")}</div></div>`;
  }catch(error){target.innerHTML="";toast(`文档查询失败：${error.message}`,true)}
}

function switchView(name){ document.querySelectorAll(".view").forEach(v=>v.classList.toggle("active",v.id===`view-${name}`)); document.querySelectorAll(".nav-item").forEach(v=>v.classList.toggle("active",v.dataset.view===name)); $("#page-title").textContent={workspace:"知识工作台",documents:"文档检查器",system:"系统状态"}[name]; }
function chooseFile(file){ if(!file)return; if(file.type!=="application/pdf"&&!file.name.toLowerCase().endsWith(".pdf")){toast("请选择 PDF 文件",true);return;} if(file.size>50*1024*1024){toast("文件不能超过 50 MB",true);return;} state.file=file; const selected=$("#selected-file"); selected.hidden=false; selected.textContent=`${file.name} · ${(file.size/1024/1024).toFixed(2)} MB`; $("#upload-button").disabled=false; }

document.querySelectorAll(".nav-item").forEach(button=>button.onclick=()=>switchView(button.dataset.view));
$("#dropzone").onclick=()=>$("#file-input").click(); $("#dropzone").onkeydown=e=>{if(["Enter"," "].includes(e.key))$("#file-input").click()};
$("#file-input").onchange=e=>chooseFile(e.target.files[0]);
for(const event of ["dragenter","dragover"]){$("#dropzone").addEventListener(event,e=>{e.preventDefault();$("#dropzone").classList.add("dragging")})}
for(const event of ["dragleave","drop"]){$("#dropzone").addEventListener(event,e=>{e.preventDefault();$("#dropzone").classList.remove("dragging")})}
$("#dropzone").addEventListener("drop",e=>chooseFile(e.dataTransfer.files[0]));
$("#upload-button").onclick=upload; $("#refresh-all").onclick=()=>{refreshHealth();state.jobs.forEach(j=>!terminal.has(j.status)&&refreshJob(j.id))}; $("#refresh-health").onclick=refreshHealth;
$("#clear-history").onclick=()=>{state.jobs=[];persist();renderJobs()};
$("#document-form").onsubmit=e=>{e.preventDefault();const id=$("#document-id").value.trim();if(id)openDocument(id)};
setInterval(()=>$("#clock").textContent=new Date().toLocaleString("zh-CN",{hour12:false}),1000);
renderJobs(); refreshHealth(); state.jobs.filter(j=>!terminal.has(j.status)).forEach(j=>pollJob(j.id));
