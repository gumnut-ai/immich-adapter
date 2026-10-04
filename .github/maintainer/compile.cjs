// Pinned compiler currently needs this generated-only detector image correction.
const fs=require('node:fs');const {execFileSync}=require('node:child_process');
const compiler=process.argv[2];
if(!compiler) throw Error('usage: node .github/maintainer/compile.cjs /path/to/gh-aw');
execFileSync(compiler,['compile','maintainer','--action-tag','c35393777e5604a63721d09512263b1383301d4f','--no-check-update'],{stdio:'inherit'});
const source=fs.readFileSync('.github/workflows/maintainer.md','utf8');
const images=Object.fromEntries([...source.matchAll(/^      (agent|apiProxy|squid): (ghcr[^\n]+)$/gm)].map(m=>[m[1],m[2]]));
if(Object.keys(images).length!==3) throw Error('expected three digest-qualified AWF images');
let text=fs.readFileSync('.github/workflows/maintainer.lock.yml','utf8');
const start=text.indexOf('  detection:\n');
if(start<0) throw Error('detector missing');
let detector=text.slice(start);
for(const image of Object.values(images)) detector=detector.split(image.split('@')[0]).join(image).replaceAll(image+image.slice(image.indexOf('@')),image);
const tag='"imageTag":"0.28.25"';
if(!detector.includes(tag)) throw Error('expected detector imageTag correction point');
detector=detector.replace(tag,'"images":'+JSON.stringify(images));
text=text.slice(0,start)+detector;
if(text.split('\n').some(line=>line.includes('uses: github/gh-aw/actions/setup@') && !line.includes('c35393777e5604a63721d09512263b1383301d4f'))) throw Error('setup action SHA drift');
fs.writeFileSync('.github/workflows/maintainer.lock.yml',text);
