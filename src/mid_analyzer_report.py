"""Standalone HTML report for :mod:`src.mid_analyzer`."""

from __future__ import annotations

import html
import json
from typing import Any

import numpy as np
import pandas as pd


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if value is pd.NA:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, float):
        return None if not np.isfinite(value) else value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [_jsonable(row) for row in frame.to_dict(orient="records")]


def _json_script(value: Any) -> str:
    text = json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.replace("</", "<\\/")


def render_report_html(
    summary: pd.DataFrame,
    events: pd.DataFrame,
    quality: pd.DataFrame,
    windows: dict[str, list[dict[str, Any]]],
    manifest: dict[str, Any],
) -> str:
    """Render one self-contained report; all filtering and plotting is local JS."""
    summary_records = _records(summary)
    event_records = _records(events)
    quality_records = _records(quality)
    event_count = len(event_records)
    strict_count = sum(1 for row in event_records if row.get("mode") == "strict")
    contract_count = len({str(row.get("InstrumentID", "")) for row in quality_records if row.get("InstrumentID")})
    days = sorted({str(row.get("trade_date", "")) for row in quality_records if row.get("trade_date")})
    title = "Mid + LastPrice 异常检测报告"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",sans-serif;margin:24px;color:#172033;background:#f5f7fb}}
h1,h2{{margin:0 0 12px;color:#0f172a}} h2{{font-size:18px;margin-top:28px}}
.note{{color:#64748b;font-size:13px;line-height:1.6}} .cards{{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 18px;min-width:150px}} .label{{color:#64748b;font-size:12px}} .value{{font-size:23px;font-weight:700;margin-top:4px}}
.filters{{display:flex;flex-wrap:wrap;gap:10px;align-items:end;background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px;margin:14px 0}}
label{{font-size:12px;color:#475569;display:flex;flex-direction:column;gap:4px}} input,select,button{{font:inherit;border:1px solid #cbd5e1;border-radius:7px;padding:7px 9px;background:#fff}} button{{cursor:pointer;background:#0f766e;color:#fff;border-color:#0f766e}} button.secondary{{background:#fff;color:#0f766e}}
.table-wrap{{overflow:auto;background:#fff;border:1px solid #e2e8f0;border-radius:12px}} table{{border-collapse:collapse;width:100%;min-width:980px}} th,td{{border-bottom:1px solid #e2e8f0;padding:8px 10px;text-align:left;font-size:12px;white-space:nowrap}} th{{position:sticky;top:0;background:#eef2ff;color:#334155;cursor:pointer}} tr:hover td{{background:#f0fdfa}}
.empty{{padding:20px;color:#64748b}} .status-ok{{color:#047857}} .status-error{{color:#b91c1c}} .status-duplicate{{color:#b45309}}
dialog{{border:0;border-radius:14px;width:min(1180px,94vw);max-height:92vh;padding:0;box-shadow:0 24px 90px #0f172a55}} dialog::backdrop{{background:#0f172a88}} .dialog-body{{padding:20px;overflow:auto;max-height:92vh}} .dialog-head{{display:flex;justify-content:space-between;gap:10px;align-items:start}}
canvas{{width:100%;height:300px;border:1px solid #e2e8f0;border-radius:8px;background:#fff}} .legend{{font-size:12px;color:#475569;margin:6px 0 12px}} .legend span{{margin-right:14px}} .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}}
@media(max-width:700px){{body{{margin:12px}} .card{{min-width:120px}}}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<p class="note">模型只使用新增成交、LastPrice 和买一/卖一中价。候选是历史研究样本，不代表真实撮合成交。恢复率的分母只包含有有效观察的事件。</p>
<div class="cards"><div class="card"><div class="label">事件总数（全部阈值/口径）</div><div class="value">{event_count}</div></div><div class="card"><div class="label">Strict 事件</div><div class="value">{strict_count}</div></div><div class="card"><div class="label">合约数</div><div class="value">{contract_count}</div></div><div class="card"><div class="label">覆盖日期</div><div class="value">{len(days)}</div></div></div>
<h2>阈值汇总</h2>
<div class="filters"><label>口径<select id="mode"><option value="strict" selected>Strict</option><option value="raw">Raw</option><option value="all">全部</option></select></label><label>方向<select id="side"><option value="all">全部</option><option value="down">向下</option><option value="up">向上</option></select></label><label>合约/品种<input id="contract" placeholder="例如 SA605 或 SA"></label><label>阈值<select id="threshold"><option value="all">全部</option></select></label><button class="secondary" id="reset">重置筛选</button></div>
<div class="table-wrap"><table id="summaryTable"><thead><tr><th data-key="InstrumentID">合约</th><th data-key="side">方向</th><th data-key="mode">口径</th><th data-key="threshold_pct">阈值%</th><th data-key="active_days">有效日</th><th data-key="candidate_rows">候选行</th><th data-key="independent_events">独立事件</th><th data-key="events_per_day">事件/日</th><th>Recovery80@1s</th><th>Recovery80@5s</th><th>MAE P95%</th></tr></thead><tbody></tbody></table></div>
<h2>事件明细</h2><div class="table-wrap"><table id="eventTable"><thead><tr><th data-key="event_id">编号</th><th data-key="trade_date">交易日</th><th data-key="InstrumentID">合约</th><th data-key="side">方向</th><th data-key="mode">口径</th><th data-key="threshold_pct">阈值%</th><th data-key="event_time">代表时间</th><th data-key="deviation_pct">偏离%</th><th data-key="mid_move_pct">Mid变化%</th><th>恢复/窗口</th><th>操作</th></tr></thead><tbody></tbody></table></div>
<h2>数据质量与来源</h2><div class="table-wrap"><table id="qualityTable"><thead><tr><th>交易日</th><th>合约</th><th>来源</th><th>行数</th><th>有效时间</th><th>有效成交量</th><th>有效盘口</th><th>新增成交</th><th>偏离记录</th><th>成交量回退</th><th>状态</th><th>说明</th></tr></thead><tbody></tbody></table></div>
<dialog id="eventDialog"><div class="dialog-body"><div class="dialog-head"><div><h2 id="dialogTitle">事件</h2><p class="note" id="dialogNote"></p></div><button class="secondary" id="closeDialog">关闭</button></div><div class="legend"><span><i class="dot" style="background:#dc2626"></i>LastPrice</span><span><i class="dot" style="background:#2563eb"></i>Mid</span><span><i class="dot" style="background:#059669"></i>Bid1</span><span><i class="dot" style="background:#d97706"></i>Ask1</span></div><canvas id="chart" width="1100" height="300"></canvas><div class="table-wrap"><table id="windowTable"><thead><tr><th>原始行</th><th>时间</th><th>相对秒</th><th>LastPrice</th><th>Mid</th><th>买一</th><th>卖一</th><th>成交量</th></tr></thead><tbody></tbody></table></div></div></dialog>
<script id="reportData" type="application/json">{_json_script({"summary": summary_records, "events": event_records, "quality": quality_records, "windows": windows, "manifest": manifest})}</script>
<script>
const DATA=JSON.parse(document.getElementById('reportData').textContent), summaryBody=document.querySelector('#summaryTable tbody'), eventBody=document.querySelector('#eventTable tbody'), qualityBody=document.querySelector('#qualityTable tbody');
const mfeMaeHorizon=(DATA.manifest.parameters||{{}}).mfe_mae_horizon_seconds??5;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const num=(v,d=2)=>v==null?'':Number(v).toFixed(d).replace(/\\.?0+$/,'');
const pctRate=(row,h)=>row['recovery80_'+h+'s_rate']==null?'无有效观察':num(row['recovery80_'+h+'s_rate']*100,1)+'% ('+(row['recovery80_'+h+'s_n']??0)+')';
const select=document.getElementById('threshold'); [...new Set(DATA.summary.map(x=>x.threshold_pct).filter(x=>x!=null))].sort((a,b)=>a-b).forEach(x=>select.insertAdjacentHTML('beforeend','<option value="'+esc(x)+'">'+num(x)+'%</option>'));
function matches(row){{const mode=document.getElementById('mode').value, side=document.getElementById('side').value, q=document.getElementById('contract').value.trim().toUpperCase(), threshold=document.getElementById('threshold').value; return (mode==='all'||row.mode===mode)&&(side==='all'||row.side===side)&&(!q||String(row.InstrumentID||'').toUpperCase().includes(q)||String(row.InstrumentID||'').toUpperCase().replace(/[0-9]/g,'').includes(q))&&(threshold==='all'||String(row.threshold_pct)===threshold)}}
function renderSummary(){{summaryBody.innerHTML=''; const rows=DATA.summary.filter(matches); if(!rows.length){{summaryBody.innerHTML='<tr><td colspan="11" class="empty">无符合条件的汇总。</td></tr>';return}} rows.forEach(r=>summaryBody.insertAdjacentHTML('beforeend','<tr><td>'+esc(r.InstrumentID)+'</td><td>'+esc(r.side==='down'?'向下':'向上')+'</td><td>'+esc(r.mode)+'</td><td>'+num(r.threshold_pct)+'%</td><td>'+num(r.active_days,0)+'</td><td>'+num(r.candidate_rows,0)+'</td><td>'+num(r.independent_events,0)+'</td><td>'+num(r.events_per_day)+'</td><td>'+pctRate(r,1)+'</td><td>'+pctRate(r,5)+'</td><td>'+num(r.mae_p95_pct)+'</td></tr>'))}}
function renderEvents(){{eventBody.innerHTML=''; const rows=DATA.events.filter(matches); if(!rows.length){{eventBody.innerHTML='<tr><td colspan="11" class="empty">无符合条件的事件。</td></tr>';return}} rows.forEach(r=>{{const recovery=r.recovery80_5s==null?'无有效观察':(r.recovery80_5s?'5秒恢复≥80%':'5秒未达80%'); const complete=r.mfe_mae_window_complete?mfeMaeHorizon+'秒完整':'窗口不足'; eventBody.insertAdjacentHTML('beforeend','<tr><td>'+esc(r.event_id)+'</td><td>'+esc(r.trade_date)+'</td><td>'+esc(r.InstrumentID)+'</td><td>'+esc(r.side==='down'?'向下':'向上')+'</td><td>'+esc(r.mode)+'</td><td>'+num(r.threshold_pct)+'%</td><td>'+esc(r.event_time)+'</td><td>'+num(r.deviation_pct)+'%</td><td>'+num(r.mid_move_pct)+'%</td><td>'+recovery+' / '+complete+'</td><td><button data-event="'+esc(r.event_id)+'">查看窗口</button></td></tr>')}})}}
function renderQuality(){{qualityBody.innerHTML=''; if(!DATA.quality.length){{qualityBody.innerHTML='<tr><td colspan="12" class="empty">没有来源记录。</td></tr>';return}} DATA.quality.forEach(r=>qualityBody.insertAdjacentHTML('beforeend','<tr><td>'+esc(r.trade_date)+'</td><td>'+esc(r.InstrumentID)+'</td><td title="'+esc(r.source_label)+'">'+esc(String(r.source_file||r.source_label||'').slice(-42))+'</td><td>'+num(r.rows,0)+'</td><td>'+num(r.valid_time_rows,0)+'</td><td>'+num(r.valid_volume_rows,0)+'</td><td>'+num(r.valid_bbo_rows,0)+'</td><td>'+num(r.new_trade_records,0)+'</td><td>'+num(r.deviation_records,0)+'</td><td>'+num(r.volume_reset_rows,0)+'</td><td class="status-'+esc(r.status)+'">'+esc(r.status)+'</td><td>'+esc(r.message)+'</td></tr>'))}}
function redraw(){{renderSummary();renderEvents()}} ['mode','side','contract','threshold'].forEach(id=>document.getElementById(id).addEventListener('input',redraw)); document.getElementById('reset').onclick=()=>{{document.getElementById('mode').value='strict';document.getElementById('side').value='all';document.getElementById('contract').value='';document.getElementById('threshold').value='all';redraw()}};
document.querySelectorAll('th[data-key]').forEach(th=>th.onclick=()=>{{const table=th.closest('table'), index=th.cellIndex, body=table.querySelector('tbody'), rows=[...body.querySelectorAll('tr')], direction=th.dataset.direction==='asc'?-1:1; th.dataset.direction=direction===1?'asc':'desc'; rows.sort((a,b)=>{{const av=a.children[index]?.textContent.trim()||'', bv=b.children[index]?.textContent.trim()||'', an=Number(av.replace('%','')), bn=Number(bv.replace('%','')), numeric=av!==''&&bv!==''&&Number.isFinite(an)&&Number.isFinite(bn); return (numeric?an-bn:av.localeCompare(bv,'zh-CN'))*direction}}); rows.forEach(r=>body.appendChild(r))}});
const dialog=document.getElementById('eventDialog'); document.getElementById('closeDialog').onclick=()=>dialog.close();
eventBody.onclick=e=>{{const button=e.target.closest('button[data-event]');if(!button)return; const event=DATA.events.find(x=>x.event_id===button.dataset.event), rows=DATA.windows[event.window_id]||[]; document.getElementById('dialogTitle').textContent='事件 '+event.event_id+' · '+event.InstrumentID+' '+event.event_time; document.getElementById('dialogNote').textContent='锚点 Mid='+num(event.anchor_mid)+'，代表点 Mid='+num(event.event_mid)+'，LastPrice='+num(event.event_last)+'，偏离='+num(event.deviation_pct)+'%；红色行是代表点。'; draw(rows); const body=document.querySelector('#windowTable tbody'); body.innerHTML=''; rows.forEach(r=>body.insertAdjacentHTML('beforeend','<tr'+(r.is_representative?' style="background:#fee2e2;font-weight:700"':'')+'><td>'+esc(r.row_order)+'</td><td>'+esc(r.time)+'</td><td>'+num(r.ts_seconds_from_event)+'</td><td>'+num(r.last_price)+'</td><td>'+num(r.mid)+'</td><td>'+num(r.bid1)+'</td><td>'+num(r.ask1)+'</td><td>'+num(r.volume,0)+'</td></tr>')); dialog.showModal()}};
function draw(rows){{const canvas=document.getElementById('chart'),ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h); const series=[['last_price','#dc2626'],['mid','#2563eb'],['bid1','#059669'],['ask1','#d97706']], values=series.flatMap(([k])=>rows.map(r=>Number(r[k])).filter(Number.isFinite)); if(!values.length)return; const lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1,x=i=>40+(w-60)*(i/Math.max(1,rows.length-1)),y=v=>h-25-(h-45)*(v-lo)/span;ctx.strokeStyle='#cbd5e1';ctx.strokeRect(40,10,w-55,h-35); series.forEach(([key,color])=>{{ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();let started=false;rows.forEach((r,i)=>{{const v=Number(r[key]);if(!Number.isFinite(v)){{started=false;return}} if(!started){{ctx.moveTo(x(i),y(v));started=true}}else ctx.lineTo(x(i),y(v))}});ctx.stroke()}});ctx.fillStyle='#475569';ctx.font='12px sans-serif';ctx.fillText(num(hi),4,18);ctx.fillText(num(lo),4,h-25)}}
renderQuality();redraw();
</script></body></html>"""
