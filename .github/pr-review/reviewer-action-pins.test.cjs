const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
for (const workflow of ['pr-review', 'librarian', 'maintainer']) {
  test(`${workflow} retains reviewed generated action versions`, () => {
    const lock = fs.readFileSync(path.join(__dirname, `../workflows/${workflow}.lock.yml`), 'utf8');
    assert.equal((lock.match(/uses: actions\/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9/g) || []).length, 9);
    assert.equal((lock.match(/uses: actions\/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333/g) || []).length, workflow === 'pr-review' ? 7 : 8);
    assert.equal((lock.match(/- name: Setup Node.js\n\s+uses: actions\/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1/g) || []).length, 3);
    assert.doesNotMatch(lock, /uses: actions\/(?:upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a|download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c)/);
  });
}
