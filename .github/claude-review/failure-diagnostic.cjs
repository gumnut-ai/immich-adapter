const fs = require('node:fs');
const path = require('node:path');

// SDK 0.3.278 error categories are fixed strings. Never log model-authored text,
// result/errors fields, tool output, or file/parser exceptions.
const categories = new Set([
  'authentication_failed', 'oauth_org_not_allowed', 'account_on_hold',
  'verification_required', 'billing_error', 'cloud_credential_error',
  'rate_limit', 'overloaded', 'invalid_request', 'model_not_found',
  'server_error', 'unknown', 'max_output_tokens',
]);
const resultFailures = new Set([
  'error_during_execution', 'error_max_turns', 'error_max_budget_usd',
  'error_max_structured_output_retries',
]);

function failureCategory(messages) {
  if (!Array.isArray(messages)) return 'diagnostic_unavailable';
  const assistant = messages.findLast(message => message?.type === 'assistant' && message.error);
  if (assistant) return categories.has(assistant.error) ? assistant.error : 'unknown';
  const result = messages.findLast(message => message?.type === 'result');
  return resultFailures.has(result?.subtype) ? result.subtype : 'unknown';
}

if (require.main === module) {
  let category = 'diagnostic_unavailable';
  try {
    const file = path.join(process.env.RUNNER_TEMP, 'claude-execution-output.json');
    if (fs.statSync(file).size <= 8 * 1024 * 1024) {
      category = failureCategory(JSON.parse(fs.readFileSync(file, 'utf8')));
    }
  } catch {
    // The action may fail before saving its SDK output. Preserve its failure.
  }
  console.log(`::notice::Claude review failure category: ${category}. Raw execution output withheld.`);
}

module.exports = { failureCategory };
