// Code-native pixel vector mascot; identical geometry, state-specific face.
const fs=require('node:fs'),path=require('node:path');
const sharp=require(process.argv[2]||'sharp');
const root=path.join(__dirname,'../assets/themes');
const colors={idle:'#dce7f1',working:'#45bdff',complete:'#5ae396',attention:'#ffc35e',error:'#ff7777',off:'#64768b',unknown:'#9aa9bd'};
async function main(){
 for(const [state,color] of Object.entries(colors)){
  let p='';const rect=(x,y,w,h,c)=>p+=`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${c}"/>`;
  const ink='#111a27',edge='#344557';
  rect(11,1,2,3,color);rect(10,1,4,1,color);
  rect(5,5,14,1,edge);rect(3,6,18,10,edge);rect(4,5,16,12,edge);
  rect(5,6,14,10,color);rect(4,7,16,8,color);
  rect(1,9,2,5,color);rect(21,9,2,5,color);
  rect(6,8,12,6,ink);
  if(state==='complete'){
   for(const x of [7,13]){rect(x,10,1,1,color);rect(x+1,9,2,1,color);rect(x+3,10,1,1,color);}
   rect(10,12,4,1,color);
  }else if(state==='error'){
   for(const x of [7,14]){rect(x,9,1,1,color);rect(x+2,9,1,1,color);rect(x+1,10,1,1,color);rect(x,11,1,1,color);rect(x+2,11,1,1,color);}
  }else if(state==='off'){
   rect(7,11,3,1,color);rect(14,11,3,1,color);
  }else if(state==='attention'){
   rect(7,9,3,4,color);rect(14,9,3,4,color);rect(8,10,1,2,ink);rect(15,10,1,2,ink);
  }else{
   rect(7,10,3,2,color);rect(14,10,3,2,color);
  }
  rect(10,17,4,1,color);rect(6,18,12,4,edge);rect(7,18,10,4,color);
  rect(3,18,2,3,color);rect(19,18,2,3,color);rect(6,22,4,1,color);rect(14,22,4,1,color);
  rect(10,18,4,4,ink);
  if(state==='complete'){rect(10,20,1,1,color);rect(11,21,1,1,color);rect(12,20,1,1,color);rect(13,19,1,1,color);}
  else if(state==='attention'||state==='error'){rect(11,18,2,2,color);rect(11,21,2,1,color);}
  else if(state==='working'){rect(10,19,1,1,color);rect(12,19,1,1,color);rect(11,21,1,1,color);rect(13,21,1,1,color);}
  else{rect(11,19,2,2,color);}
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24" shape-rendering="crispEdges">${p}</svg>`;
  await sharp(Buffer.from(svg)).png().toFile(path.join(root,'compiled',`robot-${state}.png`));
 }
 console.log('Built 7 native 96px robot states');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
