import {store} from '../stores/entities.js';
import {session} from '../stores/session.js';
import {handoverAPI} from '../api/entities.js';
import {downloadHandover} from '../api/client.js';
import {table} from '../components/common/table.js';
import {handoverBadge} from '../components/common/handover_status.js';
import {esc,formObject} from '../utils/html.js';
import {action,message} from '../hooks/action.js';

const logSetName=id=>store.logs.find(l=>l.id===id)?.name||('#'+id);

export function handoverPage(){
 const selectedLogId=Number(document.querySelector('#handover-logset')?.value||store.logs[0]?.id);
 const eligible=store.cases.filter(c=>c.log_set_id===selectedLogId);
 return `<div class="section-title"><h2>交班复测短日志</h2><span>整段事务取舍 · 条目最少 · 逐点复测</span></div>
 <section class="split"><div><h2>带一份短日志去交班</h2>
 <p>在同一日志集上勾选若干原断电点。系统按事务整段取舍生成条目最少的短日志，保留条目维持原顺序；再把每个原断电点按位置对照换算成复测点，用同一恢复模型逐点比对。恢复读数与原账逐点相同才发放短日志和位置对照，否则整单退回。</p>
 <p class="big-note">影响读数的写入，一条都不能少。</p>
 <p class="readonly">原账与完整恢复轨迹仍可在 <a href="#runs">恢复队列 / 恢复证据</a> 随时核对，短日志不改动原账。</p></div>
 ${session.role!=='viewer'?`<form id="handover-form">
 <label>交班记录名<input name="label" value="晚班交班复测包" required></label>
 <label>日志集<select name="log_set_id" id="handover-logset">${store.logs.map(l=>`<option value="${l.id}" ${l.id===selectedLogId?'selected':''}>${esc(l.name)} · ${l.records.length}条</option>`).join('')}</select></label>
 <label>原断电点（可多选，须同一日志集）<div class="check-list">${eligible.length?eligible.map(c=>`<label class="check"><input type="checkbox" name="crash_case_ids" value="${c.id}"> ${esc(c.name)} · 前 ${c.crash_after} 条持久</label>`).join(''):'<p class="readonly">该日志集暂无故障场景，请先到故障注入点创建。</p>'}</div></label>
 <button type="submit" ${eligible.length?'':'disabled'}>生成短日志并复测 →</button>
 </form>`:'<p class="readonly">观察员可查看下方复测结果并导出通过的短日志。</p>'}</section>
 <div class="section-title"><h2>交班复测记录</h2><button class="secondary" id="refresh">刷新</button></div>
 ${table(['编号','记录名','日志集','原断电点','条目数','复测结论','明细'],store.handovers.map(h=>[
  h.id,esc(h.label),esc(logSetName(h.log_set_id)),(h.crash_points||[]).join(', '),
  h.status==='passed'?h.result?.minimality?.kept_entry_count??'—':'—',
  handoverBadge(h.status),
  `<button class="secondary link-btn" data-handover="${h.id}">查看</button>`]))}
 <div id="handover-detail"></div>`;
}

export function bindHandover(render){
 document.querySelector('#refresh')?.addEventListener('click',render);
 document.querySelector('#handover-logset')?.addEventListener('change',render);
 action(document.querySelector('#handover-form'),async f=>{
  const picked=[...f.querySelectorAll('input[name=crash_case_ids]:checked')].map(x=>Number(x.value));
  if(!picked.length)throw new Error('请至少勾选一个原断电点');
  const d=formObject(f);delete d.crash_case_ids;
  const out=await handoverAPI.create({...d,log_set_id:Number(d.log_set_id),crash_case_ids:picked});
  message(out.reused?'同一申请已存在，复用既有复测记录，未重复生成':'复测完成，记录可复查');
  await render();
  showDetail(out.id);
 });
 document.querySelectorAll('[data-handover]').forEach(btn=>btn.addEventListener('click',()=>showDetail(Number(btn.dataset.handover))));
}

async function showDetail(id){
 const box=document.querySelector('#handover-detail');
 const h=await handoverAPI.get(id);
 const r=h.result||{};
 const checks=(r.checks||[]).map(c=>table(['原断电点','复测点','复测读数','原账读数','结论'],[[
   c.original_crash_after,c.short_crash_after,esc(JSON.stringify(c.actual)),esc(JSON.stringify(c.expected)),
   c.readings_match?'<span class="ok">逐点相同</span>':'<span class="bad">不一致 · 退回</span>']])).join('');
 const reasons=(r.reject_reasons||[]).length?`<h2>退回原因</h2><ul class="reasons">${r.reject_reasons.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'';
 const passed=h.status==='passed';
 box.innerHTML=`<div class="handover-card">
  <div class="section-title"><h2>${esc(h.label)} <small>#${h.id}</small></h2>${handoverBadge(h.status)}</div>
  ${passed?`<section class="verdict"><div><p>复测结论</p><h2>逐点读数一致，准予发放</h2></div>
   <p>保留 ${r.minimality?.kept_entry_count} 条 / 丢弃 ${r.minimality?.dropped_entry_count} 条 · 涉及事务 ${r.minimality?.kept_tx_count} 个<br>短日志摘要 ${esc(r.short_log_sha256||'')}<br>输入摘要 ${esc(r.input_sha256||'')}</p></section>
   <h2>位置对照 · 断电点</h2>${table(['原断电点','短日志复测点'],(r.position_map?.crash_points||[]).map(p=>[p.original_crash_after,p.short_crash_after]))}
   <h2>位置对照 · 日志条目（原LSN → 短日志序号）</h2>${table(['原 LSN','短日志序号','操作','事务','键','值'],Object.entries(r.position_map?.lsn_index||{}).map(([lsn,idx])=>{const rec=r.short_records[idx-1]||{};return [lsn,idx,esc(rec.op),esc(rec.tx||'—'),esc(rec.key||'—'),rec.value??'—'];}))}
   <h2>短日志（按原顺序处理）</h2><pre>${esc(JSON.stringify(r.short_records,null,2))}</pre>
   <h2>丢弃条目与理由</h2>${table(['原 LSN','操作','事务','理由'],(r.dropped_entries||[]).map(d=>[d.lsn,esc(d.op),esc(d.tx||'—'),esc(d.reason)]))}
   <div class="toolbar"><button id="handover-export">下载短日志与位置对照 JSON</button></div>`
  :`<section class="verdict fail"><div><p>复测结论</p><h2>读数不一致，短日志不予发放</h2></div><p>原账仍可在恢复队列完整查看。<br>${esc(r.model_version||'')}</p></section>${reasons}`}
  <h2>逐点复测</h2>${checks}
  <details><summary>查看完整演算结果</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details></div>`;
 document.querySelector('#handover-export')?.addEventListener('click',()=>downloadHandover(h.id).catch(e=>message(e.message,true)));
 box.scrollIntoView({behavior:'smooth'});
}