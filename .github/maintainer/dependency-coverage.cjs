// Readback is mandatory: a successful POST or zero alerts is not coverage.
const fs = require('node:fs');
const normalize = url => decodeURIComponent(url.replace(/^pkg:\//, 'pkg:'));

async function readGraph(github, repo) {
  const manifests = [];
  let cursor = null;
  do {
    const result = await github.graphql(`query($owner:String!,$repo:String!,$cursor:String) {
      repository(owner:$owner,name:$repo) {
        dependencyGraphManifests(first:100,after:$cursor) {
          nodes { id filename parseable exceedsMaxSize }
          pageInfo { hasNextPage endCursor }
        }
      }
    }`, {...repo, cursor});
    const page = result.repository.dependencyGraphManifests;
    manifests.push(...page.nodes);
    cursor = page.pageInfo.hasNextPage ? page.pageInfo.endCursor : null;
    if (page.pageInfo.hasNextPage && !cursor) throw Error('Missing manifest pagination cursor');
  } while (cursor);
  return manifests;
}

async function verifyGraph({github, repo, bundle}) {
  const manifests = await readGraph(github, repo);
  const readback = {};
  for (const [path, expected] of Object.entries(bundle.snapshot.manifests)) {
    const matches = manifests.filter(m => m.filename === path);
    if (matches.length !== 1 || !matches[0].parseable || matches[0].exceedsMaxSize)
      throw Error(`Dependency graph missing/unparseable manifest: ${path}`);
    const actual = new Set();
    let cursor = null;
    do {
      const result = await github.graphql(`query($id:ID!,$cursor:String) {
        node(id:$id) { ... on DependencyGraphManifest {
          dependencies(first:100,after:$cursor) {
            nodes { packageUrl }
            pageInfo { hasNextPage endCursor }
          }
        } }
      }`, {id:matches[0].id, cursor});
      const page = result.node.dependencies;
      for (const dependency of page.nodes) {
        if (!dependency.packageUrl) throw Error(`Graph omitted exact package URL: ${path}`);
        actual.add(normalize(dependency.packageUrl));
      }
      cursor = page.pageInfo.hasNextPage ? page.pageInfo.endCursor : null;
      if (page.pageInfo.hasNextPage && !cursor) throw Error('Missing dependency pagination cursor');
    } while (cursor);
    const wanted = new Set(Object.values(expected.resolved).map(d => normalize(d.package_url)));
    if (actual.size !== wanted.size || [...wanted].some(url => !actual.has(url)))
      throw Error(`Dependency graph differs from locked inventory: ${path}; expected ${wanted.size}, read ${actual.size}`);
    readback[path] = {count:actual.size, lock_sha256:bundle.inventory[path].lock_sha256};
  }
  return readback;
}

async function assertCurrentHead({github, context, bundle}) {
  const branch = context.payload.repository.default_branch;
  if (bundle.snapshot.sha !== context.sha || bundle.snapshot.ref !== `refs/heads/${branch}`)
    throw Error('Dependency snapshot must describe this exact default-branch SHA');
  const current = await github.rest.repos.getBranch({...context.repo, branch});
  if (current.data.commit.sha !== context.sha) throw Error('Default branch advanced; retry on its current SHA');
}

async function assertCoverage({github, context, bundle}) {
  await assertCurrentHead({github, context, bundle});
  const runs = await github.paginate(github.rest.actions.listWorkflowRuns, {
    ...context.repo, workflow_id:'maintainer-dependencies.yml', head_sha:context.sha,
    branch:context.payload.repository.default_branch, status:'success', per_page:100,
  });
  let verifiedRun;
  for (const run of runs.filter(run => run.head_sha === context.sha && run.conclusion === 'success' &&
    ['push','schedule','workflow_dispatch'].includes(run.event))) {
    // A workflow whose gated index job was skipped can conclude success. Require
    // the latest attempt's actual job and submission/readback step to succeed.
    const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRun, {
      ...context.repo, run_id:run.id, filter:'latest', per_page:100,
    });
    if (jobs.some(job => job.name === 'index' && job.head_sha === context.sha &&
      job.conclusion === 'success' && job.steps?.some(step =>
        step.name === 'Submit to GitHub and require complete graph readback' && step.conclusion === 'success'))) {
      verifiedRun=run;
      break;
    }
  }
  if (!verifiedRun)
    throw Error('No successful locked-dependency submission/readback on this exact source SHA');
  return {status:'verified', submission_run:verifiedRun.html_url, sha:context.sha, readback:await verifyGraph({github, repo:context.repo, bundle}), inventory:bundle.inventory};
}

async function submit({github, context, bundle, report, sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)), attempts=12}) {
  await assertCurrentHead({github, context, bundle});
  const receipt = {sha:context.sha, inventory:bundle.inventory, status:'incomplete'};
  fs.mkdirSync('/tmp/maintainer-input', {recursive:true});
  const save = () => fs.writeFileSync('/tmp/maintainer-input/dependency-coverage.json', JSON.stringify(receipt,null,2));
  save();
  const response = await github.request('POST /repos/{owner}/{repo}/dependency-graph/snapshots', {...context.repo,...bundle.snapshot});
  receipt.submission = response.data;
  save();
  if (response.status !== 201 || response.data.result !== 'SUCCESS') throw Error('Dependency submission was not accepted');
  let failure;
  for (let attempt=0; attempt<attempts; attempt++) {
    try {
      receipt.readback = await verifyGraph({github, repo:context.repo, bundle});
      await assertCurrentHead({github, context, bundle});
      receipt.status='verified';
      save();
      report(`Dependency coverage verified for ${context.sha}: ${Object.keys(receipt.readback).length} manifests; ${Object.values(receipt.readback).reduce((n,m)=>n+m.count,0)} locked identities.\n`);
      report(JSON.stringify(receipt.inventory,null,2));
      return receipt;
    } catch (error) { failure=error; }
    if (attempt+1<attempts) await sleep(10000);
  }
  throw failure;
}

module.exports={verifyGraph, assertCoverage, submit};
