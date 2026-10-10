const fs = require('node:fs');
const path = require('node:path');

const MAX_BYTES = 8 * 1024 * 1024;
const subtypes = new Set(['success', 'error_during_execution', 'error_max_turns', 'error_max_budget_usd', 'error_max_structured_output_retries']);
const tokenFields = ['inputTokens', 'outputTokens', 'cacheReadInputTokens', 'cacheCreationInputTokens'];
const number = (value, integer = false) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER && (!integer || Number.isSafeInteger(value)) ? value : null;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);

function projectUsage(messages) {
  const unavailable = { schema_version: 1, availability: 'unavailable', sdk_status: 'unavailable' };
  if (!Array.isArray(messages)) return unavailable;
  // These are cumulative totals. Summing multiple result events double counts.
  const result = messages.findLast(message => message?.type === 'result');
  if (!object(result)) return unavailable;
  const subtype = subtypes.has(result.subtype) ? result.subtype : 'unavailable';
  const report = {
    schema_version: 1,
    availability: 'partial',
    sdk_status: subtype === 'success' && result.is_error === false ? 'success' : subtype === 'unavailable' ? 'unavailable' : 'error',
    sdk_subtype: subtype,
    num_turns: number(result.num_turns, true),
    duration_ms: number(result.duration_ms, true),
    permission_denials_count: Array.isArray(result.permission_denials) ? result.permission_denials.length : null,
    total_cost_usd: number(result.total_cost_usd),
    cost_basis: 'unavailable',
    models: [],
    tokens: Object.fromEntries(tokenFields.map(field => [field, null])),
  };
  if (object(result.modelUsage)) {
    const entries = Object.entries(result.modelUsage);
    if (entries.length > 0 && entries.length <= 32) {
      report.models = entries.map(([model, usage], index) => ({
        // No arbitrary upstream strings reach logs, Markdown, or artifacts.
        model: /^claude-(?:opus|haiku|sonnet)-\d{1,2}-\d{1,2}(?:-\d{8})?$/.test(model) ? model : `unknown_model_${index + 1}`,
        ...Object.fromEntries(tokenFields.map(field => [field, number(usage?.[field], true)])),
        costUSD: number(usage?.costUSD),
        cost_basis: usage?.costBasis === undefined || usage?.costBasis === 'list' ? 'list' : usage?.costBasis === 'managed' ? 'managed' : 'unknown',
      }));
      for (const field of tokenFields) {
        const values = report.models.map(model => model[field]);
        report.tokens[field] = values.every(value => value !== null) ? number(values.reduce((sum, value) => sum + value, 0), true) : null;
      }
      report.cost_basis = report.models.every(model => model.cost_basis === 'list') ? 'list' : 'mixed_or_unknown';
    }
  }
  if (report.sdk_status !== 'unavailable' && [report.num_turns, report.duration_ms, report.permission_denials_count, report.total_cost_usd, ...Object.values(report.tokens), ...report.models.map(model => model.costUSD)].every(value => value !== null) && report.models.length) report.availability = 'available';
  return report;
}

function readUsage(file, runnerTemp) {
  try {
    // Upstream writes exactly this file. Reject unexpected paths or symlinks.
    const expected = path.resolve(runnerTemp, 'claude-execution-output.json');
    if (path.resolve(file || expected) !== expected) return projectUsage(null);
    const fd = fs.openSync(expected, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    try {
      const stat = fs.fstatSync(fd);
      if (!stat.isFile() || stat.size > MAX_BYTES) return projectUsage(null);
      // A bounded read also protects against a file growing after fstat.
      const buffer = Buffer.alloc(MAX_BYTES + 1);
      const length = fs.readSync(fd, buffer, 0, buffer.length, 0);
      if (length > MAX_BYTES) return projectUsage(null);
      return projectUsage(JSON.parse(buffer.subarray(0, length).toString('utf8')));
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return projectUsage(null);
  }
}

function summary(report) {
  const value = number => number === null || number === undefined ? 'unavailable' : String(number);
  const lines = ['### Claude review usage', '', `SDK execution status: **${report.sdk_status}**. Usage: **${report.availability}**.`, '', 'Execution success does not verify PR comment delivery or complete review coverage.'];
  if (report.availability === 'unavailable') return `${lines.join('\n')}\n\nNo usable bounded SDK result was available; authentication or execution may have failed before saving it.\n`;
  lines.push('', `Turns: ${value(report.num_turns)}; SDK duration: ${value(report.duration_ms)} ms; permission denials: ${value(report.permission_denials_count)}.`, '', `Estimated cost: $${value(report.total_cost_usd)} (${report.cost_basis === 'list' ? 'list-price equivalent' : 'pricing basis mixed or unknown'}); this is not a subscription charge or API invoice.`, '', '| Model | Input | Output | Cache read | Cache creation | Estimated USD |', '| --- | ---: | ---: | ---: | ---: | ---: |');
  for (const model of report.models) lines.push(`| ${model.model} | ${tokenFields.map(field => value(model[field])).join(' | ')} | ${value(model.costUSD)} |`);
  lines.push(`| Total | ${tokenFields.map(field => value(report.tokens[field])).join(' | ')} | ${value(report.total_cost_usd)} |`, '', 'Totals use the latest cumulative SDK modelUsage, including query-pipeline helpers; external auxiliary calls are excluded.');
  return `${lines.join('\n')}\n`;
}

if (require.main === module) {
  const report = readUsage(process.env.CLAUDE_EXECUTION_FILE, process.env.RUNNER_TEMP);
  const output = path.join(process.env.RUNNER_TEMP, 'claude-review-usage.json');
  fs.writeFileSync(output, `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
  if (process.env.GITHUB_STEP_SUMMARY) fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary(report));
}

module.exports = { MAX_BYTES, projectUsage, readUsage, summary };
