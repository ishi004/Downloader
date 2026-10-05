'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const escapeHtml = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const e = escapeHtml;
let token = '', settings, defaults, health, entries = [], jobs = [], filter = 'all', inspecting = false, enqueueing = false;
let inputTimer, toastTimer, defaultTimer, lastInput = '', jobsSignature = '', settingsDirty = false;
const expandedJobs = new Set();
const statusNames = {queued:'대기 중',running:'다운로드',processing:'변환 중',completed:'완료',failed:'실패',cancelled:'취소됨',cancelling:'취소 중',interrupted:'중단됨'};

function bytes(n) {
  if (n == null || !Number.isFinite(n)) return '정보 없음';
  if (n === 0) return '0 B';
  const i = Math.min(3, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${['B','KiB','MiB','GiB'][i]}`;
}
function duration(n) {
  if (n == null || !Number.isFinite(n)) return '길이 정보 없음';
  n = Math.round(n); const h = Math.floor(n / 3600), m = Math.floor(n / 60) % 60, s = n % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${m}:${String(s).padStart(2,'0')}`;
}
function timeValue(value) {
  if (!value.trim()) return null;
  if (!/^\d+(?::[0-5]?\d){0,2}(?:\.\d{1,3})?$/.test(value.trim())) throw Error('구간 시간은 초 또는 HH:MM:SS 형식으로 입력하세요.');
  return value.trim().split(':').reduce((a,b) => a*60+Number(b),0);
}
function imageTag(url, className = '') {
  if (!url || (!/^https?:\/\//i.test(url) && !/^\/api\/jobs\/[a-f0-9]+\/files\/\d+$/.test(url))) return '';
  return `<img class="${className}" src="${e(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`;
}
function toast(message) {
  $('#toast').textContent = message; $('#toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 5500);
}
async function api(path, method='GET', data) {
  const response = await fetch('/api' + path, {method, headers: {'Content-Type':'application/json', 'X-MediaDock-Token':token}, ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const result = await response.json();
  if (!response.ok) {
    const d = result.detail;
    throw Error(Array.isArray(d) ? d.map(x => x.msg.replace(/^Value error, /,'')).join('\n') : d || '요청에 실패했습니다.');
  }
  return result;
}
function applySettings(s) {
  $('#save-dir').value = s.save_dir; $('#subfolder').value = s.subfolder; $('#by-channel').checked = s.by_channel;
  $('#filename').value = s.filename; $('#concurrency').value = s.concurrency; $('#speed').value = s.speed_kib;
  $('#remember').checked = s.remember; $('#app-name').value = s.app_name; $('#port').value = s.port;
  $$('[data-app-name]').forEach(el => el.textContent = s.app_name);
  document.title = s.app_name + ' · 미디어 다운로드';
}
function readSettings() {
  if (!$('#settings-form').reportValidity()) throw Error('저장 설정을 확인하세요.');
  return {...settings, save_dir: $('#save-dir').value, subfolder: $('#subfolder').value, by_channel: $('#by-channel').checked,
    filename: $('#filename').value, concurrency: Number($('#concurrency').value), speed_kib: Number($('#speed').value),
    remember: $('#remember').checked, app_name: $('#app-name').value, port: Number($('#port').value), defaults};
}
async function saveSettings(silent=false) {
  const result = await api('/settings', 'PUT', readSettings());
  settings = result.settings; settingsDirty = false;
  $$('[data-app-name]').forEach(el => el.textContent = settings.app_name);
  document.title = settings.app_name + ' · 미디어 다운로드';
  $('#settings-message').textContent = result.restart_required ? '저장했습니다. 포트는 앱 재시작 후 적용됩니다.' : '설정을 저장했습니다.';
  if (!silent) toast($('#settings-message').textContent);
}
function rememberDefaults(opts) {
  defaults = structuredClone(opts); updateDefaultKind();
  clearTimeout(defaultTimer);
  if ($('#remember').checked) defaultTimer = setTimeout(async () => {
    try {
      // Persist only defaults; don't silently apply partially edited storage settings.
      const result = await api('/settings','PUT',{...settings,defaults}); settings = result.settings;
    } catch (err) { toast(err.message); }
  },700);
}
function updateDefaultKind() {
  for (const kind of ['video','audio']) {
    const btn = $('#default-' + kind); btn.classList.toggle('selected', defaults.kind === kind); btn.setAttribute('aria-pressed', String(defaults.kind === kind));
  }
  $('#default-summary').textContent = defaults.kind === 'video' ? '원본 화질 · ' + (defaults.container === 'auto' ? '자동' : defaults.container.toUpperCase()) : '음원 · ' + defaults.audio_format.toUpperCase();
}
function makeEntry(data, group=null) {
  return {key: crypto.randomUUID(), data, group, selected:true, opts: structuredClone(defaults), estimates:{}, loaded:!!data.media_id, loading:false};
}
function normalizeOptions(item) {
  const d = item.data, o = item.opts;
  if (o.height && !d.heights.includes(o.height)) o.height = null;
  const fps = availableFps(item);
  if (o.fps && !fps.includes(o.fps)) o.fps = null;
  if (!d.has_video) o.kind = 'audio';
  if (!d.has_audio && o.kind === 'audio') o.kind = 'video';
  if (o.kind === 'audio' && ['embed','burn'].includes(o.subtitle_mode)) o.subtitle_mode = 'srt';
  const langs = d.subtitles.filter(l => l.auto === o.auto_subtitles);
  if (!langs.some(l => l.code === o.subtitle_lang)) o.subtitle_lang = langs[0]?.code || null;
  if (!langs.length) o.subtitle_mode = 'none';
}
function availableFps(item) {
  const h = item.opts.height || item.data.heights[0];
  return item.data.fps_by_height[String(h)] || [];
}
function allItems() {return entries.flatMap(g => g.items || [g]);}
async function inspectLinks(force=false) {
  const raw = $('#urls').value.trim();
  if (!raw || inspecting || (!force && raw === lastInput)) return;
  const urls = [...new Set(raw.split(/\s+/).filter(Boolean))];
  if (urls.length > 20) { toast('한 번에 최대 20개 링크를 조회할 수 있습니다.'); return; }
  if (urls.some(url => { try {return !['http:','https:'].includes(new URL(url).protocol);} catch {return true;} })) {
    if (force) toast('올바른 HTTP 또는 HTTPS 링크를 입력하세요.'); return;
  }
  inspecting = true; lastInput = raw; $('#inspect').disabled = true;
  $('#inspection-status').hidden = false; $('#inspection-status').textContent = `${urls.length}개 링크의 실제 정보를 조회하고 있습니다…`;
  try {
    const results = await api('/inspect','POST',{urls,playlist:true});
    for (const result of results) {
      entries = entries.filter(g => !(g.error && g.url === result.url));
      if (entries.some(g => (g.url || g.data?.url) === result.url)) continue;
      if (result.type === 'playlist') {
        const group = {key:crypto.randomUUID(), title:result.title,url:result.url,limited:result.limited,items:[]};
        group.items = result.entries.map(d => makeEntry(d,group.key));
        group.items.filter(i=>i.loaded).forEach(normalizeOptions); entries.push(group);
      } else if (result.type === 'error') entries.push({key:crypto.randomUUID(),error:result.error,url:result.url});
      else {const item = makeEntry(result); normalizeOptions(item); entries.push(item);}
    }
    renderResults();
    allItems().filter(i => i.loaded).forEach(loadEstimates);
  } catch (err) {toast(err.message); lastInput = '';}
  finally {
    inspecting = false; $('#inspect').disabled = false; $('#inspection-status').hidden = true;
    if ($('#urls').value.trim() !== raw) inspectLinks();
  }
}
function optionsHtml(item) {
  const o = item.opts, d = item.data, langs = d.subtitles.filter(l => l.auto === o.auto_subtitles);
  const select = (name, values, value) => `<select data-opt="${name}">${values.map(([v,label]) => `<option value="${e(v)}" ${String(v)===String(value??'')?'selected':''}>${e(label)}</option>`).join('')}</select>`;
  const field = (label, inner, full=false) => `<label class="field ${full?'full':''}">${label}${inner}</label>`;
  const estimateLabel = h => {const est = item.estimates[String(h ?? '')]; return est ? ' · ' + bytes(est.bytes) : '';};
  let html = field('종류',select('kind',[...(d.has_video?[['video','영상']]:[]),...(d.has_audio?[['audio','음원']]:[])],o.kind));
  if (o.kind === 'video') {
    html += field('영상 형식',select('container',[['auto','자동 (원본 컨테이너)'],['mp4','MP4'],['mkv','MKV']],o.container));
    html += field('화질 · 다운로드 예상 크기',select('height',[['',`원본 화질${d.heights[0]?` (${d.heights[0]}p)`:''}${estimateLabel(null)}`], ...d.heights.slice(1).map(h=>[h,`${h}p${estimateLabel(h)}`])],o.height));
    html += field('프레임률',select('fps',[['','원본 프레임률'],...availableFps(item).map(f=>[f,`${f} fps`])],o.fps));
  } else {
    html += field('음원 형식',select('audio_format',[['mp3','MP3'],['m4a','M4A (AAC)'],['original','원본 음원']],o.audio_format));
    if (o.audio_format === 'mp3') {
      html += field('MP3 품질 방식',select('mp3_mode',[['vbr','VBR · 품질 우선'],['cbr','CBR · 비트레이트 지정']],o.mp3_mode));
      html += o.mp3_mode === 'vbr' ? field('품질 (0이 가장 높음)',select('mp3_quality',Array.from({length:10},(_,i)=>[i,`${i}${i===0?' · 최고 품질':''}`]),o.mp3_quality)) : field('비트레이트',select('bitrate',[64,96,128,160,192,256,320].map(n=>[n,`${n} kbps`]),o.bitrate));
    }
  }
  html += `<div class="estimate full" data-estimate>예상 다운로드 용량: <strong>${bytes(item.currentEstimate?.bytes)}</strong><br>${e(item.currentEstimate?.basis || '정보 없음')}<br>원본 다운로드 기준 · 변환·구간 저장 후 최종 크기는 달라집니다.</div>`;
  html += field('자막 처리',select('subtitle_mode',[['none','자막 없음'],['srt','별도 SRT 저장'],...(o.kind==='video'?[['embed','영상에 내장 (선택 가능)'],['burn','영상에 고정 (다시 인코딩)']]:[])],o.subtitle_mode));
  html += field('자막 종류',select('auto_subtitles',[[false,'제작자 자막'],[true,'자동 생성 자막']],o.auto_subtitles));
  html += field('자막 언어',select('subtitle_lang',langs.length ? langs.map(l=>[l.code,`${l.name} (${l.code})`]) : [['','제공되는 자막 없음']],o.subtitle_lang),true);
  html += field('구간 시작',`<input data-opt="start" value="${o.start || ''}" placeholder="0:00" inputmode="decimal">`);
  html += field('구간 종료',`<input data-opt="end" value="${o.end ?? ''}" placeholder="끝까지" inputmode="decimal">`);
  html += '<p class="option-note full">초 또는 HH:MM:SS. 원본 전체를 내려받은 뒤 정확한 구간을 저장하고 자막 시간을 조정합니다. 구간 저장·자막 고정은 다시 인코딩합니다.</p>';
  html += `<label class="check full"><input type="checkbox" data-opt="thumbnail" ${o.thumbnail?'checked':''}>원본 썸네일 함께 저장${!d.thumbnail?' (현재 제공되지 않음)':''}</label>`;
  return html;
}
function itemHtml(item) {
  if (!item.loaded) return `<div class="playlist-entry" data-key="${item.key}"><input type="checkbox" data-selected ${item.selected?'checked':''} aria-label="${e(item.data.title)} 선택"><div class="media-text">${e(item.data.title)}<div class="media-meta">${duration(item.data.duration)}</div></div><button data-load>${item.loading?'조회 중…':'정보·옵션 조회'}</button></div>`;
  const d = item.data;
  return `<article class="media-card" data-key="${item.key}"><div class="media-heading"><label class="check"><input type="checkbox" data-selected ${item.selected?'checked':''} aria-label="${e(d.title)} 선택"></label>${imageTag(d.thumbnail)}<div class="media-text"><h3>${e(d.title)}</h3><div class="media-meta"><span>${e(d.channel)}</span><span>${duration(d.duration)}</span></div></div><button data-remove class="icon-button" aria-label="항목 제거">×</button></div><details ${item.open?'open':''}><summary>출력 옵션 · ${item.opts.kind==='video'?'영상':'음원'} · ${item.opts.height?item.opts.height+'p':'원본'} 설정</summary><div class="media-options">${optionsHtml(item)}</div></details></article>`;
}
function renderResults() {
  $('#results').innerHTML = entries.map(g => g.items ? `<section class="playlist" data-group="${g.key}"><div class="playlist-top"><strong>${e(g.title)}</strong><span>${g.items.length}개 항목</span></div><div class="playlist-controls"><button class="text-button" data-group-all>전체 선택</button><button class="text-button" data-group-none>선택 해제</button><span class="helper">선택한 항목만 다운로드합니다.${g.limited?' 최대 200개까지 조회됩니다.':''}</span></div><div class="playlist-entries">${g.items.map(itemHtml).join('')}</div></section>` : g.error ? `<div class="notice error" data-key="${g.key}"><strong>조회할 수 없습니다</strong><p>${e(g.url)}</p><p>${e(g.error)}</p><button class="text-button" data-remove>목록에서 지우기</button></div>` : itemHtml(g)).join('');
  bindResults(); updateSelection();
}
function updateSelection() {
  const items = allItems().filter(i=>!i.error), count = items.filter(i=>i.selected).length;
  $('#queue-actions').hidden = !items.length; $('#selected-count').textContent = `${count}개 선택`;
  $('#select-all').checked = !!items.length && items.every(i=>i.selected); $('#select-all').indeterminate = count>0 && count<items.length;
  $('#enqueue').disabled = !count || enqueueing || !health?.ready;
}
function findItem(key) {return allItems().find(i=>i.key===key);}
function bindResults() {
  $$('[data-key]', $('#results')).forEach(node => {
    const item = findItem(node.dataset.key); if (!item) return;
    $('[data-selected]',node)?.addEventListener('change', ev => {item.selected = ev.target.checked; updateSelection();});
    $('[data-remove]',node)?.addEventListener('click',()=>{entries = entries.filter(i=>i.key!==item.key); entries.forEach(g=>{if(g.items)g.items=g.items.filter(i=>i.key!==item.key);}); renderResults();});
    $('[data-load]',node)?.addEventListener('click',async()=>{try{await hydrate(item);item.open=true;renderResults();loadEstimates(item);}catch(err){toast(err.message);}});
    $('details',node)?.addEventListener('toggle',ev=>item.open=ev.target.open);
    bindOptions(node,item);
  });
  $$('[data-group]').forEach(node=>{
    const group=entries.find(g=>g.key===node.dataset.group);
    $('[data-group-all]',node).onclick=()=>{group.items.forEach(i=>i.selected=true);renderResults();};
    $('[data-group-none]',node).onclick=()=>{group.items.forEach(i=>i.selected=false);renderResults();};
  });
}
async function hydrate(item) {
  if(item.loaded) return;
  item.loading=true;renderResults();
  try {
    const [result]=await api('/inspect','POST',{urls:[item.data.url],playlist:false});
    if(result.type!=='media') throw Error(result.error || '이 항목은 개별 영상 링크로 다시 조회해 주세요.');
    item.data=result;item.loaded=true;normalizeOptions(item);
  } finally {item.loading=false;}
}
async function loadEstimates(item) {
  if(!item.loaded) return;
  const opts=structuredClone(item.opts), revision=JSON.stringify(opts); item.estimateRevision=revision;
  try {
    const current=await api('/estimate/'+item.data.media_id,'POST',opts);
    const estimates={};
    if(opts.kind==='video') {
      for(const h of [null,...item.data.heights.slice(1)]) estimates[String(h??'')]=await api('/estimate/'+item.data.media_id,'POST',{...opts,height:h,fps:null});
    }
    if(item.estimateRevision!==revision) return;
    item.currentEstimate=current;item.estimates=estimates;
    const node=$(`[data-key="${item.key}"]`);
    // Never replace a field while the user is typing in it.
    if(node && !node.contains(document.activeElement)) {const options=$('.media-options',node);if(options){options.innerHTML=optionsHtml(item);bindOptions(node,item);}}
  }catch(err){item.currentEstimate={bytes:null,basis:err.message};}
}
function bindOptions(node,item) {
  $$('[data-opt]',node).forEach(el=>{
    if(['start','end'].includes(el.dataset.opt)) {
      el.addEventListener('input',()=>{
        try {const v=timeValue(el.value);item.opts[el.dataset.opt]=el.dataset.opt==='start'?v||0:v;el.setCustomValidity('');}
        catch(err){el.setCustomValidity(err.message);}
      });
      el.addEventListener('change',()=>{
        if(!el.reportValidity())return;
        if(item.opts.end!==null&&item.opts.end<=item.opts.start){toast('종료 시간은 시작 시간보다 뒤여야 합니다.');return;}
        rememberDefaults(item.opts);
      });
      return;
    }
    el.addEventListener('change',()=>{
    const key=el.dataset.opt;
    try{
      let value=el.type==='checkbox'?el.checked:el.value;
      if(['height','fps'].includes(key))value=value?Number(value):null;
      if(['mp3_quality','bitrate'].includes(key))value=Number(value);
      if(key==='auto_subtitles')value=value==='true';
      if(['start','end'].includes(key))value=timeValue(value);
      item.opts[key]=key==='start'?value||0:value;
      if(key==='height')item.opts.fps=null;
      normalizeOptions(item);
      if(item.opts.end!==null && item.opts.end<=item.opts.start) throw Error('종료 시간은 시작 시간보다 뒤여야 합니다.');
      if(item.data.duration && (item.opts.start>=item.data.duration || item.opts.end>item.data.duration)) throw Error('구간 시간이 영상 길이를 벗어납니다.');
      item.open=true;rememberDefaults(item.opts);renderResults();loadEstimates(item);
    }catch(err){toast(err.message);}
    });
  });
}
async function enqueueSelected() {
  const selected=allItems().filter(i=>i.selected&&!i.error);if(!selected.length)return;
  for(const el of $$('[data-opt="start"],[data-opt="end"]'))if(!el.reportValidity())return;
  enqueueing=true;updateSelection();$('#inspection-status').hidden=false;
  try {
    for(let i=0;i<selected.length;i++){
      $('#inspection-status').textContent=`선택 항목 확인 중 ${i+1} / ${selected.length}`;
      await hydrate(selected[i]);
    }
    await saveSettings(true);
    const results=await api('/jobs','POST',{items:selected.map(i=>({media_id:i.data.media_id,options:i.opts}))});
    const dup=results.filter(r=>r.duplicate).length;
    toast(`${results.length-dup}개 작업을 대기열에 추가했습니다.${dup?` ${dup}개는 동일한 작업이 있어 건너뛰었습니다.`:''}`);
    selected.forEach(i=>i.selected=false);filter='all';renderResults();setFilter('all',false);await poll();
  }catch(err){toast(err.message);renderResults();}
  finally{enqueueing=false;$('#inspection-status').hidden=true;updateSelection();}
}
function setFilter(next,scroll=true){
  filter=next;jobsSignature='';
  $$('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter===filter));
  $$('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===filter));
  $('#history-title').textContent=filter==='completed'?'완료한 파일':filter==='active'?'작업 대기열':'다운로드 내역';
  renderJobs();if(scroll)$('#history-section').scrollIntoView({behavior:'smooth',block:'start'});
}
function renderJobs(){
  const visible=jobs.filter(j=>filter==='all'||filter==='active'&&['running','processing','queued','cancelling'].includes(j.status)||filter==='completed'&&j.status==='completed'||filter==='issues'&&['failed','cancelled','interrupted'].includes(j.status));
  const sig=JSON.stringify(visible);if(sig===jobsSignature)return;jobsSignature=sig;
  $('#history-count').textContent=visible.length;
  if(!visible.length){$('#history').innerHTML=`<div class="empty-state"><div class="empty-icon">↓</div><h3>${jobs.length?'해당하는 작업이 없습니다':'첫 번째 미디어를 보관해 보세요'}</h3><p>${jobs.length?'다른 필터에서 작업 기록을 확인하세요.':'위에 링크를 입력하면 이곳에서<br>다운로드 진행 상황과 완료 파일을 확인할 수 있어요.'}</p></div>`;return;}
  $('#history').innerHTML=visible.map(j=>{
    const active=['running','processing','cancelling'].includes(j.status),o=j.options;
    const thumbIndex=j.files.findIndex(f=>f.name.includes('.thumbnail.'));
    const thumbnail=thumbIndex>=0?`/api/jobs/${j.id}/files/${thumbIndex}`:j.thumbnail;
    const description=o.kind==='video'?`${o.container==='auto'?'자동':o.container.toUpperCase()} · ${o.height?o.height+'p':'원본 화질'}${o.fps?' · '+o.fps+' fps':''}`:`${o.audio_format.toUpperCase()}${o.audio_format==='mp3'?' · '+(o.mp3_mode==='cbr'?o.bitrate+' kbps':'VBR '+o.mp3_quality):''}`;
    return `<article class="job" data-job="${j.id}"><div class="job-heading">${imageTag(thumbnail,'job-thumb')}<div class="job-info"><div class="job-title">${e(j.title)}</div><div class="job-meta">${e(description)} · ${e(j.channel)}${o.start||o.end?' · 구간 '+duration(o.start)+'–'+(o.end?duration(o.end):'끝'):''}</div></div><span class="status-label ${j.status}">${statusNames[j.status]}</span></div>
    ${active?`<div class="progress-area"><div class="progress-meta"><span>${e(j.stage)}${j.progress!=null?' · '+j.progress.toFixed(1)+'%':' · 처리 중'}</span><span>${j.speed?bytes(j.speed)+'/s':''}${j.eta?' · 약 '+duration(j.eta)+' 남음':''}</span></div><progress max="100" ${j.progress!=null?`value="${j.progress}"`:''} aria-label="${e(j.stage)} 진행률"></progress></div>`:''}
    <div class="job-actions">${['queued','running','processing'].includes(j.status)?'<button data-cancel>작업 취소</button>':''}${['failed','cancelled','interrupted'].includes(j.status)?'<button data-retry>다시 시도 ↻</button>':''}${j.files.length?j.files.map((f,i)=>`<a href="/api/jobs/${j.id}/files/${i}" download>${i===0?'파일 받기 ↓':e(f.name.split('.').pop().toUpperCase())+' ↓'}</a>`).join(''):''}<span class="helper">${new Date(j.created*1000).toLocaleString('ko-KR',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}</span></div>
    <details class="job-details" ${expandedJobs.has(j.id)?'open':''}><summary>${j.error?'오류 및 작업 정보':'저장 경로 및 작업 정보'}</summary>${j.error?`<pre>${e(j.error)}</pre>`:''}<p>${e(j.url)}</p>${j.files.map((f,i)=>`<div class="file-row"><a href="/api/jobs/${j.id}/files/${i}" download>${e(f.path)}</a><span>${bytes(f.size)}</span></div>`).join('')}<p>작업 ID · ${j.id}</p>${j.output_duration?`<p>검사된 재생 시간 · ${duration(j.output_duration)}</p>`:''}</details></article>`;
  }).join('');
  $$('[data-job]').forEach(node=>{
    const id=node.dataset.job;
    $('[data-cancel]',node)?.addEventListener('click',async()=>{try{await api(`/jobs/${id}/cancel`,'POST',{});await poll();}catch(err){toast(err.message);}});
    $('[data-retry]',node)?.addEventListener('click',async()=>{try{await api(`/jobs/${id}/retry`,'POST',{});await poll();}catch(err){toast(err.message);}});
    $('details',node).addEventListener('toggle',ev=>ev.target.open?expandedJobs.add(id):expandedJobs.delete(id));
  });
}
let polling=false;
async function poll(){
  if(polling)return;polling=true;
  try{
    const [state,newJobs]=await Promise.all([api('/state'),api('/jobs')]);token=state.token;jobs=newJobs;health=state.health;
    $('#stat-active').textContent=state.stats.active;$('#stat-queued').textContent=state.stats.queued;$('#stat-completed').textContent=state.stats.completed;
    $('#stat-bytes').textContent=bytes(state.stats.bytes);$('#nav-count').textContent=state.stats.active+state.stats.queued;$('#free-space').textContent=bytes(state.free_bytes);
    $('#connection').innerHTML='<span class="status-dot"></span>로컬 전용';$('#system-error').hidden=health.ready;
    if(!health.ready)$('#system-error').textContent='필수 도구 확인이 필요합니다. '+(health.error||'인코더 또는 필터가 없습니다.');
    renderJobs();
  }catch(err){$('#connection').textContent='연결 끊김';$('#system-error').hidden=false;$('#system-error').textContent='백엔드 연결이 끊겼습니다. start.cmd를 실행한 뒤 새로고침하세요. 기록은 디스크에 보관됩니다.';}
  finally{polling=false;}
}
async function boot(){
  try{
    const state=await api('/state');token=state.token;settings=state.settings;defaults=structuredClone(settings.defaults);health=state.health;
    applySettings(settings);updateDefaultKind();$('#tools-info').textContent=JSON.stringify(health,null,2);
    await poll();setInterval(poll,1500);
  }catch(err){$('#system-error').hidden=false;$('#system-error').textContent='앱을 시작할 수 없습니다: '+err.message;}
}
$('#inspect').onclick=()=>inspectLinks(true);
$('#urls').addEventListener('input',()=>{clearTimeout(inputTimer);inputTimer=setTimeout(()=>inspectLinks(),1000);});
$('#enqueue').onclick=enqueueSelected;
$('#select-all').onchange=ev=>{allItems().forEach(i=>i.selected=ev.target.checked);renderResults();};
for(const kind of ['video','audio'])$('#default-'+kind).onclick=()=>{if(defaults)rememberDefaults({...defaults,kind,subtitle_mode:kind==='audio'&&['embed','burn'].includes(defaults.subtitle_mode)?'srt':defaults.subtitle_mode});};
$('#settings-form').addEventListener('input',()=>{settingsDirty=true;$('#settings-message').textContent='저장하지 않은 변경 사항이 있습니다.';});
$('#settings-form').onsubmit=async ev=>{ev.preventDefault();try{await saveSettings();await poll();}catch(err){$('#settings-message').textContent=err.message;toast(err.message);}};
$$('[data-filter]').forEach(b=>b.onclick=()=>setFilter(b.dataset.filter,false));
$$('[data-view]').forEach(b=>b.onclick=()=>setFilter(b.dataset.view));
$('#settings-nav').onclick=()=>{$('#settings-card').scrollIntoView({behavior:'smooth'});$('#save-dir').focus({preventScroll:true});};
$('#tools-open').onclick=()=>{$('#tools-info').textContent=JSON.stringify(health,null,2);$('#tools-dialog').showModal();};
$('#tools-close').onclick=()=>$('#tools-dialog').close();
$('#mobile-tools-open').onclick=()=>{$('#tools-info').textContent=JSON.stringify(health,null,2);$('#tools-dialog').showModal();};
document.addEventListener('error',ev=>{if(ev.target instanceof HTMLImageElement)ev.target.hidden=true;},true);
boot();
