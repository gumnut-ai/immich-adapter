'use strict';
const fs = require('node:fs');
const { spawn } = require('node:child_process');
for (const directory of ['/scratch/home', '/scratch/config', '/scratch/tmp']) fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
const child = spawn('/opt/claude/node_modules/.bin/claude', process.argv.slice(2), { stdio: 'inherit' });
child.on('error', () => { process.stderr.write('Isolated CLI unavailable\n'); process.exitCode = 1; });
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));
child.on('close', (code, signal) => { process.exitCode = signal ? 130 : code ?? 1; });
