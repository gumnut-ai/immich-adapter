// Compile with the reviewed native release; common corrections belong to automation.
const fs = require('node:fs');
const { execFileSync } = require('node:child_process');
const { correctDetectorImages, normalizeActionReferences } = require('../../.shared-automation/native-toolchain/gh-aw-v0.89.21.cjs');
const { correctReviewedActionPins } = require('./reviewed-action-pins.cjs');
const compiler = process.argv[2];
const workflow = process.argv[3] || 'maintainer';
if (!compiler || !['maintainer', 'librarian'].includes(workflow)) throw Error('usage: node .github/maintainer/compile.cjs /path/to/gh-aw [maintainer|librarian]');
execFileSync(compiler, ['compile', workflow, '--action-tag', 'c35393777e5604a63721d09512263b1383301d4f', '--no-check-update'], { stdio: 'inherit' });
const source = fs.readFileSync(`.github/workflows/${workflow}.md`, 'utf8');
let text = correctDetectorImages(source, fs.readFileSync(`.github/workflows/${workflow}.lock.yml`, 'utf8'));
text = correctReviewedActionPins(text, workflow);
text = normalizeActionReferences(source, text);
fs.writeFileSync(`.github/workflows/${workflow}.lock.yml`, text);
execFileSync('python3', ['.github/agent-delivery/correct-publisher-permissions.py'], { stdio: 'inherit' });
