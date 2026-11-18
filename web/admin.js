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
    notice();$('health').className='health on';$('health').textContent='本机服务在线';
    $('config').textContent=(result.model_ready?'当前模型 · '+result.model_name:'仅收图 · 等待启用模型')+'　/　'+(result.callback_ready?'4S 回调已配置':'4S 回调待配置');
    if(result.failed_callbacks)notice('有 '+result.failed_callbacks+' 条回调已耗尽重试，请筛选“回调失败”处理。');
    const c=result.counts;
    const sum=Object.values(c).reduce((a,b)=>a+b,0);
    $('all').textContent=sum;$('done').textContent=c.completed||0;$('failed').textContent=c.failed||0;
    $('pending').textContent=sum-(c.completed||0)-(c.failed||0);
    const fragment=document.createDocumentFragment();
    for(const t of result.items){
      const row=node('tr'), first=node('td');
      first.append(node('strong',t.alert_id),node('small',t.project_id));
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
    const src=base()+'/'+taskId+'/image';
    if($('preview').getAttribute('src')!==src){$('preview').src=src;$('fullImage').removeAttribute('src');}
    $('scope').textContent=t.roi?'审核矩形（左、上、右、下） '+t.roi.join(' / '):'审核范围 · 整张图片';
    const facts={'报警编号':t.alert_id,'项目编号':t.project_id,'报警类型':alarms[t.alarm_type],'摄像头':t.camera_id,
      '原报警时间':dateText(t.occurred_at),'收件时间':dateText(t.received_at),'处理状态':states[t.status],
      '模型 / 规则':(t.model||'—')+' / '+t.rule_version,'审核尝试':t.review_attempts+' 次','本次耗时':t.elapsed_ms==null?'—':t.elapsed_ms+' ms',
      '完成时间':dateText(t.finished_at),'回调状态':callbacks[t.callback_status],
      '回调尝试':t.callback_attempts+' 次','任务编号':t.task_id};
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
  e.preventDefault();const f=e.target.elements,file=f.image.files[0];
  if(!file||file.size>8*1024*1024){$('uploadError').textContent='请选择 8 MiB 以内的单张图片';return;}
  $('send').disabled=true;$('uploadError').textContent='';
  try{
    const data=await new Promise((ok,fail)=>{const reader=new FileReader();reader.onload=()=>ok(reader.result.split(',')[1]);reader.onerror=()=>fail(Error('读取图片失败'));reader.readAsDataURL(file);});
    const payload={project_id:f.project_id.value,alert_id:f.alert_id.value,alarm_type:f.alarm_type.value,image_base64:data};
    const t=await fetchJson(base(),{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Action':'1'},body:JSON.stringify(payload)});
    $('upload').close();$('filters').reset();await refresh();await showDetail(t.task_id);
  }catch(err){$('uploadError').textContent=err.message;}finally{$('send').disabled=false;}
};

let modelData = {profiles: []};
const modelForm = $('modelForm');
const modelSelect = $('modelProfiles');
const modelHeaders = {'Content-Type':'application/json','X-Admin-Action':'1'};

function modelMessage(message, error=false) {
  $('modelMessage').textContent = message;
  $('modelMessage').className = error ? 'error' : '';
}

function showModelForm() {
  const profile = modelData.profiles.find(item => item.name === modelSelect.value);
  const fields = modelForm.elements;
  fields.name.value = profile ? profile.name : '';
  fields.name.readOnly = Boolean(profile);
  fields.model_base.value = profile ? profile.model_base : '';
  fields.model.value = profile ? profile.model : '';
  fields.model_key.value = '';
  $('modelKeyHelp').textContent = profile && profile.has_key
    ? '已保存 API Key。留空会继续使用，填写新值可更换。'
    : '请填写与该地址对应的 API Key。';
}

async function loadModels(preferred) {
  modelData = await fetchJson('/admin/api/models');
  $('activeModel').textContent = modelData.active_model
    ? '当前使用 ' + modelData.active_profile + ' · ' + modelData.active_model
    : '当前已暂停审核，图片会先保存。';
  modelSelect.replaceChildren();
  modelData.profiles.forEach(profile => modelSelect.add(new Option(profile.name + ' · ' + profile.model, profile.name)));
  modelSelect.add(new Option('＋ 添加新配置', ''));
  const selectedName = preferred !== undefined ? preferred : modelData.active_profile;
  modelSelect.value = modelData.profiles.some(item => item.name === selectedName) ? selectedName : '';
  showModelForm();
}

function modelBusy(busy) {
  ['saveModel','activateModel','pauseModel'].forEach(id => $(id).disabled = busy);
}

async function saveModelForm() {
  if (!modelForm.reportValidity()) return null;
  const fields = modelForm.elements;
  const name = fields.name.value.trim();
  if (!modelSelect.value && modelData.profiles.some(profile => profile.name === name)) {
    throw Error('此配置名称已存在，请在上方选择后修改。');
  }
  const payload = {name, model_base:fields.model_base.value.trim(), model:fields.model.value.trim(),
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
$('modelSettings').addEventListener('close', () => { modelForm.elements.model_key.value = ''; });
modelForm.onsubmit = async event => {
  event.preventDefault();
  modelBusy(true);
  try {
    const saved = await saveModelForm();
    if (saved) { await loadModels(saved.name); modelMessage('配置已保存。点击「验证并启用」后生效。'); }
  } catch (error) { modelMessage(error.message, true); }
  finally { modelBusy(false); }
};
$('activateModel').onclick = async () => {
  modelBusy(true);
  try {
    const saved = await saveModelForm();
    if (!saved) return;
    modelMessage('正在用合成图片验证模型，请稍候…');
    await fetchJson('/admin/api/models/activate', {method:'POST', headers:modelHeaders,
                                                  body:JSON.stringify({name:saved.name, revision:saved.revision})});
    await loadModels(saved.name);
    await refresh();
    modelMessage('验证通过，模型已启用。');
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
