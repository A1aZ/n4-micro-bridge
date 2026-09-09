'use strict';
// Rasterize official Phosphor artwork and generated avatars for Pillow/SVG.
// This build helper needs sharp; production renderers only read the outputs.
const fs=require('node:fs'),path=require('node:path');
const sharp=require(process.argv[2] || 'sharp');
const root=path.join(__dirname,'../assets/themes');
const catalog=require('../assets/themes/catalog.json');
async function main(){
  const output=path.join(root,'compiled');fs.mkdirSync(output,{recursive:true});
  const icons=[...new Set([...catalog.taskIcons,...Object.values(catalog.commands).map(v=>v.icon),...Object.values(catalog.variants).flatMap(v=>Object.values(v)),...Object.values(catalog.knobIcons),catalog.unassigned.icon])];
  for(const name of icons){
    const source=fs.readFileSync(path.join(root,'icons',name+'.svg'),'utf8').replace(/fill="currentColor"/g,'fill="#f4f1e8"');
    await sharp(Buffer.from(source)).resize(128,128).png().toFile(path.join(output,name+'.png'));
    await sharp(Buffer.from(source)).resize(64,64).png().toFile(path.join(output,name+'-native.png'));
    const pixelSource=name==='x-bold'?source.replace(/#f4f1e8/g,'#ff9292'):source;
    await sharp(Buffer.from(pixelSource)).resize(24,24).png().toFile(path.join(output,name+'-pixel.png'));
  }
  for(let i=1;i<=6;i++){
    const name='pixel-'+String(i).padStart(2,'0')+'.png';
    if(!fs.existsSync(path.join(root,name)))continue; // legacy portraits are not shipped in source releases
    await sharp(path.join(root,name)).trim({background:'#111a27',threshold:35}).resize(96,96,{fit:'contain',background:'#111a27',kernel:'nearest'}).png().toFile(path.join(output,name));
  }
  console.log('Prepared '+icons.length+' library icons and 6 generated avatars.');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
