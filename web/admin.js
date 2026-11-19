'use strict';
const $ = id => document.getElementById(id);
const states = {awaiting_model:'等待模型',queued:'排队中',processing:'审核中',retrying:'等待重试',completed:'已完成',failed:'技术失败'};
const verdicts = {valid:'有效',false_alarm:'误报',uncertain:'无法确定'};
const callbacks = {waiting_result:'等待审核',unconfigured:'待配回调',pending:'等待发送',sending:'发送中',retrying:'等待重试',delivered:'已送达',failed:'回调失败'};
const alarms = {no_helmet:'未戴安全帽',no_vest:'未穿反光衣',danger_zone:'危险区域',combined:'组合报警'};
const elements = $('filters').elements;
for (const [name, mapping] of Object.entries({status:states, verdict:verdicts, callback:callbacks})) {
  elements[name].add(new Option('全部',''));
  Object.entries(mapping).forEach(([v,label])=>elements[name].add(new Option(label,v)));
}
let page=1, maxPage=1, filter='', listRequest=0, selected=null, detailRequest=0;
const base=()=>'/admin/api/live/tasks';
function node(tag, value, cls) {
  const el=document.createElement(tag);
  if(value!=null) el.textContent=value;
  if(cls) el.className=cls;
  return el;
}
function chip(code, dict) {
  const color = {completed:'good',delivered:'good',valid:'blue',false_alarm:'good',failed:'bad',retrying:'waiting',awaiting_model:'waiting',unconfigured:'waiting',processing:'blue',uncertain:'waiting'};
  return node('span',dict[code]||'—','chip '+(color[code]||''));
}
function dateText(v){return v?new Date(v).toLocaleString('zh-CN',{hour12:false}):'—';}
async function fetchJson(url,options){
  const res=await fetch(url,options);
  const result=await res.json();
  if(!res.ok){
    const msg=Array.isArray(result.detail)?result.detail.map(e=>e.loc.slice(1).join('.')+' '+e.msg).join(' / '):result.detail;
    throw Error(msg||'请求失败 '+res.status);
  }
  return result;
}
function notice(msg=''){
  $('notice').textContent=msg;
  $('notice').hidden=!msg;
}
async function refresh() {
  const id=++listRequest;
  try {
    const params=new URLSearchParams(filter);
    params.set('page',page);
    const result=await fetchJson(base()+'?'+params);
    if(id!==listRequest)return;
    maxPage=Math.max(1,Math.ceil(result.total/25));
    if(page>maxPage){page=maxPage;return refresh();}
    notice();$('health').className='health on';$('health').textContent='审核服务在线';
    const modelStatus=result.model_ready?'主模型 · '+result.model_name+(result.fallback_model?'　/　备用 · '+result.fallback_model:''):'仅收图 · 等待启用模型';
    $('config').textContent=modelStatus+'　/　'+(result.callback_ready?'4S 回调已配置':'4S 回调待配置');
    if(result.failed_callbacks)notice('有 '+result.failed_callbacks+' 条回调已耗尽重试，请筛选“回调失败”处理。');
    const c=result.counts;
    const sum=Object.values(c).reduce((a,b)=>a+b,0);
    $('all').textContent=sum;$('done').textContent=c.completed||0;$('failed').textContent=c.failed||0;
    $('pending').textContent=sum-(c.completed||0)-(c.failed||0);
    const fragment=document.createDocumentFragment();
    for(const t of result.items){
      const row=node('tr'), first=node('td');
      first.append(node('strong',t.alert_id),node('small',t.project_id+' / '+t.images.length+' 张图'));
      row.append(first,node('td',alarms[t.alarm_type]),node('td',dateText(t.received_at)));
      for(const item of [chip(t.status,states),chip(t.verdict,verdicts),chip(t.callback_status,callbacks)]){
        const cell=node('td');cell.append(item);row.append(cell);
      }
      const open=node('button','详情','link');open.setAttribute('aria-label','查看 '+t.alert_id+' 详情');
      open.onclick=()=>showDetail(t.task_id);const cell=node('td');cell.append(open);row.append(cell);fragment.append(row);
    }
    $('rows').replaceChildren(fragment);
    $('empty').hidden=result.total>0;
    $('empty').querySelector('p').textContent=filter?'请调整查询条件。':'4S 提交的报警图片会出现在这里。';
    $('total').textContent='共 '+result.total+' 条';$('page').textContent=page+' / '+maxPage;
    $('prev').disabled=page<=1;$('next').disabled=page>=maxPage;
  }catch(e){if(id===listRequest){notice(e.message);$('health').className='health';$('health').textContent='连接异常';}}
}
async function showDetail(taskId, poll=false){
  const ticket=++detailRequest;
  if(!poll){selected=taskId;notice();}
  try{
    const t=await fetchJson(base()+'/'+taskId);
    if(ticket!==detailRequest||selected!==taskId)return;
    $('detailMode').textContent='报警复核';
    $('verdict').replaceChildren(chip(t.verdict,verdicts));
    $('reason').textContent=t.reason||t.error||(t.status==='awaiting_model'?'图片已保存，启用视觉模型后开始审核。':'暂未产生审核理由。');
    const select=$('frameSelect'), current=poll?select.value:'1';
    select.replaceChildren(...t.images.map(f=>new Option('第 '+f.index+' 张',String(f.index))));
    select.value=current;
    $('frameLabel').hidden=t.images.length<2;
    function displayFrame(){
      const f=t.images[select.selectedIndex]||t.images[0];
      const src=base()+'/'+taskId+'/image?index='+f.index;
      if($('preview').getAttribute('src')!==src){$('preview').src=src;$('fullImage').removeAttribute('src');}
      $('scope').textContent=f.roi?'审核区域 '+f.roi.join(' / '):'审核范围 · 整张图片';
      $('frameResult').textContent=t.images.length>1?
        [verdicts[f.verdict]||states[f.status]||'待审核', f.reason||f.error].filter(Boolean).join(' · '):'';
    }
    select.onchange=displayFrame;
    displayFrame();
    const facts={'报警编号':t.alert_id,'图片编号':t.image_id,'项目编号':t.project_id,'报警类型':alarms[t.alarm_type],'摄像头':t.camera_id,
      '原报警时间':dateText(t.occurred_at),'收件时间':dateText(t.received_at),'处理状态':states[t.status],
      '模型 / 规则':(t.model||'—')+' / '+t.rule_version,'审核尝试':t.review_attempts+' 次','本次耗时':t.elapsed_ms==null?'—':t.elapsed_ms+' ms',
      '完成时间':dateText(t.finished_at),'回调状态':callbacks[t.callback_status],
      '回调尝试':t.callback_attempts+' 次','任务编号':t.task_id};
    if(t.model_calls&&t.model_calls.length)facts['模型调用']=t.model_calls.map(c=>
      '图 '+c.image_index+' · 第 '+c.attempt+' 次 '+(c.role==='primary'?'主模型':'备用模型')+' '+c.requested_model+'：'+
      ({completed:'完成',failed:'失败',processing:'处理中',interrupted:'中断'}[c.status]||c.status)+(c.error?'（'+c.error+'）':'')).join('；');
    $('facts').replaceChildren(...Object.entries(facts).flatMap(([k,v])=>[node('dt',k),node('dd',v??'—')]));
    $('deliveryError').textContent=t.callback_error||'';$('resend').hidden=t.callback_status!=='failed';
    if(!$('detail').open)$('detail').showModal();
  }catch(e){if(ticket===detailRequest)notice(e.message);}
}
$('detail').addEventListener('close',()=>{selected=null;detailRequest++;});
$('zoomBtn').onclick=()=>{$('fullImage').src=$('preview').src;$('zoom').showModal();};
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
$('resend').onclick=async()=>{
  try{await fetchJson(base()+'/'+selected+'/resend',{method:'POST',headers:{'X-Admin-Action':'1'}});showDetail(selected);refresh();}
  catch(e){$('deliveryError').textContent=e.message;}
};
$('prev').onclick=()=>{if(page>1){page--;refresh();}};$('next').onclick=()=>{if(page<maxPage){page++;refresh();}};
$('filters').onsubmit=e=>{
  e.preventDefault();if(elements.since.value&&elements.until.value&&elements.since.value>elements.until.value){notice('起始日期不能晚于结束日期');return;}
  const params=new URLSearchParams();for(const [k,v] of new FormData(e.target))if(v)params.set(k,v);
  filter=params.toString();page=1;refresh();
};
$('filters').onreset=()=>{filter='';page=1;queueMicrotask(refresh);};
$('refresh').onclick=refresh;
$('uploadBtn').onclick=()=>{
  const f=$('uploadForm');f.reset();
  $('uploadTitle').textContent='提交报警图片';
  $('uploadHint').textContent='安全帽和反光衣图片会交给当前启用的模型审核。';
  $('uploadError').textContent='';$('upload').showModal();
};
$('uploadForm').onsubmit=async e=>{
  e.preventDefault();const f=e.target.elements,files=Array.from(f.image.files);
  if(files.length<1||files.length>5||files.some(file=>file.size>8*1024*1024)){
    $('uploadError').textContent='请选择 1–5 张图片，每张最多 8 MiB';return;
  }
  $('send').disabled=true;$('uploadError').textContent='';
  try{
    const images=await Promise.all(files.map(file=>new Promise((ok,fail)=>{
      const reader=new FileReader();reader.onload=()=>ok({image_base64:reader.result.split(',')[1]});
      reader.onerror=()=>fail(Error('读取图片失败'));reader.readAsDataURL(file);
    })));
    const payload={project_id:f.project_id.value,alert_id:f.alert_id.value,alarm_type:f.alarm_type.value,images};
    const t=await fetchJson(base(),{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Action':'1'},body:JSON.stringify(payload)});
    $('upload').close();$('filters').reset();await refresh();await showDetail(t.task_id);
  }catch(err){$('uploadError').textContent=err.message;}finally{$('send').disabled=false;}
};

let modelData = {profiles: []};
const modelForm = $('modelForm');
const modelSelect = $('modelProfiles');
const modelHeaders = {'Content-Type':'application/json','X-Admin-Action':'1'};
const providerBases = {deepseek:'https://api.deepseek.com',bailian:'https://dashscope.aliyuncs.com/compatible-mode/v1',compatible:''};

function modelMessage(message, error=false) {
  $('modelMessage').textContent = message;
  $('modelMessage').className = error ? 'error' : '';
}

function showModelForm() {
  const profile = modelData.profiles.find(item => item.name === modelSelect.value);
  const fields = modelForm.elements;
  fields.name.value = profile ? profile.name : '';
  fields.name.readOnly = Boolean(profile);
  fields.provider.value = profile ? profile.provider : 'deepseek';
  fields.model_base.value = profile ? profile.model_base : providerBases[fields.provider.value];
  fields.model.value = profile ? profile.model : '';
  fields.model_key.value = '';
  $('catalogModels').replaceChildren(new Option('先读取模型列表，也可以手动填写',''));
  $('modelKeyHelp').textContent = profile && profile.has_key
    ? '已保存 API Key。留空会继续使用，填写新值可更换。'
    : '请填写与该地址对应的 API Key。';
}

async function loadModels(preferred) {
  modelData = await fetchJson('/admin/api/models');
  $('activeModel').textContent = modelData.active_model
    ? '当前主模型：' + modelData.active_profile + ' · ' + modelData.active_model +
      (modelData.fallback_model ? '；备用：' + modelData.fallback_profile + ' · ' + modelData.fallback_model : '；未设置备用模型')
    : '当前已暂停审核，图片会先保存。';
  modelSelect.replaceChildren();
  modelData.profiles.forEach(profile => modelSelect.add(new Option(profile.name + ' · ' + profile.model, profile.name)));
  modelSelect.add(new Option('＋ 添加新配置', ''));
  const selectedName = preferred !== undefined ? preferred : (modelData.active_model ? modelData.active_profile : modelData.preferred_primary);
  modelSelect.value = modelData.profiles.some(item => item.name === selectedName) ? selectedName : '';
  const primary=$('primaryProfile'), fallback=$('fallbackProfile');
  primary.replaceChildren(new Option('请选择主模型',''));
  fallback.replaceChildren(new Option('不使用备用模型',''));
  modelData.profiles.forEach(profile => {
    const label=profile.name+' · '+profile.model;
    primary.add(new Option(label,profile.name));fallback.add(new Option(label,profile.name));
  });
  const wantedPrimary=modelData.active_model?modelData.active_profile:(modelData.preferred_primary||'');
  primary.value=modelData.profiles.some(item=>item.name===wantedPrimary)?wantedPrimary:'';
  const wantedFallback=modelData.fallback_profile||modelData.preferred_fallback||'';
  fallback.value=modelData.profiles.some(item=>item.name===wantedFallback)?wantedFallback:'';
  $('primaryTimeout').value=modelData.timeout_seconds||15;
  showModelForm();
}

function modelBusy(busy) {
  ['saveModel','fetchCatalog','activateModel','pauseModel'].forEach(id => $(id).disabled = busy);
}

async function saveModelForm() {
  if (!modelForm.reportValidity()) return null;
  const fields = modelForm.elements;
  const name = fields.name.value.trim();
  if (!modelSelect.value && modelData.profiles.some(profile => profile.name === name)) {
    throw Error('此配置名称已存在，请在上方选择后修改。');
  }
  const payload = {name, provider:fields.provider.value, model_base:fields.model_base.value.trim(), model:fields.model.value.trim(),
                   model_key:fields.model_key.value.trim()};
  const result = await fetchJson('/admin/api/models/save', {method:'POST', headers:modelHeaders, body:JSON.stringify(payload)});
  fields.model_key.value = '';
  return result;
}

$('modelSettingsBtn').onclick = async () => {
  modelMessage('正在读取模型配置…');
  $('modelSettings').showModal();
  try { await loadModels(); modelMessage(''); }
  catch (error) { modelMessage(error.message, true); }
};
modelSelect.onchange = () => { showModelForm(); modelMessage(''); };
modelForm.elements.provider.onchange=()=>{
  modelForm.elements.model_base.value=providerBases[modelForm.elements.provider.value];
  $('catalogModels').replaceChildren(new Option('先读取模型列表，也可以手动填写',''));
};
$('catalogModels').onchange=()=>{if($('catalogModels').value)modelForm.elements.model.value=$('catalogModels').value;};
$('fetchCatalog').onclick=async()=>{
  const fields=modelForm.elements;
  if(!fields.model_base.reportValidity())return;
  modelBusy(true);modelMessage('正在读取平台模型列表…');
  try{
    const result=await fetchJson('/admin/api/models/catalog',{method:'POST',headers:modelHeaders,body:JSON.stringify({
      provider:fields.provider.value,name:modelSelect.value||null,model_base:fields.model_base.value.trim(),
      model_key:fields.model_key.value.trim()})});
    $('catalogModels').replaceChildren(new Option('请选择模型',''));
    result.models.forEach(item=>$('catalogModels').add(new Option(item.id+(item.vision==='unknown'?'（图片能力待验证）':''),item.id)));
    modelMessage('读取到 '+result.models.length+' 个模型'+(result.truncated?'（列表已截断）':'')+'。选择后请保存配置。');
  }catch(error){modelMessage(error.message,true);}finally{modelBusy(false);}
};
$('modelSettings').addEventListener('close', () => { modelForm.elements.model_key.value = ''; });
modelForm.onsubmit = async event => {
  event.preventDefault();
  modelBusy(true);
  try {
    const saved = await saveModelForm();
    if (saved) { await loadModels(saved.name); modelMessage('模型配置已保存。请选择主备，再点击「验证并启用主备」。'); }
  } catch (error) { modelMessage(error.message, true); }
  finally { modelBusy(false); }
};
$('activateModel').onclick = async () => {
  modelBusy(true);
  try {
    const primary=modelData.profiles.find(item=>item.name===$('primaryProfile').value);
    const fallback=modelData.profiles.find(item=>item.name===$('fallbackProfile').value);
    if(!primary)throw Error('请先保存并选择主模型。');
    if(fallback&&fallback.name===primary.name)throw Error('主模型和备用模型请选择不同配置。');
    if(!$('primaryTimeout').reportValidity())return;
    modelMessage('正在用合成图片验证主模型和备用模型，请稍候…');
    await fetchJson('/admin/api/models/routing', {method:'POST', headers:modelHeaders, body:JSON.stringify({
      primary:primary.name,primary_revision:primary.revision,fallback:fallback?fallback.name:null,
      fallback_revision:fallback?fallback.revision:null,timeout_seconds:Number($('primaryTimeout').value)})});
    await loadModels(primary.name);
    await refresh();
    modelMessage('验证通过，主备设置已启用。');
  } catch (error) { modelMessage(error.message, true); }
  finally { modelBusy(false); }
};
$('pauseModel').onclick = async () => {
  modelBusy(true);
  try {
    await fetchJson('/admin/api/models/pause', {method:'POST', headers:modelHeaders, body:'{}'});
    await loadModels(modelSelect.value);
    await refresh();
    modelMessage('审核已暂停。正在处理的任务会按原模型完成。');
  } catch (error) { modelMessage(error.message, true); }
  finally { modelBusy(false); }
};
refresh();
setInterval(()=>{if(!document.hidden){refresh();if(selected&&$('detail').open)showDetail(selected,true);}},5000);
