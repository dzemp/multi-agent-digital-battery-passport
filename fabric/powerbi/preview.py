"""Offline design proof using the generated report positions and synthetic sample values.

This is not a Power BI renderer and does not execute DAX or the semantic model.
"""

import json


def write_preview(path, cso, cfo, colors):
    pages = []
    for role, report in (("CSO", cso), ("CFO", cfo)):
        for page in report:
            pages.append({"role": role, "key": page.key, "label": page.label, "visuals": page.visuals})
    payload = json.dumps({"pages": pages, "colors": colors}, default=float).replace("<", "\\u003c")
    document = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Battery passport | Executive report design proof</title>
<style>
*{box-sizing:border-box}body{margin:0;background:var(--page);font-family:"Segoe UI",sans-serif;color:var(--ink)}
.proof{display:flex;align-items:center;gap:14px;padding:12px 20px;background:var(--white);\
border-bottom:1px solid var(--line);font-size:13px;flex-wrap:wrap}
.proof strong{font-size:14px}.proof small{margin-left:auto;color:var(--muted)}select{font:inherit;padding:5px 12px;\
border:1px solid var(--line);border-radius:4px;background:var(--white)}
#viewport{margin:18px auto;position:relative}#board{position:absolute;width:1600px;height:900px;background:var(--page);\
transform-origin:top left;overflow:hidden;box-shadow:0 6px 16px #0000000d}
.visual{position:absolute;overflow:hidden}.tile{background:var(--white);border:1px solid var(--line);border-radius:8px;\
padding:14px}
.title{font-size:15px;line-height:20px;font-weight:600;color:var(--ink)}.detail{font-size:12px;line-height:17px;\
color:var(--muted);margin-top:3px}
.value{font-size:38px;line-height:50px;font-weight:600;white-space:nowrap;margin-top:12px;letter-spacing:-.7px}
.accent{border-left:4px solid var(--accent)}
.legend{font-size:12px;display:flex;gap:14px;flex-wrap:wrap;margin:15px 0 5px;color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px}
svg{display:block;width:100%;height:100%;overflow:visible}svg text{font:12px "Segoe UI",sans-serif;fill:var(--muted)}
.chart{height:calc(100% - 85px);margin-top:12px}.tablewrap{overflow:auto;max-height:calc(100% - 62px);margin-top:14px}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;color:var(--muted);font-weight:600;\
background:var(--page);font-size:12px;position:sticky;top:0}
td,th{padding:10px 9px;border-bottom:1px solid var(--line);\
vertical-align:top}td:last-child{font-variant-numeric:tabular-nums}
.navigation{border:0;border-radius:6px;cursor:pointer;text-align:left;padding:0 14px;font:14px "Segoe UI",sans-serif;\
transition:filter .15s}
.navigation:hover{filter:brightness(1.15)}.text{white-space:pre-wrap;line-height:1.35}.text p{margin:0}
</style></head><body>
<div class="proof"><strong>Executive report design proof</strong>
<label>Report <select id="role"><option>CFO</option><option>CSO</option></select></label>
<label>Page <select id="page"></select></label>
<small>Synthetic layout samples. Not a Power BI screenshot, DAX result or current app export.</small></div>
<div id="viewport"><main id="board" aria-label="Executive report page"></main></div>
<script>
const {pages,colors}=__PAYLOAD__, palette=colors.dataColors;
Object.entries(colors).filter(([key])=>key!=='dataColors')\
.forEach(([key,value])=>document.documentElement.style.setProperty('--'+key,value));
const board=document.querySelector('#board'), role=document.querySelector('#role'),\
 select=document.querySelector('#page');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const lit=v=>v?.expr?.Literal?.Value?.replace(/^'|'$/g,'')?.replace(/''/g,"'");
const fill=o=>lit(o?.solid?.color);
const compact=n=>Math.abs(n)>=1e9?(n/1e9).toFixed(1)+'bn':Math.abs(n)>=1e6?(n/1e6).toFixed(1)+'m':\
Math.abs(n)>=1e3?(n/1e3).toFixed(0)+'k':Number(n).toFixed(Math.abs(n)<1&&n!==0?2:0);
const title=s=>`<div class="title">${esc(s.title)} <span style="font-size:10px;font-weight:400;\
color:var(--muted)"> / synthetic</span></div>${s.detail?`<div class="detail">${esc(s.detail)}</div>`:''}`;
function axes(w,h,min,max,format){let s='';for(let i=0;i<5;i++){let x=160+(w-192)*i/4;let n=min+(max-min)*i/4;\
s+=`<line x1="${x}" x2="${x}" y1="8" y2="${h-24}" stroke="var(--line)"/><text x="${x}" y="${h-4}"\
 text-anchor="middle">${esc(format(n))}</text>`}return s}
function bars(rows,series,w,h,stacked){
 let all=rows.flatMap(r=>r.slice(1)),min=Math.min(0,...all),max=Math.max(.001,...all);
 if(stacked)max=Math.max(.001,...rows.map(r=>r.slice(1).reduce((a,b)=>a+Math.max(0,b),0)));
 let format=series.length===1&&series[0]==='Plant coverage'?v=>(v*100).toFixed(1)+'%':compact;
 let x=v=>160+(v-min)/(max-min)*(w-192),s=axes(w,h,min,max,format),rh=(h-36)/Math.max(1,rows.length);
 rows.forEach((r,i)=>{let y=12+i*rh;s+=`<text x="148" y="${y+rh/2+4}"\
 text-anchor="end">${esc(r[0].length>28?r[0].slice(0,27)+'...':r[0])}</text>`;
 let offset=0;r.slice(1).forEach((v,j)=>{let bh=stacked?Math.min(32,rh*.65):Math.min(25,(rh-8)/series.length);
 let by=stacked?y+(rh-bh)/2:y+4+j*bh;let start=stacked?offset:0;if(stacked)offset+=v;
 let bx=x(Math.min(start,start+v)),width=Math.max(1,Math.abs(x(start+v)-x(start)));
 s+=`<rect x="${bx}" y="${by}" width="${width}" height="${bh-3}" rx="3"\
 fill="${palette[j%palette.length]}"><title>${esc(r[0])}: ${v.toLocaleString()}</title></rect>`})});
 return `<svg viewBox="0 0 ${w} ${h}">${s}</svg>`}
function lines(rows,series,w,h){
 let vals=rows.flatMap(r=>r.slice(1)),min=Math.min(0,...vals),max=Math.max(.001,...vals),left=70,top=12,bottom=h-28;
 let x=i=>left+i*(w-left-30)/Math.max(1,rows.length-1),y=v=>bottom-(v-min)/(max-min)*(bottom-top),s='';
 for(let i=0;i<5;i++){let n=min+(max-min)*i/4,py=y(n);\
s+=`<line x1="${left}" x2="${w-20}" y1="${py}" y2="${py}" stroke="var(--line)"/><text x="${left-10}" y="${py+4}"\
 text-anchor="end">${esc(compact(n))}</text>`}
 rows.forEach((r,i)=>s+=`<text x="${x(i)}" y="${h-5}" text-anchor="middle">${esc(r[0])}</text>`);
 series.forEach((_,j)=>{s+=`<polyline points="${rows.map((r,i)=>x(i)+','+y(r[j+1])).join(' ')}" fill="none"\
 stroke="${palette[j]}" stroke-width="3"/>`;
 rows.forEach((r,i)=>s+=`<circle cx="${x(i)}" cy="${y(r[j+1])}" r="4" fill="${palette[j]}"><title>${esc(r[0])}:\
 ${r[j+1].toLocaleString()}</title></circle>`)});
 return `<svg viewBox="0 0 ${w} ${h}">${s}</svg>`}
function render(){
 const page=pages.find(p=>p.role===role.value&&p.key===select.value);board.replaceChildren();if(!page)return;
 page.visuals.forEach(({document:d,sample:s})=>{
 const v=d.visual,p=d.position,o=v.objects||{},vco=v.visualContainerObjects||{},\
el=document.createElement(v.visualType==='actionButton'?'button':'div');
 el.className='visual';el.dataset.name=d.name;\
Object.assign(el.style,{left:p.x+'px',top:p.y+'px',width:p.width+'px',height:p.height+'px',zIndex:p.z});
 if(v.visualType==='shape'){el.style.background=fill(o.fill?.[0]?.properties?.fillColor)}
 else if(v.visualType==='textbox'){
   const bg=vco.background?.[0]?.properties,hasbg=lit(bg?.show)==='true';
   if(hasbg){el.style.background=fill(bg.color);el.style.padding='14px';\
if(fill(bg.color)===colors.white){el.style.border='1px solid var(--line)';el.style.borderRadius='8px'}}
   el.classList.add('text');const paragraphs=o.general[0].properties.paragraphs;
   paragraphs.forEach(p=>{const t=document.createElement('p');\
p.textRuns.forEach(r=>{let span=document.createElement('span');span.textContent=r.value||'\\u00a0';\
Object.assign(span.style,{fontFamily:r.textStyle.fontFamily,fontSize:r.textStyle.fontSize,color:r.textStyle.color,\
fontWeight:r.textStyle.fontFamily.includes('Semibold')?'600':'400'});t.append(span)});el.append(t)});
 }else if(v.visualType==='actionButton'){
   const tx=o.text.find(e=>e.selector)?.properties,fl=o.fill.find(e=>e.selector)?.properties;
   el.classList.add('navigation');el.textContent=lit(tx.text);el.style.color=fill(tx.fontColor);\
el.style.background=fill(fl.fillColor);
   const target=lit(vco.visualLink[0].properties.navigationSection);el.onclick=()=>{select.value=target;render()};
 }else if(s){
   el.classList.add('tile');el.innerHTML=title(s);
   if(v.visualType==='cardVisual'){el.classList.add('accent');el.style.setProperty('--accent',s.accent);\
el.innerHTML+=`<div class="value" style="color:${s.accent}">${esc(s.value)}</div>`}
   else if(v.visualType==='tableEx'){el.innerHTML+=\
`<div class="tablewrap"><table><thead><tr>${s.headings.map(h=>'<th>'+esc(h)+'</th>').join('')}\
</tr></thead><tbody>${s.rows.map(r=>'<tr>'+r.map(c=>'<td>'+esc(c)+'</td>').join('')+'</tr>').join('')}\
</tbody></table></div>`}
   else {el.innerHTML+=`<div class="legend">${s.series.map((l,i)=>`<span><i class="dot"\
 style="background:${palette[i]}"></i>${esc(l.replace(/^Base /,''))}</span>`).join('')}</div>`;
   const ch=document.createElement('div');ch.className='chart';let h=p.height-(s.detail?115:100),w=p.width-28;
   ch.innerHTML=v.visualType==='lineChart'?lines(s.rows,s.series,w,h):bars(s.rows,s.series,w,h,\
v.visualType==='barChart');el.append(ch)}
 }board.append(el);const chart=el.querySelector('.chart');\
if(chart)chart.style.height=Math.max(80,el.clientHeight-chart.offsetTop-14)+'px'});
 document.title=page.role+' | '+page.label+' | Executive report design proof';resize()
}
function choices(){select.replaceChildren();\
pages.filter(p=>p.role===role.value).forEach(p=>{let o=new Option(p.label,p.key);select.add(o)});render()}
function resize(){const s=Math.min(1,(innerWidth-32)/1600);document.querySelector('#viewport').style.width=1600*s+'px';\
document.querySelector('#viewport').style.height=900*s+'px';board.style.transform=`scale(${s})`}
role.onchange=choices;select.onchange=render;addEventListener('resize',resize);choices();
</script></body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document.replace("__PAYLOAD__", payload), encoding="utf-8", newline="\n")
    print("Design proof (not a Power BI rendering):", path)
