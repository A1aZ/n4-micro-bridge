'use strict';
// Allowlist export. Never publishes, deletes, or reads user runtime data.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const root=path.resolve(__dirname,'..');
const version=require('../package.json').version;
const destination=process.argv[2]||path.join(root,'artifacts/releases',`mirabox-source-${version}-${Date.now()}`);
const excluded=new Set(['.git','__pycache__','node_modules','TransportDLL','x64','ARM64','Debug','Release','.vs','packages']);
const blockedScripts=new Set(['inspect-asar.cjs','compare-theme-proof.cjs']);
const allowed=new Set(['.py','.cjs','.cs','.ps1','.html','.css','.js','.json','.svg','.png','.ico','.md','.txt','.c','.h','.inx','.rc','.vcxproj','.sln','.yml']);
function collect(directory,relative,files){
 for(const item of fs.readdirSync(directory,{withFileTypes:true})){
  if(item.isSymbolicLink())throw Error('Refusing symlink: '+relative+'/'+item.name);
  if(excluded.has(item.name)||blockedScripts.has(item.name))continue;
  const rel=path.posix.join(relative,item.name),full=path.join(directory,item.name);
  if(item.isDirectory())collect(full,rel,files);
  else if(item.name==='LICENSE'||item.name==='MS-PL.txt'||allowed.has(path.extname(item.name))){
   if(/^assets\/themes\/pixel-\d+\.png$/.test(rel))continue;
   files.push(rel);
  }
 }
}
function exportSource(target){
 target=path.resolve(target);if(fs.existsSync(target))throw Error('Output already exists; choose a new directory');
 const files=['README.md','LICENSE','COPYRIGHT','THIRD_PARTY_NOTICES.md','SECURITY.md','CONTRIBUTING.md','CHANGELOG.md','package.json','requirements.txt','.gitignore'];
 for(const name of ['src','webui','tests','scripts','examples','driver','.github'])if(fs.existsSync(path.join(root,name)))collect(path.join(root,name),name,files);
 collect(path.join(root,'assets'),'assets',files);
 collect(path.join(root,'upstream/StreamDock-Device-SDK/Python-SDK/src'),'upstream/StreamDock-Device-SDK/Python-SDK/src',files);
 files.push('upstream/StreamDock-Device-SDK/LICENSE','upstream/openmicrokbd/LICENSE');
 for(const name of ['release.md','portable-app.md','micro-native-status.md','status-robot.md','troubleshooting.md'])files.push('docs/'+name);
 const manifest=[];
 for(const rel of [...new Set(files)].sort()){
  const input=path.join(root,rel),data=fs.readFileSync(input);
  if(/\.(?:py|cjs|cs|ps1|html|md|json)$/.test(rel)&&/C:[\\/]+Users[\\/]+alan|D:[\\/]+playground[\\/]+mirabox/i.test(data.toString()))throw Error('Personal workspace path in '+rel);
  manifest.push({path:rel,bytes:data.length,sha256:crypto.createHash('sha256').update(data).digest('hex')});
 }
 fs.mkdirSync(target,{recursive:true});
 for(const entry of manifest){const output=path.join(target,entry.path);fs.mkdirSync(path.dirname(output),{recursive:true});fs.copyFileSync(path.join(root,entry.path),output);}
 fs.writeFileSync(path.join(target,'source-manifest.json'),JSON.stringify({version,kind:'source-candidate',includesTransportBinaries:false,files:manifest},null,2));
 return {directory:target,files:manifest.length,version};
}
if(require.main===module)console.log(JSON.stringify(exportSource(destination)));
module.exports={exportSource};
