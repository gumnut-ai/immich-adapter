// Preserve this repository's reviewed action versions where gh-aw v0.89.21
// bypasses its action cache. Source-owned Node steps retain their own pins.
const fs = require('node:fs');
function correctReviewedActionPins(text, workflow) {
  if (!['maintainer', 'librarian', 'pr-review'].includes(workflow)) throw Error('unknown workflow');
  // v0.89.21 has embedded pins that bypass the action cache in some jobs.
  // Keep reviewed artifact pins and generated Node steps; retain source-owned Node steps.
  for (const [pattern, replacement, count] of [
    [/uses: actions\/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1/g, 'uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2', 9],
    [/uses: actions\/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1/g, 'uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2', workflow === 'pr-review' ? 7 : 8],
    [/(- name: Setup Node.js\n\s+uses: actions\/setup-node@)820762786026740c76f36085b0efc47a31fe5020 # v7.0.0/g, (_, prefix) => prefix + '949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0', 3],
  ]) {
    if ([...text.matchAll(pattern)].length !== count) throw Error('reviewed action pin correction point drift');
    text = text.replace(pattern, replacement);
  }
  return text;
}
module.exports = { correctReviewedActionPins };
if (require.main === module) {
  const [file, workflow] = process.argv.slice(2);
  if (!file) throw Error('usage: node reviewed-action-pins.cjs LOCK WORKFLOW');
  fs.writeFileSync(file, correctReviewedActionPins(fs.readFileSync(file, 'utf8'), workflow));
}
