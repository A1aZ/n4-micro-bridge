'use strict';
const fs=require('node:fs'),path=require('node:path');
const catalog=require('../assets/themes/catalog.json');
const cache=new Map();
const {microStatus}=require('./micro-status.cjs');
const statusArt=require('../assets/themes/status-art.json');
function agentFeedback(light,enabled=true){
  const active=enabled&&light.b>0&&light.e!==0&&(light.c!==0||[3,5].includes(light.e));
  const animated=active&&[2,3,4,5,6].includes(light.e)&&light.s>0;
  const label=!enabled?'停用':active?({1:'常亮',2:'流动',3:'彩虹',4:'呼吸',5:'渐变',6:'微呼吸'}[light.e]||'灯效'):'未亮';
  return {active,animated,label};
}
const escape=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const allowed=new Set([...catalog.taskIcons,...Object.values(catalog.commands).map(v=>v.icon),catalog.unassigned.icon]);
for(const names of Object.values(catalog.variants))for(const name of Object.values(names))allowed.add(name);
for(const name of Object.values(catalog.knobIcons))allowed.add(name);
for(const name of [...allowed])allowed.add(name+'-native');
for(const name of [...allowed])allowed.add(name+'-pixel');
for(let i=1;i<=6;i++)allowed.add('pixel-'+String(i).padStart(2,'0'));
for(const state of ['idle','working','complete','attention','error','off','unknown'])allowed.add('robot-'+state);
function imageData(name){
  if(!allowed.has(name))throw new Error('Unknown theme asset');
  if(!cache.has(name))cache.set(name,'data:image/png;base64,'+fs.readFileSync(path.join(__dirname,'../assets/themes/compiled',name+'.png')).toString('base64'));
  return cache.get(name);
}
function themeKeyGroup(key,x,y){
  const theme=key.theme,width=key.width,light=key.light,enabled=key.enabled!==false&&key.targetKey!=null;
  const target=key.targetKey||'',slot=/^AG0[0-5]$/.test(target)?Number(target.slice(2)):null;
  const visiblyActive=enabled&&(slot===null||agentFeedback(light,enabled).active);
  const foreground=visiblyActive?'#f4f1e8':'#7e8b9e';
  const level=enabled?Number(light.intensity)||0:0;
  const rgb=light.c||0,accent='#'+[rgb>>16&255,rgb>>8&255,rgb&255].map(c=>Math.round(55+(c-55)*level).toString(16).padStart(2,'0')).join('');
  const text=(value,xx,yy,size,anchor='middle',numeric=false)=>`<text x="${xx}" y="${yy}" fill="${foreground}" text-anchor="${anchor}" dominant-baseline="middle" font-family="${numeric?'Bahnschrift Condensed,Bahnschrift,':'Microsoft YaHei,'}Segoe UI,sans-serif" font-size="${size}" font-weight="700"${numeric?' font-stretch="condensed"':''}>${escape(value)}</text>`;
  const icon=(name,size,cx,cy,pixel=false)=>`<image href="${imageData(name)}" x="${cx-size/2}" y="${cy-size/2}" width="${size}" height="${size}" opacity="${visiblyActive?1:.4}"${pixel?' style="image-rendering:pixelated"':''}/>`;
  let content='';
  if(slot!==null){
    const number=String(slot+1).padStart(2,'0');
    const feedback=agentFeedback(light,enabled),rgbColor=rgb||0x59d3e6;
    const edge=feedback.active?'#'+[rgbColor>>16&255,rgbColor>>8&255,rgbColor&255].map(c=>Math.round(c*light.b).toString(16).padStart(2,'0')).join(''):'#414e5e';
    content+=`<style>@keyframes agentPulse{0%,100%{opacity:.55}50%{opacity:1}}@keyframes agentTravel{from{transform:translateX(0)}to{transform:translateX(${width-42}px)}}@keyframes agentFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}.agent-pulse{animation:agentPulse 5s ease-in-out infinite}.agent-travel{animation:agentTravel 5s linear infinite}.agent-float{animation:agentFloat 5s ease-in-out infinite}@media(prefers-reduced-motion:reduce){.agent-pulse,.agent-travel,.agent-float{animation:none!important}}</style>`;
    content+=`<rect x="${x+3}" y="${y+3}" width="${width-7}" height="105" rx="9" fill="${edge}" fill-opacity=".22" stroke="${edge}" stroke-width="6"${feedback.animated?' class="agent-pulse"':''}/>`;
    if(theme==='symbols'){
      content+=icon(catalog.taskIcons[slot]+'-native',64,x+width/2,y+56)+text(number,x+12,y+14,13,'start');
    } else if(theme==='atlas'){
      content+=text(number,x+12,y+46,59,'start',true);
    } else if(theme==='pixel'){
      content+=icon(statusArt[microStatus(light,enabled)],96,x+width/2,y+56,true)+text(number,x+9,y+13,13,'start');
    }
    content+=`<circle cx="${x+width-15}" cy="${y+16}" r="4" fill="${edge}"${feedback.animated?' class="agent-pulse"':''}/>`;
    if(enabled&&!feedback.active)content+=`<g data-light-dimmed="true"><title>灯光已暗，不代表未分配任务</title><circle cx="${x+width-17}" cy="${y+16}" r="6" fill="#9fb1c4"/><circle cx="${x+width-13}" cy="${y+12}" r="5" fill="#111a27"/></g>`;
    content+=`<rect data-micro-status-color="true" x="${x+8}" y="${y+104}" width="${width-16}" height="6" fill="${feedback.active?edge:'#242e3c'}"/>`;
  } else {
    const entry=catalog.commands[target]||catalog.unassigned,pixel=theme==='pixel';
    const name=catalog.variants[theme]?.[target]||entry.icon;
    content+=icon(name+'-native',64,x+width/2,y+((theme==='atlas'||width>112)?48:56),false);
    if(theme==='atlas'||width>112)content+=text(enabled?entry.label:'未分配',x+width/2,y+93,width>112?15:13);
    if(enabled&&level>0)content+=`<path d="M${x+12} ${y+5}H${x+width-13}" stroke="${accent}" stroke-width="3"/>`;
  }
  return `<g class="n4-key" data-theme="${escape(theme)}" data-key-index="${key.index}" data-target="${escape(target)}"><title>${escape(target||'未分配')}</title><rect x="${x+1}" y="${y+1}" width="${width-3}" height="109" rx="9" fill="#111a27" stroke="#2e3b4c"/>${content}</g>`;
}
function infoStripGroup(key,x,y){
  const info=key.knobInfo,mode=info.mode||'micro';
  const title={micro:'导航',scroll:'聊天滚动',reasoning:'推理强度',brightness:'亮度'}[mode];
  let detail={micro:'跟随 Codex 模式',scroll:'鼠标放聊天区',reasoning:info.reasoningConfigured?'− / +':'待绑定左右方向',brightness:info.brightness==null?'旋转调节 · 每格5%':info.brightness+'%'}[mode];
  if(info.enabled===false)detail='已停用';
  return `<g class="n4-key" data-strip-info="true"><rect x="${x+1}" y="${y+1}" width="174" height="109" rx="8" fill="#111a27" stroke="#2e3b4c"/><text x="${x+12}" y="${y+19}" fill="#7e8b9e" font-size="11" font-family="Microsoft YaHei,sans-serif">旋钮 ${info.index+1}</text><image href="${imageData(catalog.knobIcons[mode])}" x="${x+12}" y="${y+39}" width="25" height="25"/><text x="${x+47}" y="${y+58}" fill="#f4f1e8" font-size="17" font-weight="700" font-family="Microsoft YaHei,sans-serif">${escape(title)}</text><text x="${x+88}" y="${y+94}" text-anchor="middle" fill="#7e8b9e" font-size="12" font-family="Microsoft YaHei,sans-serif">${escape(detail)}</text></g>`;
}
module.exports={themeKeyGroup,infoStripGroup,catalog,agentFeedback};
