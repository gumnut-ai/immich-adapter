// This runs only against the reviewed default branch, before agent admission.
async function poll({github, context, core, fs, eventName, schedule, coverage}) {
  if (coverage?.status !== 'verified' || coverage.sha !== context.sha || !Object.keys(coverage.readback || {}).length)
    throw Error('Missing verified exact-source dependency coverage; zero alerts is not coverage');
  const alerts = await github.paginate(github.rest.dependabot.listAlertsForRepo,
    {...context.repo, state: 'open', per_page: 100});
  const evidence = alerts.map(a => ({number: a.number, package: a.dependency.package,
    manifest: a.dependency.manifest_path, ghsa: a.security_advisory.ghsa_id,
    severity: a.security_vulnerability.severity, range: a.security_vulnerability.vulnerable_version_range,
    fixed: a.security_vulnerability.first_patched_version?.identifier ?? null,
    published: a.security_advisory.published_at, created: a.created_at, url: a.html_url}));
  const routine = eventName === 'workflow_dispatch' || (eventName === 'schedule' && schedule === '0 9 * * *');
  fs.mkdirSync('/tmp/maintainer-input', {recursive:true});
  fs.writeFileSync('/tmp/maintainer-input/alerts.json', JSON.stringify({routine, coverage, alerts: evidence}, null, 2));
  core.setOutput('admit', String(routine || evidence.length > 0));
  core.summary.addRaw('Open dependency alerts: '+evidence.length+'; routine: '+routine+'\n');
  await core.summary.write();
}
module.exports={poll};
