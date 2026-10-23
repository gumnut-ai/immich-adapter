// Pinned compiler currently needs this generated-only detector image correction.
const fs=require('node:fs');const {execFileSync}=require('node:child_process');
const compiler=process.argv[2];
const workflow=process.argv[3]||'maintainer';
if(!compiler || !['maintainer','librarian'].includes(workflow)) throw Error('usage: node .github/maintainer/compile.cjs /path/to/gh-aw [maintainer|librarian]');
execFileSync(compiler,['compile',workflow,'--action-tag','c35393777e5604a63721d09512263b1383301d4f','--no-check-update'],{stdio:'inherit'});
const source=fs.readFileSync(`.github/workflows/${workflow}.md`,'utf8');
const images=Object.fromEntries([...source.matchAll(/^      (agent|apiProxy|squid): (ghcr[^\n]+)$/gm)].map(m=>[m[1],m[2]]));
if(Object.keys(images).length!==3) throw Error('expected three digest-qualified AWF images');
let text=fs.readFileSync(`.github/workflows/${workflow}.lock.yml`,'utf8');
// v0.89.21 omits pre-activation permissions and rejects them in frontmatter.
// Scope read access to the trusted default-branch admission job only.
const admissionStart=text.indexOf('  pre_activation:\n');
const admissionEnd=text.indexOf('\n  safe_outputs:\n',admissionStart);
if(admissionStart<0 || admissionEnd<0) throw Error('expected admission job boundaries');
const admission=text.slice(admissionStart,admissionEnd);
if((admission.match(/^  [a-z_]+:$/gm)||[]).length!==1 || /\n    permissions:/.test(admission) || !admission.includes('\n    steps:')) throw Error('admission permission correction point drift');
if(workflow==='maintainer') text=text.slice(0,admissionStart)+admission.replace('\n    steps:', '\n    permissions:\n      contents: read\n      actions: read\n    steps:')+text.slice(admissionEnd);
const start=text.indexOf('  detection:\n');
if(start<0) throw Error('detector missing');
let detector=text.slice(start);
for(const image of Object.values(images)) detector=detector.split(image.split('@')[0]).join(image).replaceAll(image+image.slice(image.indexOf('@')),image);
const tag='"imageTag":"0.28.25"';
if(!detector.includes(tag)) throw Error('expected detector imageTag correction point');
detector=detector.replace(tag,'"images":'+JSON.stringify(images));
text=text.slice(0,start)+detector;
if(text.split('\n').some(line=>line.includes('uses: github/gh-aw/actions/setup@') && !line.includes('c35393777e5604a63721d09512263b1383301d4f'))) throw Error('setup action SHA drift');
// v0.89.21 has embedded pins that bypass the action cache in some jobs.
// Keep reviewed artifact pins and generated Node steps; retain source-owned Node steps.
for (const [pattern, replacement, count] of [
  [/uses: actions\/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1/g, 'uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2', 9],
  [/uses: actions\/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1/g, 'uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2', 8],
  [/(- name: Setup Node.js\n\s+uses: actions\/setup-node@)820762786026740c76f36085b0efc47a31fe5020 # v7.0.0/g, (_, prefix) => prefix + '949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0', 3],
]) {
  if ([...text.matchAll(pattern)].length !== count) throw Error('reviewed action pin correction point drift');
  text = text.replace(pattern, replacement);
}
// Preserve release labels on SHA-pinned action references after compilation.
for(const [action,sha,version] of [
  ['github/gh-aw/actions/setup','c35393777e5604a63721d09512263b1383301d4f','v0.89.21'],
  ['oven-sh/setup-bun','0c5077e51419868618aeaa5fe8019c62421857d6','v2.2.0'],
]) text=text.replace(new RegExp(`(${action}@${sha})(?: # [^\n]*)?$`,'gm'),`$1 # ${version}`);
// Preserve reviewed source labels/annotations that native compilation replaces with a SHA.
for (const [, ref, annotation] of source.matchAll(/^\s*(?:- )?uses: (\S+@[a-f0-9]{40}) # (.+)$/gm))
  text = text.split(`${ref} # ${ref.split('@')[1]}`).join(`${ref} # ${annotation}`);
fs.writeFileSync(`.github/workflows/${workflow}.lock.yml`,text);
execFileSync('python3',['.github/agent-delivery/correct-publisher-permissions.py'],{stdio:'inherit'});
