const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {DEFAULT_CONFIG, merge, load, save} = require('../src/config-store.cjs');
test('merge normalizes config and preserves defaults', () => {
  const c = merge(DEFAULT_CONFIG, {releaseDelayMs: 9999, selectedEncoder: 8, slots:[{title:'Build'}], encoders:{0:{clockwise:'X'}}});
  assert.equal(c.releaseDelayMs, 2000); assert.equal(c.selectedEncoder, 3); assert.equal(c.slots.length, 6); assert.equal(c.slots[0].title, 'Build'); assert.equal(c.encoders[0].clockwise, 'X'); assert.equal(c.encoders[1].clockwise, 'ENC_CW');
});
test('save/load uses atomic JSON file', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mirabox-config-')), file = path.join(dir, 'config.json');
  const c = save(file, {releaseDelayMs: 150, autoRelease: false}); assert.equal(load(file).releaseDelayMs, 150); assert.equal(c.autoRelease, false); assert.equal(fs.existsSync(`${file}.tmp-${process.pid}`), false);
  fs.rmSync(dir, {recursive:true, force:true});
});
test('invalid config surfaces an error', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mirabox-config-')), file = path.join(dir, 'config.json'); fs.writeFileSync(file, '{bad'); assert.throws(() => load(file), /Invalid config/); fs.rmSync(dir, {recursive:true, force:true});
});
