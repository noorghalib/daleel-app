'use strict';

// No sample records or browser-storage fallback. Null means not loaded; [] means loaded and empty.
const DATA_KEYS = ['transactions','ledger','files','reports','models','notifications','activity'];
const emptyData = () => ({profile:null,summary:null,...Object.fromEntries(DATA_KEYS.map(key=>[key,null]))});
const state = {route:'overview',filter:'all',query:'',page:1,period:'30',chart:'volume',
  connection:'disconnected',error:'',adapter:null,data:emptyData(),request:0,returnFocus:null,
  uploadFile:null,uploadKind:'transactions',busy:false};
const $ = selector => document.querySelector(selector);

function hydrateIcons(root=document){root.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon);});}
function toast(message){
  const el=document.createElement('div'); el.className='toast';
  el.innerHTML=`${icon('info')}<span>${escapeHTML(message)}</span><button aria-label="إغلاق الإشعار">${icon('close')}</button>`;
  el.querySelector('button').onclick=()=>el.remove(); $('#toast-region').append(el); setTimeout(()=>el.remove(),5000);
}
function dateLabel(){return new Intl.DateTimeFormat('ar-IQ',{weekday:'long',day:'numeric',month:'long',year:'numeric'}).format(new Date());}
function showNumber(value){return Number.isFinite(value)?number(value):'—';}
function showDate(value){
  if(!value)return '—'; const date=new Date(value);
  return Number.isNaN(date.getTime())?'—':new Intl.DateTimeFormat('ar-IQ',{dateStyle:'medium',timeStyle:'short'}).format(date);
}
function statusBadge(status){
  const names={normal:'طبيعي',review:'يحتاج مراجعة',high:'خطر مرتفع',reviewed:'تمت المراجعة',
    queued:'قيد الانتظار',processing:'قيد المعالجة',completed:'مكتمل',failed:'تعذّر الإكمال'};
  const colors={queued:'pending',processing:'info',completed:'normal',failed:'high'};
  if(!Object.hasOwn(names,status))return '<span class="status pending">غير مصنف</span>';
  return `<span class="status ${colors[status]||status}"><span class="dot"></span>${names[status]}</span>`;
}
function dataset(){return state.route==='ledger'?state.data.ledger:state.data.transactions;}
function pendingRecords(){return (state.data.transactions||[]).filter(r=>['review','high'].includes(r.status)&&!r.reviewed);}
function filteredRecords(){
  let rows=dataset()||[];
  if(state.route==='review')rows=rows.filter(r=>['review','high'].includes(r.status));
  if(state.filter==='reviewed')rows=rows.filter(r=>r.reviewed);
  else {if(state.route==='review')rows=rows.filter(r=>!r.reviewed);if(state.filter!=='all')rows=rows.filter(r=>r.status===state.filter&&!r.reviewed);}
  if(state.query.trim()){const q=state.query.trim().toLocaleLowerCase();rows=rows.filter(r=>[r.name,r.id,r.type,r.amount].some(v=>String(v??'').toLocaleLowerCase().includes(q)));}
  return rows;
}
function heading(){return `<section class="page-heading"><div><div class="eyebrow">${icon('calendar')}${dateLabel()}</div><h1>${titles[state.route]}</h1><p>${subtitles[state.route]}</p></div><div class="heading-actions">${['overview','transactions','ledger','review'].includes(state.route)?`<select class="date-select" id="period-select" aria-label="الفترة الزمنية"><option value="7" ${state.period==='7'?'selected':''}>آخر ٧ أيام</option><option value="30" ${state.period==='30'?'selected':''}>آخر ٣٠ يوماً</option><option value="90" ${state.period==='90'?'selected':''}>آخر ٩٠ يوماً</option></select>`:''}<button class="button primary" data-action="upload">${icon('plus')}تحليل ملف جديد</button></div></section>`;}
function dataNotice(){
  if(state.connection==='loading')return `<div class="section-intro loading-notice" role="status">${icon('clock')}جارٍ تحميل البيانات…</div>`;
  if(state.connection==='error')return `<div class="section-intro error-notice" role="alert">${icon('alert')}<div>تعذّر تحميل البيانات<p>${escapeHTML(state.error)}</p><button class="button small" data-action="refresh">إعادة المحاولة</button></div></div>`;
  return '';
}
function emptyState(entity,loaded=false){
  const disconnected=state.connection==='disconnected';
  const title=state.query?'لا توجد نتائج مطابقة':loaded?`لا توجد ${entity}`:state.connection==='loading'?'جارٍ تحميل البيانات':'لم يتم تحميل البيانات بعد';
  const message=state.query?'جرّب عبارة أخرى أو أعد تعيين الفلاتر.':disconnected?'ستظهر البيانات عند اتصال مساحة العمل بخدمة البيانات.':loaded?'ستظهر السجلات هنا بعد إضافتها إلى مساحة العمل.':'يمكنك إعادة المحاولة من زر التحديث.';
  return `<div class="empty-state">${icon(state.query?'search':'folder')}<h3>${title}</h3><p>${message}</p>${state.query?'<button class="button" data-action="reset-filters">إعادة تعيين الفلاتر</button>':''}</div>`;
}
function stats(){
  const s=state.data.summary;
  return `<section class="stats" aria-label="ملخص المعاملات">${[
    ['إجمالي المعاملات','total','arrows','navy'],['معاملات طبيعية','normal','shield','teal'],
    ['تحتاج إلى مراجعة','review','flag','amber'],['خطر مرتفع','high','alert','red']
  ].map(([label,key,i,color])=>`<article class="stat-card"><div class="stat-top"><span>${label}</span><span class="stat-icon ${color}">${icon(i)}</span></div><div class="stat-value">${showNumber(s?.[key])}</div><div class="stat-bottom"><span>${s?'حسب الفترة المحددة':'بانتظار تحميل البيانات'}</span></div></article>`).join('')}</section>`;
}
function activityChart(){
  const activity=state.data.activity;
  const alerts=state.chart==='alerts'; const key=alerts?'alerts':'transactions';
  let content=emptyState('بيانات للنشاط',Array.isArray(activity));
  if(activity?.length){
    const values=activity.map(p=>p[key]);const max=Math.max(1,...values);
    const points=values.map((v,i)=>`${values.length===1?300:600*i/(values.length-1)},${150-(v/max)*135}`).join(' ');
    content=`<div class="chart-layout"><div class="y-axis"><span>${number(max)}</span><span>${number(max/2)}</span><span>٠</span></div><div class="chart-stage"><svg class="chart-svg" viewBox="0 0 600 156" preserveAspectRatio="none" role="img" aria-label="نشاط ${alerts?'التنبيهات':'المعاملات'}"><path d="M0 0h600M0 78h600M0 156h600" stroke="var(--cloud)" fill="none"/><polyline class="chart-main-line" pathLength="1" points="${points}" fill="none" stroke="var(--teal)" stroke-width="2.5" vector-effect="non-scaling-stroke"/></svg><div class="chart-labels">${activity.filter((_,i)=>i%Math.max(1,Math.ceil(activity.length/7))===0).map(p=>`<span>${escapeHTML(p.label)}</span>`).join('')}</div></div></div>`;
  }
  return `<article class="panel chart-card"><div class="panel-head"><div><h2>نشاط المعاملات</h2><p class="panel-subtitle">عدد المعاملات المحللة حسب وقت التحليل</p></div><div class="segmented"><button data-action="chart" data-value="volume" class="${!alerts?'active':''}" aria-pressed="${!alerts}">المعاملات</button><button data-action="chart" data-value="alerts" class="${alerts?'active':''}" aria-pressed="${alerts}">التنبيهات</button></div></div>${content}</article>`;
}
function riskCard(){
  const s=state.data.summary; const total=s?[s.normal,s.review,s.high].reduce((a,b)=>a+b,0):0;
  if(!s||!total)return `<article class="panel risk-card risk-empty"><div class="panel-head"><div><h2>توزيع الحالات</h2><p class="panel-subtitle">تصنيف المعاملات حسب حالة المراجعة</p></div></div><div class="empty-ring" aria-hidden="true"></div><p class="empty-chart-label">${s?'لا توجد معاملات لعرض توزيعها':'بانتظار تحميل البيانات'}</p></article>`;
  const normal=s.normal/total*100,review=s.review/total*100;
  return `<article class="panel risk-card"><div class="panel-head"><h2>توزيع الحالات</h2></div><div class="donut-area"><div class="donut" style="background:conic-gradient(var(--green) 0 ${normal}%,var(--amber) ${normal}% ${normal+review}%,var(--red) ${normal+review}% 100%)" aria-hidden="true"></div><div class="donut-inner"><span>معاملات طبيعية</span><strong>${decimal(normal)}٪</strong></div></div><div class="risk-legend">${[['normal','طبيعي',''],['review','يحتاج مراجعة','amber'],['high','خطر مرتفع','red']].map(([k,label,c])=>`<div class="risk-row"><span class="dot ${c}"></span>${label}<strong>${decimal(s[k]/total*100)}٪</strong><small>${number(s[k])}</small></div>`).join('')}</div></article>`;
}
function insight(){
  const loaded=Array.isArray(state.data.transactions),count=pendingRecords().length;
  return `<section class="insight"><div class="insight-mark">${icon('scan')}</div><div class="insight-copy"><div class="insight-title">مركز المراجعة <span class="ai-tag">AI</span></div><p>${!loaded?'تظهر أولويات المراجعة بعد تحميل المعاملات ونتائج تحليلها.':count?`لديك <b>${number(count)} حالات</b> بانتظار المراجعة في السجلات المحمّلة.`:'لا توجد حالات معلّقة في السجلات المحمّلة.'}</p></div><a href="#review">قائمة المراجعة ${icon('arrow-left')}</a></section>`;
}
function tabs(){
  const rows=dataset();
  return [['all','الكل'],...(state.route==='review'?[]:[['normal','طبيعي']]),['review','يحتاج مراجعة'],['high','خطر مرتفع'],['reviewed','تمت المراجعة']].map(([value,label])=>`<button class="filter-tab ${state.filter===value?'active':''}" data-action="filter" data-value="${value}" aria-pressed="${state.filter===value}">${label}${rows?`<span class="tab-count">${number(value==='all'?rows.length:rows.filter(r=>value==='reviewed'?r.reviewed:r.status===value&&!r.reviewed).length)}</span>`:''}</button>`).join('');
}
function tablePanel(overview=false){
  const ledger=state.route==='ledger',source=dataset();
  const rows=overview?(state.data.transactions||[]).slice(0,5):filteredRecords();const size=6;
  const pages=Math.max(1,Math.ceil(rows.length/size));state.page=Math.min(state.page,pages);
  const visible=overview?rows:rows.slice((state.page-1)*size,state.page*size);
  const title=overview?'أحدث المعاملات':ledger?'سجل القيود المحاسبية':state.route==='review'?'حالات المراجعة':'سجل المعاملات';
  return `<section class="panel table-panel"><div class="table-heading"><div class="table-heading-title"><h2>${title}</h2>${source?`<span class="label">${number(rows.length)} سجل</span>`:''}</div>${overview?`<a class="text-link" href="#transactions">كل المعاملات ${icon('arrow-left')}</a>`:`<button class="button small" data-action="export" ${!rows.length?'disabled':''}>${icon('download')}تصدير CSV</button>`}</div>${!overview?`<div class="table-tools"><div class="filter-tabs">${tabs()}</div><label class="mini-search">${icon('search')}<input id="table-search" aria-label="بحث في السجلات" placeholder="ابحث في السجلات..." value="${escapeHTML(state.query)}"></label></div>`:''}<div class="table-wrap"><table><thead><tr><th>${ledger?'القيد المحاسبي':'المعاملة / المستفيد'}</th><th>المبلغ</th><th>${ledger?'القطاع':'نوع العملية'}</th>${!overview?'<th>درجة النموذج</th>':''}<th>الحالة</th><th>تاريخ التسجيل</th><th><span class="visually-hidden">تفاصيل</span></th></tr></thead><tbody>${visible.map(r=>`<tr><td><div class="record-cell"><span class="record-avatar">${icon(ledger?'book':'arrows')}</span><div><strong>${escapeHTML(r.name||r.id)}</strong><small dir="ltr">${escapeHTML(r.id)}</small></div></div></td><td class="amount number">${showNumber(r.amount)} <small>${escapeHTML(r.currency||'')}</small></td><td>${escapeHTML((ledger?r.sector:r.type)||'—')}</td>${!overview?`<td class="number">${Number.isFinite(r.score)?decimal(r.score):'—'}</td>`:''}<td>${statusBadge(r.reviewed?'reviewed':r.status)}</td><td class="muted">${showDate(r.date)}</td><td><button class="review-button" data-action="detail" data-id="${escapeHTML(r.id)}" aria-label="تفاصيل ${escapeHTML(r.id)}">${icon('chevron-left')}</button></td></tr>`).join('')}</tbody></table></div>${!visible.length?emptyState(ledger?'قيود محاسبية':'معاملات',Array.isArray(source)):''}<div class="table-footer"><span>${source?`عرض ${number(visible.length)} من ${number(rows.length)} سجل`:'لم يتم تحميل السجلات'}</span>${!overview&&rows.length?`<div class="pagination"><button data-action="page" data-value="${state.page-1}" aria-label="الصفحة السابقة" ${state.page===1?'disabled':''}>${icon('chevron-right')}</button><span class="page-number">${number(state.page)} / ${number(pages)}</span><button data-action="page" data-value="${state.page+1}" aria-label="الصفحة التالية" ${state.page===pages?'disabled':''}>${icon('chevron-left')}</button></div>`:''}</div></section>`;
}
function filesPage(){
  const files=state.data.files;
  return `<div class="upload-strip"><div class="upload-icon">${icon('upload')}</div><div><h3>ملفك هو نقطة البداية</h3><p>أضف ملف CSV لبدء التحليل.</p></div><button class="button primary" data-action="upload">${icon('plus')}رفع ملف جديد</button></div><section class="panel table-panel"><div class="table-heading"><h2>مكتبة الملفات</h2><button class="button small" data-action="template">${icon('download')}قالب المعاملات</button></div>${files?.length?`<div class="table-wrap"><table><thead><tr><th>اسم الملف</th><th>نوع التحليل</th><th>الحجم</th><th>الحالة</th><th>تاريخ الإضافة</th></tr></thead><tbody>${files.map(f=>`<tr><td><div class="record-cell"><span class="file-icon">${icon('file')}</span><strong>${escapeHTML(f.name)}</strong></div></td><td>${f.kind==='ledger'?'قيود محاسبية':'معاملات مالية'}</td><td>${formatBytes(f.size)}</td><td>${statusBadge(f.status)}</td><td>${showDate(f.createdAt)}</td></tr>`).join('')}</tbody></table></div>`:emptyState('ملفات',Array.isArray(files))}</section>`;
}
function reportsPage(){
  const reports=state.data.reports;
  if(!reports?.length)return `<section class="panel table-panel"><div class="table-heading"><h2>التقارير المتاحة</h2></div>${emptyState('تقارير',Array.isArray(reports))}</section>`;
  return `<section class="report-grid">${reports.map(r=>`<article class="panel report-card"><div class="report-top">${icon('chart')}</div><h3>${escapeHTML(r.title)}</h3><p>${escapeHTML(r.description||'')}</p><div class="report-card-footer"><span>${showDate(r.createdAt)}</span><button class="button small" data-action="report" data-id="${escapeHTML(r.id)}" ${!state.adapter?.downloadReport?'disabled':''}>${icon('download')}تنزيل</button></div></article>`).join('')}</section>`;
}
function modelsPage(){
  const models=state.data.models;
  if(!models?.length)return `<section class="panel table-panel"><div class="table-heading"><h2>نماذج التحليل</h2></div>${emptyState('نماذج متاحة',Array.isArray(models))}</section>`;
  return `<section class="subgrid">${models.map(m=>`<article class="panel model-card"><div class="model-header"><div class="model-symbol">${icon('cpu')}</div>${m.status?`<span class="label">${escapeHTML(m.status)}</span>`:''}</div><h2>${escapeHTML(m.name)}</h2><p class="model-family">${escapeHTML(m.family||'')}</p><p>${escapeHTML(m.description||'')}</p><div class="model-facts">${Object.entries(m.metrics||{}).map(([k,v])=>`<div><span>${escapeHTML(k)}</span><strong>${escapeHTML(v)}</strong></div>`).join('')}</div><div class="model-card-footer"><small>إصدار النموذج</small><span>${escapeHTML(m.version||'—')}</span></div></article>`).join('')}</section>`;
}
function settingsPage(){
  const p=state.data.profile;
  return `<div class="settings-layout"><aside class="panel settings-nav"><p>الحساب ومساحة العمل</p></aside><form class="panel settings-form" id="settings-form"><h2>معلومات الحساب</h2><p>${p?'إدارة بيانات حسابك ومساحة العمل.':'ستظهر معلومات الحساب بعد تحميلها.'}</p><div class="form-grid"><label class="field"><span>الاسم الكامل</span><input name="name" value="${escapeHTML(p?.name||'')}" required maxlength="60" autocomplete="name" ${!p?'disabled':''}></label><label class="field"><span>اسم مساحة العمل</span><input name="company" value="${escapeHTML(p?.company||'')}" required maxlength="60" autocomplete="organization" ${!p?'disabled':''}></label><label class="field"><span>البريد الإلكتروني</span><input type="email" name="email" value="${escapeHTML(p?.email||'')}" required maxlength="120" autocomplete="email" dir="ltr" ${!p?'disabled':''}></label><label class="field"><span>لغة الواجهة</span><select disabled><option>العربية — العراق</option></select></label></div><div class="form-actions"><button type="submit" class="button primary" ${!p||!state.adapter?.saveProfile?'disabled':''}>${icon('check')}حفظ التغييرات</button></div></form></div>`;
}
function updateChrome(){
  $('#sidebar-company').textContent=state.data.profile?.company||'مساحة العمل';
  const name=state.data.profile?.name;$('#profile-button').innerHTML=name?escapeHTML(name.trim().split(/\s+/).slice(0,2).map(s=>s[0]).join('')):icon('building');
  const badge=$('#review-count');badge.hidden=!Array.isArray(state.data.transactions)||!pendingRecords().length;badge.textContent=badge.hidden?'':number(pendingRecords().length);
  const labels={disconnected:'غير متصل',loading:'جارٍ التحميل',ready:'متصل',error:'تعذّر الاتصال'};
  $('#connection-indicator').innerHTML=`<i class="${state.connection==='ready'?'connected':''}"></i><span>${labels[state.connection]}</span>`;
  const notifications=state.data.notifications;
  $('#notification-content').innerHTML=notifications?.length?notifications.map(n=>`<div class="notification-item"><span class="notification-icon">${icon('info')}</span><div><strong>${escapeHTML(n.title)}</strong><p>${escapeHTML(n.message||'')}</p></div></div>`).join(''):`<p class="muted">${notifications?'لا توجد إشعارات.':'لم يتم تحميل الإشعارات بعد.'}</p>`;
  $('#copyright').textContent=`© ${new Intl.NumberFormat('ar-IQ',{useGrouping:false}).format(new Date().getFullYear())} دليل`;
}
function render(){
  document.title=`Daleel AI — ${titles[state.route]}`;$('#breadcrumb-name').textContent=titles[state.route];
  document.querySelectorAll('[data-route]').forEach(a=>{const active=a.dataset.route===state.route;a.classList.toggle('active',active);a.toggleAttribute('aria-current',active);if(active)a.setAttribute('aria-current','page');});
  let content=heading()+dataNotice();
  if(state.route==='overview')content+=stats()+`<div class="analytics-grid">${activityChart()}${riskCard()}</div>`+insight()+tablePanel(true);
  else if(['transactions','ledger','review'].includes(state.route))content+=tablePanel();
  else if(state.route==='files')content+=filesPage();else if(state.route==='reports')content+=reportsPage();
  else if(state.route==='models')content+=modelsPage();else content+=settingsPage();
  $('#main').innerHTML=content;hydrateIcons($('#main'));document.querySelectorAll('.table-wrap').forEach(el=>{el.scrollLeft=el.scrollWidth;});updateChrome();
  $('#period-select')?.addEventListener('change',e=>{state.period=e.target.value;state.page=1;if(state.adapter)refresh();else render();});
  $('#table-search')?.addEventListener('input',e=>{const cursor=e.target.selectionStart;state.query=e.target.value;state.page=1;render();const input=$('#table-search');input.focus();input.setSelectionRange(cursor,cursor);});
  $('#settings-form')?.addEventListener('submit',saveProfile);
}
function navigate(){if(location.hash==='#main'){$('#main').focus();return;}const route=location.hash.slice(1)||'overview';state.route=Object.hasOwn(titles,route)?route:'overview';state.filter='all';state.query='';state.page=1;$('#global-search').value='';closeModal(false);closeMobile();render();window.scrollTo({top:0,behavior:'instant'});}
function openModal(content,drawer=false){state.returnFocus=document.activeElement;$('#dialog').classList.toggle('drawer',drawer);$('#dialog').innerHTML=content;$('#dialog').hidden=false;$('#modal-overlay').hidden=false;document.body.style.overflow='hidden';hydrateIcons($('#dialog'));setTimeout(()=>$('#dialog').querySelector('button,input,select,textarea,a')?.focus(),30);}
function closeModal(restore=true){if(state.busy)return;$('#dialog').hidden=true;$('#modal-overlay').hidden=true;document.body.style.overflow='';if(restore)state.returnFocus?.focus();}
function modalHeader(title,subtitle=''){return `<div class="dialog-header"><div><h2 id="dialog-title">${title}</h2><p>${escapeHTML(subtitle)}</p></div><button class="icon-button" data-action="close" aria-label="إغلاق النافذة">${icon('close')}</button></div>`;}
function formatBytes(size){if(!Number.isFinite(size))return '—';return size>=1024*1024?`${decimal(size/(1024*1024))} MB`:`${number(Math.ceil(size/1024))} KB`;}
function openUpload(kind=state.route==='ledger'?'ledger':'transactions'){
  state.uploadFile=null;state.uploadFiles={};state.uploadKind=kind;
  const ledger=kind==='ledger';
  openModal(modalHeader('ابدأ تحليلاً جديداً','CSV بترميز UTF-8. كل عملية رفع تحفظ تحليلاً جديداً.')+`<div class="upload-types">${[['transactions','معاملات مالية','arrows'],['ledger','قيود محاسبية','book']].map(([k,label,i])=>`<button class="upload-type ${kind===k?'selected':''}" data-action="upload-kind" data-value="${k}" aria-pressed="${kind===k}">${icon(i)}<strong>${label}</strong></button>`).join('')}</div>${ledger?`<div class="form-grid">${[['headers','رؤوس القيود — ledger_headers.csv'],['lines','بنود القيود — ledger_lines.csv'],['chart_of_accounts','دليل الحسابات — ledger_accounts.csv']].map(([key,label])=>`<label class="field"><span>${label}</span><input type="file" data-ledger-file="${key}" accept=".csv,text/csv"></label>`).join('')}</div><label class="field"><span>القطاع</span><select id="sector-select"><option value="">اختر القطاع</option><option value="energy">الطاقة</option><option value="healthcare">الرعاية الصحية</option><option value="luxurygoods">السلع الفاخرة</option><option value="manufacturing">التصنيع</option><option value="transport">النقل</option></select></label><p class="dialog-note">للأمثلة المرفقة اختر الطاقة. حد 1000 رأس و10000 بند و10000 حساب. تُحدد أعلى 1% للمراجعة مع التقريب للأعلى؛ ليست إثبات احتيال.</p>`:`<label class="field"><span>ملف المعاملات — transactions.csv</span><input type="file" id="file-input" accept=".csv,text/csv"></label><p class="dialog-note">حتى 10000 معاملة.</p>`}<p class="dialog-note">إجمالي الطلب حتى 10 MiB. ملفات Excel غير مدعومة حالياً.</p><div id="file-feedback" role="status" aria-live="polite"></div><div class="dialog-actions"><button class="button ghost" data-action="close">إلغاء</button><button class="button primary" id="upload-button" data-action="submit-upload" disabled>تحليل وحفظ</button></div>`);
  $('#file-input')?.addEventListener('change',e=>selectFile(e.target.files[0]));
  $('#sector-select')?.addEventListener('change',updateUploadButton);
  document.querySelectorAll('[data-ledger-file]').forEach(input=>input.addEventListener('change',()=>{
    const file=input.files[0];
    if(file&&(!/\.csv$/i.test(file.name)||!file.size)){input.value='';delete state.uploadFiles[input.dataset.ledgerFile];fileError('اختر ملف CSV غير فارغ.');return;}
    state.uploadFiles[input.dataset.ledgerFile]=file;$('#file-feedback').textContent='';updateUploadButton();
  }));
}
function updateUploadButton(){
 const ledger=state.uploadKind==='ledger';
 const files=ledger?Object.values(state.uploadFiles||{}).filter(Boolean):[state.uploadFile].filter(Boolean);
 const ready=ledger?['headers','lines','chart_of_accounts'].every(k=>state.uploadFiles[k])&&$('#sector-select')?.value:!!state.uploadFile;
 const tooLarge=files.reduce((sum,file)=>sum+file.size,0)>10*1024*1024;
 if(tooLarge)$('#file-feedback').textContent='إجمالي الملفات يتجاوز 10 MiB.';
 $('#upload-button').disabled=state.busy||!ready||!state.adapter?.uploadFile||tooLarge;
}
function fileError(message){$('#file-feedback').textContent=message;updateUploadButton();}
function selectFile(file){
 state.uploadFile=null;
 if(!file){updateUploadButton();return;}
 if(!/\.csv$/i.test(file.name)||!file.size){fileError('اختر ملف CSV غير فارغ.');return;}
 state.uploadFile=file;$('#file-feedback').textContent=file.name;updateUploadButton();
}
async function submitUpload(){
 if(!state.adapter?.uploadFile||state.busy||$('#upload-button').disabled)return;
 const adapter=state.adapter;state.busy=true;updateUploadButton();
 try{
  await adapter.uploadFile({file:state.uploadFile,files:state.uploadFiles,kind:state.uploadKind,sector:$('#sector-select')?.value||null});
  state.busy=false;closeModal();location.hash=state.uploadKind==='ledger'?'ledger':'transactions';
  await refresh();toast('اكتمل التحليل وحُفظت النتائج.');
 }catch(error){state.busy=false;fileError(error instanceof Error?error.message:'تعذّر التحليل.');}
}
function findRecord(id){return [...(state.data.transactions||[]),...(state.data.ledger||[])].find(r=>r.id===id);}
function openDetail(id){
  const r=findRecord(id);if(!r)return;
  openModal(modalHeader('تفاصيل السجل',r.id)+statusBadge(r.reviewed?'reviewed':r.status)+`<div class="detail-amount"><span>المبلغ المسجل</span><strong>${showNumber(r.amount)} <small>${escapeHTML(r.currency||'')}</small></strong></div><dl class="detail-meta"><div><dt>الوصف / المستفيد</dt><dd>${escapeHTML(r.name||'—')}</dd></div><div><dt>نوع العملية</dt><dd>${escapeHTML(r.type||'—')}</dd></div><div><dt>تاريخ التسجيل</dt><dd>${showDate(r.date)}</dd></div><div><dt>درجة النموذج</dt><dd>${Number.isFinite(r.score)?decimal(r.score):'—'}</dd></div></dl><div class="evidence-box"><div class="evidence-heading">${icon('flag')}ملاحظات التحليل</div><p>${escapeHTML(r.reason||'لا توجد ملاحظات محمّلة لهذا السجل.')}</p><small class="score-caption">درجة النموذج ليست نسبة ثقة مضمونة، وأسباب القواعد مستقلة عن قرار النموذج.</small></div>${r.reviewed?`<div class="review-confirmation">${icon('check-circle')}تمت مراجعة السجل</div><p>${escapeHTML(r.reviewNote||'')}</p>`:`<label class="field"><span>ملاحظة المراجع</span><textarea id="review-note" maxlength="500" ${!state.adapter?.reviewRecord?'disabled':''}></textarea></label>`}<div id="review-error" role="alert"></div><div class="dialog-actions"><button class="button ghost" data-action="close">إغلاق</button><button class="button primary" data-action="review-record" data-id="${escapeHTML(r.id)}" data-value="${r.reviewed?'reopen':'review'}" ${!state.adapter?.reviewRecord?'disabled':''}>${r.reviewed?'إعادة إلى المراجعة':'تأكيد المراجعة'}</button></div>`,true);
}
async function reviewRecord(id,reopen){
  if(!state.adapter?.reviewRecord||state.busy)return;const r=findRecord(id);if(!r)return;
  state.busy=true;const controls=[...$('#dialog').querySelectorAll('button')];controls.forEach(b=>b.disabled=true);
  try {await state.adapter.reviewRecord({id,kind:(state.data.ledger||[]).includes(r)?'ledger':'transactions',reviewed:!reopen,note:$('#review-note')?.value.trim()||''});state.busy=false;closeModal();await refresh();toast('تم حفظ قرار المراجعة.');}
  catch(error){state.busy=false;controls.forEach(b=>b.disabled=false);$('#review-error').textContent=error instanceof Error?error.message:'تعذّر حفظ المراجعة.';}
}
async function saveProfile(e){
  e.preventDefault();if(!state.adapter?.saveProfile)return;
  const form=e.target,button=form.querySelector('[type="submit"]'),fields=new FormData(form);
  const profile={name:fields.get('name').trim(),company:fields.get('company').trim(),email:fields.get('email').trim()};
  if(!profile.name||!profile.company){toast('أدخل الاسم واسم مساحة العمل.');return;}
  button.disabled=true;
  try {await state.adapter.saveProfile(profile);await refresh();toast('تم حفظ بيانات الحساب.');}
  catch(error){toast(error instanceof Error?error.message:'تعذّر حفظ بيانات الحساب.');button.disabled=false;}
}
function downloadBlob(filename,blob){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function downloadCSV(filename,rows){const cell=v=>{let s=String(v??'');if(/^[=+@-]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';};downloadBlob(filename,new Blob(['\ufeff'+rows.map(r=>r.map(cell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}));}
function exportRecords(){const rows=filteredRecords();if(!rows.length)return;downloadCSV(`daleel-${state.route}.csv`,[['record_id','name','amount','currency','type','status','model_score','reviewed','review_note'],...rows.map(r=>[r.id,r.name,r.amount,r.currency,r.type,r.status,r.score,r.reviewed,r.reviewNote])]);toast('تم تصدير السجلات المحمّلة.');}
async function downloadReport(id){if(!state.adapter?.downloadReport)return;try{const result=await state.adapter.downloadReport({id});if(!(result?.blob instanceof Blob)||!result.filename)throw new Error('تعذّر استلام ملف التقرير.');downloadBlob(result.filename,result.blob);}catch(error){toast(error instanceof Error?error.message:'تعذّر تنزيل التقرير.');}}

function normalizeData(value){
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('استجابة بيانات غير صالحة.');
  const next=emptyData();
  for(const key of DATA_KEYS){if(value[key]!=null){if(!Array.isArray(value[key]))throw new Error(`استجابة ${key} يجب أن تكون قائمة.`);next[key]=value[key];}}
  if(value.profile!=null){if(typeof value.profile!=='object'||Array.isArray(value.profile))throw new Error('استجابة الحساب غير صالحة.');next.profile=value.profile;}
  if(value.summary!=null){if(typeof value.summary!=='object'||['total','normal','review','high'].some(k=>!Number.isSafeInteger(value.summary[k])||value.summary[k]<0))throw new Error('ملخص المعاملات غير صالح.');next.summary=value.summary;}
  for(const key of ['transactions','ledger','files','reports','models']){
    if(next[key]){const ids=new Set();for(const r of next[key]){if(!r||typeof r!=='object'||typeof r.id!=='string'||!r.id||ids.has(r.id))throw new Error(`معرّفات ${key} مفقودة أو مكررة.`);ids.add(r.id);}}
  }
  if(next.activity?.some(p=>!p||typeof p.label!=='string'||!Number.isFinite(p.transactions)||p.transactions<0||!Number.isFinite(p.alerts)||p.alerts<0))throw new Error('بيانات الرسم غير صالحة.');
  return next;
}
async function refresh(){
  if(!state.adapter)return;const request=++state.request,adapter=state.adapter;
  state.connection='loading';state.error='';state.data=emptyData();render();
  try {const data=normalizeData(await adapter.loadWorkspace({periodDays:Number(state.period)}));if(request!==state.request||adapter!==state.adapter)return;state.data=data;state.connection='ready';render();}
  catch(error){if(request!==state.request||adapter!==state.adapter)return;state.connection='error';state.error=error instanceof Error?error.message:'تعذّر الاتصال بخدمة البيانات.';state.data=emptyData();render();}
}
window.Daleel = Object.freeze({
  connect(adapter){if(!adapter||typeof adapter.loadWorkspace!=='function')throw new TypeError('An adapter with loadWorkspace() is required.');state.adapter=adapter;return refresh();},
  refresh,
  disconnect(){state.request++;state.adapter=null;state.data=emptyData();state.connection='disconnected';state.error='';closeModal();render();}
});

function syncMobileAccess(){const closed=matchMedia('(max-width:1040px)').matches&&!$('.sidebar').classList.contains('mobile-open');$('.sidebar').inert=closed;$('.sidebar').setAttribute('aria-hidden',String(closed));}
function closeMobile(){$('.sidebar').classList.remove('mobile-open');$('#mobile-scrim').classList.remove('visible');$('#mobile-toggle').setAttribute('aria-expanded','false');syncMobileAccess();}
document.addEventListener('click',e=>{
  const target=e.target.closest('[data-action]');if(!target||target.disabled)return;const action=target.dataset.action,value=target.dataset.value;
  if(action==='upload')openUpload();else if(action==='close')closeModal();else if(action==='refresh')refresh();
  else if(action==='filter'){state.filter=value;state.page=1;render();}else if(action==='page'){state.page=Number(value);render();}
  else if(action==='chart'){state.chart=value;render();}else if(action==='detail')openDetail(target.dataset.id);
  else if(action==='export')exportRecords();else if(action==='report')downloadReport(target.dataset.id);
  else if(action==='reset-filters'){state.query='';state.filter='all';state.page=1;$('#global-search').value='';render();}
  else if(action==='upload-kind'){openUpload(value);}
  else if(action==='submit-upload')submitUpload();else if(action==='review-record')reviewRecord(target.dataset.id,value==='reopen');
  else if(action==='template')downloadCSV('daleel-transactions-template.csv',[['record_id','step','type','amount','oldbalanceOrg','newbalanceOrig','oldbalanceDest','newbalanceDest']]);
});
$('#modal-overlay').addEventListener('click',()=>closeModal());
$('#notifications-button').addEventListener('click',()=>{const open=$('#notifications').hidden;$('#notifications').hidden=!open;$('#notifications-button').setAttribute('aria-expanded',String(open));});
document.addEventListener('click',e=>{if(!e.target.closest('.notification-anchor')){$('#notifications').hidden=true;$('#notifications-button').setAttribute('aria-expanded','false');}});
$('#profile-button').addEventListener('click',()=>location.hash='settings');$('#workspace-switch').addEventListener('click',()=>location.hash='settings');
$('#mobile-toggle').addEventListener('click',()=>{const open=!$('.sidebar').classList.contains('mobile-open');$('.sidebar').classList.toggle('mobile-open',open);$('#mobile-scrim').classList.toggle('visible',open);$('#mobile-toggle').setAttribute('aria-expanded',String(open));syncMobileAccess();});
$('#mobile-scrim').addEventListener('click',closeMobile);
$('#help-button').addEventListener('click',()=>openModal(modalHeader('دليل الاستخدام')+`<div class="help-content"><h3>المعاملات والقيود</h3><p>استخدم البحث وحالة السجل للوصول إلى المعلومات، وافتح التفاصيل لقراءة أسباب التنبيه.</p><h3>المراجعة</h3><p>تحقق من المستندات قبل اعتماد القرار. أسباب القواعد مستقلة عن درجة النموذج.</p><h3>الملفات والتقارير</h3><p>ارفع ملفاً من مكتبة الملفات، وتابع حالة معالجته، ثم نزّل التقارير المتاحة لمساحة عملك.</p><h3>البحث السريع</h3><p>اضغط Ctrl + K أو ⌘ + K للانتقال إلى البحث.</p></div><div class="dialog-actions"><span></span><button class="button primary" data-action="close">إغلاق</button></div>`));
let searchTimer;
$('#global-search').addEventListener('input',e=>{clearTimeout(searchTimer);const value=e.target.value;searchTimer=setTimeout(()=>{if(state.route!=='transactions'){state.route='transactions';state.filter='all';history.replaceState(null,'','#transactions');}state.query=value;state.page=1;render();},180);});
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();closeModal(false);$('#global-search').focus();}
  if(e.key==='Escape'){closeModal();closeMobile();$('#notifications').hidden=true;$('#notifications-button').setAttribute('aria-expanded','false');}
  if(e.key==='Tab'&&!$('#dialog').hidden){const list=[...$('#dialog').querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href]')].filter(el=>el.getClientRects().length);const first=list[0],last=list.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}
});
matchMedia('(max-width:1040px)').addEventListener('change',syncMobileAccess);
window.addEventListener('resize',()=>document.querySelectorAll('.table-wrap').forEach(el=>{el.scrollLeft=el.scrollWidth;}));
window.addEventListener('hashchange',navigate);
hydrateIcons();navigate();
