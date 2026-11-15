const test = require("node:test");
const assert = require("node:assert/strict");
const { describe } = require("../static/js/upload-progress.js");

test("calculates transferred bytes, average speed and ETA", () => {
  assert.deepEqual(describe(1048576, 5242880, 2000), {
    percent: 20, complete: false,
    text: "1.0 MiB / 5.0 MiB · 512.0 KiB/s · ~8s remaining",
  });
});
test("does not claim completion for nearly complete requests", () => {
  assert.equal(describe(9999, 10000, 1000).percent, 99);
  assert.equal(describe(9999, 10000, 1000).complete, false);
  assert.equal(describe(10000, 10000, 1000).complete, true);
  assert.doesNotMatch(describe(10000, 10000, 1000).text, /remaining/);
});
test("unknown totals have speed but no made-up percentage or ETA", () => {
  assert.deepEqual(describe(2048, null, 1000), {
    percent: null, complete: false, text: "2.0 KiB sent · 2.0 KiB/s",
  });
});
test("initial and invalid measurements never produce infinite estimates", () => {
  for (const args of [[0, 100, 0], [100, 1000, 0], [-1, 0, -1], [NaN, Infinity, NaN]]) {
    assert.match(describe(...args).text, /Calculating speed/);
    assert.doesNotMatch(describe(...args).text, /NaN|Infinity/);
  }
});
test("formats longer estimates and clamps oversized progress events", () => {
  assert.match(describe(1024, 63488, 1000).text, /~1m 1s remaining/);
  assert.match(describe(1, 3602, 1000).text, /~1h 0m remaining/);
  assert.equal(describe(1500, 1000, 1000).percent, 100);
});
