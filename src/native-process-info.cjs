'use strict';
const path=require('node:path');
function imageName(row){
  if(row.name)return String(row.name).toLowerCase();
  const text=String(row.command||'').trim();
  const first=text.startsWith('"')?text.slice(1).split('"')[0]:text.split(/\s+/)[0];
  return path.win32.basename(first.replace(/\//g,'\\')).toLowerCase();
}
function managedProcessIds(rows,rootPid){
  const ids=new Set(rootPid?[Number(rootPid)]:[]);
  let changed=true;
  while(changed){changed=false;for(const row of rows)if(ids.has(row.parentPid)&&!ids.has(row.pid)){ids.add(row.pid);changed=true;}}
  return ids;
}
module.exports={imageName,managedProcessIds};
