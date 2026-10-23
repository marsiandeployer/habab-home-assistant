import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';

const listen = (server) => new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const close = (server) => new Promise((resolve) => server.close(resolve));
const run = (proxy, url) => new Promise((resolve, reject) => {
  const child = spawn(process.execPath, ['--input-type=module', '-e', `
    try {
      const response = await fetch(${JSON.stringify(url)}, {signal: AbortSignal.timeout(2000)});
      process.stdout.write(await response.text(), () => process.exit(0));
    } catch { process.stdout.write('connection_failed', () => process.exit(0)); }
  `], {
    env: { NODE_USE_ENV_PROXY: '1', HTTP_PROXY: proxy, HTTPS_PROXY: proxy, NO_PROXY: '' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  const timer = setTimeout(() => child.kill(), 5000);
  child.stdout.on('data', (data) => { output += data; });
  child.on('error', reject);
  child.on('close', (code) => {
    clearTimeout(timer);
    if (code !== 0) reject(new Error('proxy_probe_failed'));
    else resolve(output);
  });
});

test('worker Node fetch uses the explicitly configured proxy for HTTP', async () => {
  const requests = [];
  const server = http.createServer((request, response) => {
    requests.push(request.url);
    response.setHeader('Connection', 'close');
    response.end('proxy_reached');
  });
  server.on('connect', (request, socket) => {
    requests.push(request.url);
    socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    socket.once('data', () => socket.end('HTTP/1.1 200 OK\r\nContent-Length: 13\r\nConnection: close\r\n\r\nproxy_reached'));
  });
  await listen(server);
  try {
    const result = await run(`http://127.0.0.1:${server.address().port}`, 'http://unresolvable.invalid/getMe');
    assert.equal(result, 'proxy_reached');
    assert.equal(requests.length, 1);
    assert.ok(['http://unresolvable.invalid/getMe', 'unresolvable.invalid:80'].includes(requests[0]));
  } finally { await close(server); }
});

test('HTTPS uses CONNECT and proxy failure does not become a successful direct request', async () => {
  const tunnels = [];
  const server = http.createServer();
  server.on('connect', (request, socket) => {
    tunnels.push(request.url);
    socket.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
  });
  await listen(server);
  try {
    const result = await run(`http://127.0.0.1:${server.address().port}`, 'https://unresolvable.invalid/botfixture/getMe');
    assert.equal(result, 'connection_failed');
    assert.ok(tunnels.length >= 1);
    assert.ok(tunnels.every((authority) => authority === 'unresolvable.invalid:443'));
  } finally { await close(server); }
});
