import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { get } from 'node:http';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

test('static server rejects malformed paths and directories without exiting', { timeout: 10_000 }, async (t) => {
  const child = spawn(process.execPath, [fileURLToPath(new URL('../dev-server.js', import.meta.url))], {
    env: { ...process.env, HOST: '127.0.0.1', PORT: '0' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  t.after(async () => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    const closed = once(child, 'close');
    child.kill();
    await closed;
  });
  let stderr = '';
  child.stderr.setEncoding('utf8').on('data', (chunk) => { stderr += chunk; });
  const port = await new Promise((resolve, reject) => {
    let output = '';
    child.once('error', reject);
    child.once('exit', (code) => reject(new Error(`Server exited (${code}): ${stderr}`)));
    child.stdout.setEncoding('utf8').on('data', (chunk) => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:(\d+)/);
      if (match) resolve(Number(match[1]));
    });
  });
  const request = (path) => new Promise((resolve, reject) => {
    get({ hostname: '127.0.0.1', port, path }, (response) => {
      let body = '';
      response.setEncoding('utf8').on('data', (chunk) => { body += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, body, headers: response.headers }));
      response.on('error', reject);
    }).on('error', reject);
  });

  for (const [path, expected] of [
    ['/%', 400], ['/%E0%A4%A', 400], ['/%00', 400], ['/src', 404], ['/src/', 404],
    ['/missing.js', 404], ['/src/main.js/child', 404], ['/%2e%2e/package.json', 404],
  ]) {
    assert.equal((await request(path)).status, expected, path);
    const home = await request('/');
    assert.equal(home.status, 200, `server remains healthy after ${path}`);
    assert.match(home.body, /new EventSource/);
  }
  const javascript = await request('/src/selection.js?test=1');
  assert.equal(javascript.status, 200);
  assert.match(javascript.headers['content-type'], /javascript/);
  assert.match(javascript.body, /selectFeaturesInExtent/);
  assert.equal(stderr, '');
});
