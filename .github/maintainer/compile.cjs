// Pinned compiler currently needs this generated-only detector image correction.
const fs=require('node:fs');const {execFileSync}=require('node:child_process');
const compiler=process.argv[2];
if(!compiler) throw Error('usage: node .github/maintainer/compile.cjs /path/to/gh-aw');
execFileSync(compiler,['compile','maintainer','--action-tag','c35393777e5604a63721d09512263b1383301d4f','--no-check-update'],{stdio:'inherit'});
const source=fs.readFileSync('.github/workflows/maintainer.md','utf8');
const images=Object.fromEntries([...source.matchAll(/^      (agent|apiProxy|squid): (ghcr[^\n]+)$/gm)].map(m=>[m[1],m[2]]));
if(Object.keys(images).length!==3) throw Error('expected three digest-qualified AWF images');
let text=fs.readFileSync('.github/workflows/maintainer.lock.yml','utf8');
// v0.89.21 omits pre-activation permissions and rejects them in frontmatter.
// Scope read access to the trusted checkout/coverage admission job only.
const admissionStart=text.indexOf('  pre_activation:\n');
const admissionEnd=text.indexOf('\n  safe_outputs:\n',admissionStart);
if(admissionStart<0 || admissionEnd<0) throw Error('expected admission job boundaries');
const admission=text.slice(admissionStart,admissionEnd);
if((admission.match(/^  [a-z_]+:$/gm)||[]).length!==1 || /\n    permissions:/.test(admission) || !admission.includes('\n    steps:')) throw Error('admission permission correction point drift');
text=text.slice(0,admissionStart)+admission.replace('\n    steps:', '\n    permissions:\n      contents: read\n      actions: read\n    steps:')+text.slice(admissionEnd);
const start=text.indexOf('  detection:\n');
if(start<0) throw Error('detector missing');
let detector=text.slice(start);
for(const image of Object.values(images)) detector=detector.split(image.split('@')[0]).join(image).replaceAll(image+image.slice(image.indexOf('@')),image);
const tag='"imageTag":"0.28.25"';
if(!detector.includes(tag)) throw Error('expected detector imageTag correction point');
detector=detector.replace(tag,'"images":'+JSON.stringify(images));
text=text.slice(0,start)+detector;
if(text.split('\n').some(line=>line.includes('uses: github/gh-aw/actions/setup@') && !line.includes('c35393777e5604a63721d09512263b1383301d4f'))) throw Error('setup action SHA drift');
// Match the pinned action's release label for online action-pin verification.
text=text.replaceAll('github/gh-aw/actions/setup@c35393777e5604a63721d09512263b1383301d4f # c35393777e5604a63721d09512263b1383301d4f','github/gh-aw/actions/setup@c35393777e5604a63721d09512263b1383301d4f # v0.89.21');
fs.writeFileSync('.github/workflows/maintainer.lock.yml',text);
execFileSync('python3',['.github/agent-delivery/correct-publisher-permissions.py'],{stdio:'inherit'});
