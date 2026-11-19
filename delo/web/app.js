'use strict';
const $ = id => document.getElementById(id);
let meta, currentDocument, loadNumber = 0, toastTimer;
const actionLabels = {registered:'Документ зарегистрирован', edited:'Реквизиты исправлены',
  assigned:'Назначен исполнитель', completed:'Исполнение завершено', archived:'Передан в архив'};
function element(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else if (key === 'value') node.value = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child instanceof Node ? child : document.createTextNode(child));
  return node;
}
function user() { return meta.users.find(u => u.id === Number($('user').value)); }
function message(id, text = '') { $(id).textContent = text; $(id).hidden = !text; }
function toast(text) { clearTimeout(toastTimer); message('toast', text); toastTimer = setTimeout(() => message('toast'), 3500); }
async function api(path, options = {}) {
  const headers = {'X-Demo-User':$('user').value, ...(options.headers || {})};
  if (options.body !== undefined) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(options.body); }
  const response = await fetch(path, {...options, headers});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Не удалось выполнить запрос');
  return result;
}
function filters() {
  const query = new URLSearchParams();
  for (const [key,id] of [['q','search'],['kind','kind'],['status','status'],['from','dateFrom'],['to','dateTo'],['as_of','asOf']]) {
    if ($(id).value) query.set(key,$(id).value);
  }
  if ($('overdue').checked) query.set('overdue','1');
  return query.toString();
}
function prettyDate(value) { return value ? value.split('-').reverse().join('.') : '—'; }
function statusBadge(status) { return element('span', {class:'status '+status, text:meta.statuses[status]}); }
async function loadDocuments() {
  const number = ++loadNumber;
  message('error');
  try {
    const result = await api('/api/documents?'+filters());
    if (number !== loadNumber) return;
    $('rows').replaceChildren();
    for (const doc of result.documents) {
      const title = element('td',{},[
        element('div',{class:'doc-number',text:doc.number}),
        element('div',{class:'doc-title',text:doc.title}),
        element('div',{class:'doc-type',text:meta.kinds[doc.kind]})]);
      const due = element('td',{class:'due'+(doc.is_overdue?' overdue':'')},[prettyDate(doc.due_on)]);
      if (doc.is_overdue) due.append(element('span',{class:'overdue-label',text:'Просрочено'}));
      const open = element('button',{class:'open-doc','aria-label':'Открыть '+doc.number,text:'↗'});
      open.addEventListener('click',() => openDocument(doc.id));
      $('rows').append(element('tr',{'data-id':String(doc.id)},[title,
        element('td',{text:doc.correspondent}),element('td',{},[statusBadge(doc.status)]),
        element('td',{text:doc.assignee_name || 'Не назначен'}),due,element('td',{},[open])]));
    }
    for (const [id,key] of [['statTotal','total'],['statActive','active'],['statOverdue','overdue'],['statArchived','archived']]) $(id).textContent = result.stats[key];
    $('listCount').textContent = result.stats.total;
    $('navCount').textContent = result.stats.total;
    $('empty').hidden = result.stats.total !== 0;
    $('tableSummary').textContent = `Найдено: ${result.stats.total} · контроль на ${prettyDate(result.as_of)}`;
  } catch (error) {
    if (number !== loadNumber) return;
    $('rows').replaceChildren(); $('empty').hidden = true;
    for (const id of ['statTotal','statActive','statOverdue','statArchived','navCount','listCount']) $(id).textContent = '—';
    $('tableSummary').textContent = 'Отбор не выполнен'; message('error',error.message);
  }
}
function labelled(label, input) { return element('label',{},[label,input]); }
function actionForm(title, fields, buttonLabel, action) {
  const button = element('button',{class:'primary',type:'submit',text:buttonLabel});
  const form = element('form',{class:'action-form'},[element('h3',{text:title}),...fields,button]);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    form.querySelectorAll('button').forEach(b => b.disabled=true); message('detailError');
    try { await action(new FormData(form)); await loadDocuments(); await openDocument(currentDocument.id); toast('Изменения сохранены'); }
    catch (error) { message('detailError',error.message); }
    finally { form.querySelectorAll('button').forEach(b => b.disabled=false); }
  });
  return form;
}
function buildActions(doc) {
  $('detailActions').replaceChildren();
  const person = user(), fields = [];
  if (person.role === 'registrar' && doc.status === 'registered') {
    fields.push(labelled('Тема',element('input',{name:'title',value:doc.title,required:'',maxlength:'200'})),
      labelled('Корреспондент',element('input',{name:'correspondent',value:doc.correspondent,required:'',maxlength:'200'})),
      labelled('Срок исполнения',element('input',{name:'due_on',type:'date',value:doc.due_on,required:''})));
    $('detailActions').append(actionForm('Исправить реквизиты',fields,'Сохранить реквизиты',data => api('/api/documents/'+doc.id,
      {method:'PATCH',body:{version:doc.version,title:data.get('title'),correspondent:data.get('correspondent'),due_on:data.get('due_on')}})));
  }
  if (person.role === 'manager' && ['registered','in_progress'].includes(doc.status)) {
    const select = element('select',{name:'assignee_id',required:'','aria-label':'Назначить исполнителя'});
    for (const executor of meta.users.filter(u => u.role === 'executor' && u.id !== doc.assignee_id)) select.append(element('option',{value:executor.id,text:executor.display_name}));
    $('detailActions').append(actionForm('Маршрутизация документа',[labelled('Исполнитель',select)],'Назначить исполнителя',data => api('/api/documents/'+doc.id+'/assign',
      {method:'POST',body:{version:doc.version,assignee_id:Number(data.get('assignee_id'))}})));
  }
  if (person.role === 'executor' && doc.assignee_id === person.id && doc.status === 'in_progress') {
    $('detailActions').append(actionForm('Результат исполнения',[labelled('Что выполнено',element('textarea',{name:'result',required:'',maxlength:'2000',placeholder:'Опишите результат учебного поручения'}))],
      'Завершить исполнение',data => api('/api/documents/'+doc.id+'/complete',{method:'POST',body:{version:doc.version,result:data.get('result')}})));
  }
  if (person.role === 'manager' && doc.status === 'completed') {
    $('detailActions').append(actionForm('Документ исполнен и готов к передаче',[],'Передать в архив',() => api('/api/documents/'+doc.id+'/archive',
      {method:'POST',body:{version:doc.version}})));
  }
  if (!$('detailActions').childElementCount) $('detailActions').append(element('p',{class:'read-only',text:'Для выбранной роли и состояния доступен просмотр карточки и истории.'}));
  const refresh = element('button',{class:'secondary',text:'Обновить карточку',type:'button'});
  refresh.addEventListener('click',()=>openDocument(doc.id)); $('detailActions').append(refresh);
}
async function openDocument(id) {
  try {
    const result = await api('/api/documents/'+id), doc = result.document;
    currentDocument = doc; message('detailError');
    $('detailNumber').textContent = doc.number+' · версия '+doc.version;
    $('detailTitle').textContent = doc.title;
    const grid = element('dl',{class:'detail-grid'});
    for (const [label,value] of [['Вид',meta.kinds[doc.kind]],['Состояние',meta.statuses[doc.status]],['Корреспондент',doc.correspondent],['Исполнитель',doc.assignee_name || 'Не назначен'],['Дата регистрации',prettyDate(doc.registered_on)],['Срок исполнения',prettyDate(doc.due_on)]]) grid.append(element('div',{},[element('dt',{text:label}),element('dd',{text:value})]));
    $('detailBody').replaceChildren(grid);
    if (doc.result) $('detailBody').append(element('p',{class:'muted',text:'Результат: '+doc.result}));
    buildActions(doc); $('history').replaceChildren();
    for (const item of result.events) {
      const li = element('li',{},[actionLabels[item.action] || item.action,
        element('small',{text:item.actor_name+' · '+new Date(item.created_at).toLocaleString('ru-RU',{timeZone:'UTC'})+' UTC'})]);
      let details = '';
      if (item.action === 'assigned') details = meta.users.find(u => u.id === item.details.assignee_id)?.display_name || '';
      if (item.action === 'completed') details = item.details.result;
      if (item.action === 'edited') details = Object.values(item.details).join(' · ');
      if (details) li.append(element('p',{text:details}));
      $('history').append(li);
    }
    if (!$('detailDialog').open) $('detailDialog').showModal();
  } catch (error) {
    if ($('detailDialog').open) message('detailError',error.message); else message('error',error.message);
  }
}
async function boot() {
  try {
    meta = await (await fetch('/api/meta')).json();
    for (const person of meta.users) $('user').append(element('option',{value:person.id,text:person.display_name}));
    for (const [key,label] of Object.entries(meta.kinds)) $('kind').append(element('option',{value:key,text:label}));
    for (const [key,label] of Object.entries(meta.statuses)) $('status').append(element('option',{value:key,text:label}));
    $('user').addEventListener('change',() => { $('detailDialog').close(); $('createDialog').close(); $('newDocument').hidden = user().role !== 'registrar'; loadDocuments(); });
    $('filterForm').addEventListener('submit',event => {event.preventDefault(); loadDocuments();});
    for (const id of ['kind','status','overdue']) $(id).addEventListener('change',loadDocuments);
    $('resetFilters').addEventListener('click',() => {$('filterForm').reset(); loadDocuments();});
    $('newDocument').addEventListener('click',() => {$('createForm').reset(); message('createError'); $('createDialog').showModal();});
    for (const id of ['closeCreate','cancelCreate']) $(id).addEventListener('click',()=>$('createDialog').close());
    $('closeDetail').addEventListener('click',()=>$('detailDialog').close());
    $('createForm').addEventListener('submit',async event => {
      event.preventDefault();
      const button = event.target.querySelector('button[type=submit]'); button.disabled=true; message('createError');
      try {
        const result = await api('/api/documents',{method:'POST',body:Object.fromEntries(new FormData(event.target))});
        $('createDialog').close(); await loadDocuments(); await openDocument(result.document.id); toast('Документ зарегистрирован');
      } catch(error) {message('createError',error.message);} finally {button.disabled=false;}
    });
    $('exportCsv').addEventListener('click',async () => {
      $('exportCsv').disabled=true;
      try {
        const response = await fetch('/api/export.csv?'+filters(),{headers:{'X-Demo-User':$('user').value}});
        if (!response.ok) {const result=await response.json(); throw new Error(result.error);}
        const url=URL.createObjectURL(await response.blob()),link=element('a',{href:url,download:'documents.csv'});
        document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('CSV подготовлен по текущему отбору');
      } catch(error) {message('error',error.message);} finally {$('exportCsv').disabled=false;}
    });
    await loadDocuments();
  } catch(error) {message('error','Не удалось загрузить демонстрацию. '+error.message); $('newDocument').disabled=true;}
}
boot();
