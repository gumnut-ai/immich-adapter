process.env.TEST_DELIVERY_DAEMON = "maintainer";
require("../librarian/verify-delivery.test.cjs");

// Incomplete discovery must fail delivery even when no candidate was changed.
require('node:test').test('incomplete discovery cannot pass as a no-candidate result', async () => {
  const verify = require('./verify-delivery.cjs').verifyDelivery;
  const args = {
    github: {},
    context: { repo: { owner: 'example', repo: 'example' } },
    env: { AGENT: 'success', DETECTION: 'success', PUBLISHER: 'success',
      OUTPUT_STATUS: 'success', OUTPUT_TYPES: 'report_incomplete', PR_NUMBER: '' },
    report() {},
  };
  const check = require('node:assert/strict');
  await check.rejects(verify(args), /unexpected or incomplete safe output/);
  args.env.OUTPUT_TYPES = 'noop';
  check.equal(await verify(args), 'noop-awaiting-native-conclusion');
});
