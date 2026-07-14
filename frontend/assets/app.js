const $ = (selector) => document.querySelector(selector);
const state = { file: null, jobs: JSON.parse(localStorage.getItem("tg-jobs") || "[]"), timers: new Map(), documents: [], documentOffset: 0, documentLimit: 10, documentTotal: 0 };
const terminal = new Set(["succeeded", "failed", "deduplicated", "discarded", "conflict"]);
const deletableDocumentStatuses = new Set(["ready", "failed", "deleting", "superseded"]);
const statusLabel = { queued:"排队中", running:"处理中", succeeded:"已完成", failed:"失败", deduplicated:"已去重", conflict:"待处理", discarded:"已丢弃" };
const stepLabel = { validate:"文件校验", extract:"解析文本", dedup:"内容去重", conflict_check:"冲突检查", commit_artifacts:"保存产物", chunk:"文本分块", embed:"生成向量", index:"写入索引", publish:"发布文档" };

function toast(message, error=false){ const el=document.createElement("div"); el.className=`toast${error?" error":""}`; el.textContent=message; $("#toasts").append(el); setTimeout(()=>el.remove(),4500); }
async function api(path, options={}){ const response=await fetch(path,options); if(!response.ok){ let detail=`HTTP ${response.status}`; try{detail=(await response.json()).detail||detail}catch{} throw new Error(detail); } return response.status===204?null:response.json(); }
function persist(){ localStorage.setItem("tg-jobs",JSON.stringify(state.jobs.slice(0,30))); }
function formatTime(value){ if(!value)return "—"; return new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(value)); }
function escapeHtml(value=""){ const div=document.createElement("div"); div.textContent=value; return div.innerHTML; }
function optionalNumber(selector){ const value=$(selector).value.trim(); return value===""?null:Number(value); }
function formatScore(value){ return value==null?null:Number(value).toFixed(4).replace(/0+$/,"").replace(/\.$/,""); }

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

function renderDocuments(){
  const list=$("#documents-list"), empty=$("#documents-empty"); list.innerHTML=""; empty.hidden=state.documents.length>0;
  $("#documents-total").textContent=`${state.documentTotal} DOCUMENTS`;
  state.documents.forEach(doc=>{
    const row=document.createElement("article"); row.className="document-row";
    row.innerHTML=`<button class="document-main" type="button"><span class="document-symbol">PDF</span><span class="document-name"><strong>${escapeHtml(doc.title||doc.original_filename||"未命名文档")}</strong><small>${escapeHtml(doc.original_filename||doc.source_uri)} · ${formatTime(doc.created_at)}</small></span></button><span class="status ${escapeHtml(doc.status)}">${escapeHtml(doc.status)}</span><div class="document-actions"><button class="text-button edit-document" type="button">编辑</button><button class="text-button danger delete-document" type="button">删除</button></div>`;
    row.querySelector(".document-main").onclick=()=>openDocument(doc.id);
    row.querySelector(".edit-document").onclick=()=>editDocument(doc);
    const deleteButton=row.querySelector(".delete-document");
    deleteButton.disabled=!deletableDocumentStatuses.has(doc.status);
    deleteButton.title=deleteButton.disabled?"文档仍在处理中，暂时不能删除":"";
    deleteButton.onclick=()=>deleteDocument(doc);
    list.append(row);
  });
  const pages=Math.max(1,Math.ceil(state.documentTotal/state.documentLimit)); const page=Math.floor(state.documentOffset/state.documentLimit)+1;
  $("#documents-pagination").hidden=state.documentTotal<=state.documentLimit;
  $("#documents-page").textContent=`第 ${page} / ${pages} 页`;
  $("#documents-prev").disabled=state.documentOffset===0;
  $("#documents-next").disabled=state.documentOffset+state.documentLimit>=state.documentTotal;
}

async function loadDocuments(reset=false){
  if(reset)state.documentOffset=0;
  const params=new URLSearchParams({offset:state.documentOffset,limit:state.documentLimit});
  const query=$("#documents-query").value.trim(), status=$("#documents-status").value;
  if(query)params.set("q",query); if(status)params.set("status",status);
  $("#documents-list").innerHTML='<div class="loading">正在加载知识库…</div>';
  try{const data=await api(`/v1/documents?${params}`);state.documents=data.items;state.documentTotal=data.total;renderDocuments();}
  catch(error){$("#documents-list").innerHTML="";toast(`文档列表加载失败：${error.message}`,true);}
}

function renderSearchResults(data){
  const results=$("#search-results"), empty=$("#search-empty"), summary=$("#search-summary");
  const degraded=Array.isArray(data.degraded_components)?data.degraded_components:[];
  results.innerHTML="";
  empty.hidden=data.results.length>0;
  if(!data.results.length){empty.querySelector("h3").textContent="没有找到相关内容";empty.querySelector("p").textContent=degraded.length?`部分检索引擎不可用：${degraded.join("、")}。请稍后重试。`:"尝试更换关键词、增加召回数量或启用另一种检索方式。";}
  summary.hidden=false;
  summary.innerHTML=`<span><strong>${data.total}</strong> 条结果</span><span><strong>${Number(data.retrieval_time_ms).toFixed(1)}</strong> ms</span><span>${escapeHtml(data.fusion_method.toUpperCase())}</span><span>向量 ${data.components?.vector??0} · 关键词 ${data.components?.keyword??0}</span>${degraded.length?`<span>已降级：${escapeHtml(degraded.join("、"))}</span>`:""}`;
  data.results.forEach((item,index)=>{
    const source=item.source||{};
    const card=document.createElement("article");
    card.className="search-result-card";
    const scores=[
      ["综合",item.score],
      ["向量",item.vector_score],
      ["关键词",item.keyword_score],
      ["重排",item.rerank_score],
    ].filter(([,value])=>value!=null);
    card.innerHTML=`<div class="result-rank">${String(index+1).padStart(2,"0")}</div><div class="result-content"><div class="result-topline"><div class="result-source"><strong>${escapeHtml(source.original_filename||source.source_uri||"未知来源")}</strong><span>CHUNK ${(source.chunk_index??0)+1}${source.page_no!=null?` · PAGE ${source.page_no}`:""}</span></div><div class="score-list">${scores.map(([name,value])=>`<span>${name} <b>${formatScore(value)}</b></span>`).join("")}</div></div><p>${escapeHtml(item.text||"该结果没有可显示的文本内容。")}</p><div class="result-footer"><code>${escapeHtml(source.document_id||"")}</code>${source.document_id?'<button class="text-button result-document" type="button">查看原文 →</button>':""}</div></div>`;
    const openButton=card.querySelector(".result-document");
    if(openButton)openButton.onclick=()=>openDocument(source.document_id);
    results.append(card);
  });
}

async function runSearch(){
  const query=$("#search-query").value.trim();
  const enableVector=$("#search-vector").checked, enableKeyword=$("#search-keyword").checked;
  if(!query)return;
  if(!enableVector&&!enableKeyword){toast("请至少启用一种检索方式",true);return;}

  const payload={
    query,
    top_k:optionalNumber("#search-top-k"),
    vector_top_k:optionalNumber("#search-vector-top-k"),
    keyword_top_k:optionalNumber("#search-keyword-top-k"),
    fusion_method:$("#search-fusion").value,
    enable_vector:enableVector,
    enable_keyword:enableKeyword,
    enable_rerank:$("#search-rerank").checked,
  };
  if(payload.fusion_method==="weighted_score"){
    payload.vector_weight=optionalNumber("#search-vector-weight");
    payload.keyword_weight=optionalNumber("#search-keyword-weight");
    if((payload.vector_weight??0)+(payload.keyword_weight??0)===0){toast("向量权重和关键词权重不能同时为 0",true);return;}
  }
  const sourceUri=$("#search-source-uri").value.trim();
  if(sourceUri)payload.filters={source_uri:sourceUri};

  const button=$("#search-button"), results=$("#search-results"), empty=$("#search-empty"), summary=$("#search-summary");
  button.disabled=true;button.firstChild.textContent="正在检索 ";
  results.innerHTML='<div class="loading">正在执行向量召回、关键词召回与结果融合…</div>';
  empty.hidden=true;summary.hidden=true;
  try{
    const data=await api("/v1/search",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    renderSearchResults(data);
  }catch(error){results.innerHTML="";empty.hidden=false;empty.querySelector("h3").textContent="检索失败";empty.querySelector("p").textContent="请检查检索服务状态后重试。";toast(`检索失败：${error.message}`,true);}
  finally{button.disabled=false;button.firstChild.textContent="开始检索 ";}
}

function syncSearchOptions(){
  const weighted=$("#search-fusion").value==="weighted_score";
  $("#search-vector-weight").disabled=!weighted;
  $("#search-keyword-weight").disabled=!weighted;
  $("#search-vector-top-k").disabled=!$("#search-vector").checked;
  $("#search-keyword-top-k").disabled=!$("#search-keyword").checked;
}

async function editDocument(doc){
  const title=window.prompt("文档标题",doc.title||doc.original_filename||"");
  if(title===null)return;
  if(!title.trim()){toast("标题不能为空",true);return;}
  try{await api(`/v1/documents/${doc.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:title.trim()})});toast("文档标题已更新");await loadDocuments();if($("#document-id").value===doc.id)await openDocument(doc.id);}
  catch(error){toast(`更新失败：${error.message}`,true);}
}

async function deleteDocument(doc){
  if(!deletableDocumentStatuses.has(doc.status)){toast("文档仍在处理中，暂时不能删除",true);return;}
  const name=doc.title||doc.original_filename||doc.id;
  if(!window.confirm(`确定删除“${name}”吗？\n分块、向量和产物文件也会被永久删除。`))return;
  try{await api(`/v1/documents/${doc.id}`,{method:"DELETE"});toast("文档及关联数据已删除");state.jobs.forEach(job=>{if(job.document_id===doc.id)job.document_id=null;if(job.pending_document_id===doc.id)job.pending_document_id=null;if(Array.isArray(job.conflict_candidates))job.conflict_candidates=job.conflict_candidates.filter(id=>id!==doc.id)});persist();renderJobs();if($("#document-id").value===doc.id){$("#document-id").value="";$("#document-result").innerHTML="";}if(state.documentOffset>0&&state.documents.length===1)state.documentOffset=Math.max(0,state.documentOffset-state.documentLimit);await loadDocuments();}
  catch(error){toast(`删除失败：${error.message}`,true);}
}

async function openDocument(id){
  switchView("documents"); $("#document-id").value=id; const target=$("#document-result"); target.innerHTML='<div class="loading">正在读取文档与分块…</div>';
  try{
    const [doc,chunks,artifacts]=await Promise.all([api(`/v1/documents/${id}`),api(`/v1/documents/${id}/chunks`),api(`/v1/documents/${id}/artifacts`)]);
    target.innerHTML=`<div class="doc-card"><div class="panel-header"><div><p class="kicker">${escapeHtml(doc.status)}</p><h2>${escapeHtml(doc.title||doc.original_filename||"未命名文档")}</h2></div><span class="format-tag">${chunks.length} CHUNKS</span></div><div class="doc-meta"><div class="meta-box"><span>文档 ID</span><strong>${escapeHtml(doc.id)}</strong></div><div class="meta-box"><span>页数</span><strong>${escapeHtml(String(doc.metadata?.page_count??"—"))}</strong></div><div class="meta-box"><span>产物文件</span><strong>${artifacts.files.length}</strong></div></div><div class="chunks">${chunks.map(c=>`<article class="chunk"><div class="chunk-head"><span>CHUNK ${c.chunk_index+1}</span><span>PAGE ${c.page_no??"—"} · ${c.token_count} TOKENS</span></div><p>${escapeHtml(c.text)}</p></article>`).join("")}</div></div>`;
  }catch(error){target.innerHTML="";toast(`文档查询失败：${error.message}`,true)}
}

function switchView(name){ document.querySelectorAll(".view").forEach(v=>v.classList.toggle("active",v.id===`view-${name}`)); document.querySelectorAll(".nav-item").forEach(v=>v.classList.toggle("active",v.dataset.view===name)); $("#page-title").textContent={workspace:"知识工作台",search:"知识检索",documents:"知识库管理",system:"系统状态"}[name]; if(name==="documents")loadDocuments(); if(name==="search")setTimeout(()=>$("#search-query").focus(),0); }
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
$("#documents-filter").onsubmit=e=>{e.preventDefault();loadDocuments(true)};
$("#refresh-documents").onclick=()=>loadDocuments();
$("#documents-prev").onclick=()=>{state.documentOffset=Math.max(0,state.documentOffset-state.documentLimit);loadDocuments()};
$("#documents-next").onclick=()=>{state.documentOffset+=state.documentLimit;loadDocuments()};
$("#search-form").onsubmit=e=>{e.preventDefault();runSearch()};
$("#search-fusion").onchange=syncSearchOptions;
$("#search-vector").onchange=syncSearchOptions;
$("#search-keyword").onchange=syncSearchOptions;
setInterval(()=>$("#clock").textContent=new Date().toLocaleString("zh-CN",{hour12:false}),1000);
syncSearchOptions(); renderJobs(); refreshHealth(); state.jobs.filter(j=>!terminal.has(j.status)).forEach(j=>pollJob(j.id));
