import assert from 'node:assert/strict';
import { test } from 'node:test';
import worker from './worker.mjs';

const env = { WEB_EDGE_KEY: 'a7'.repeat(32) };
const customer = 'https://customer-nonprod.qtkservice.com';
const manager = 'https://manager-nonprod.qtkservice.com';
const gateway = 'https://tirodhan-np-tdnp01-apim.azure-api.net';

test('each hostname reaches only its APIM route, with the path and query preserved', async (t) => {
  const upstream = t.mock.method(globalThis, 'fetch', async () =>
    new Response('asset', { headers: { 'Content-Type': 'image/svg+xml' } }),
  );
  for (const [origin, prefix] of [[customer, 'customer'], [manager, 'manager']]) {
    const request = new Request(origin + '/tirodhan-lotus.svg?version=2', {
      headers: {
        'X-Tirodhan-Web-Edge': 'spoofed',
        'X-Tirodhan-Web-Origin': 'spoofed',
        Authorization: 'browser-access',
        Cookie: 'browser-session=opaque',
        Accept: 'image/svg+xml',
      },
    });
    const response = await worker.fetch(request, env);
    const [url, options] = upstream.mock.calls.at(-1).arguments;
    assert.equal(String(url), gateway + '/web/' + prefix + '/tirodhan-lotus.svg?version=2');
    assert.equal(options.headers.get('X-Tirodhan-Web-Edge'), env.WEB_EDGE_KEY);
    assert.equal(options.headers.get('X-Tirodhan-Web-Origin'), null);
    assert.equal(options.headers.get('Authorization'), null);
    assert.equal(options.headers.get('Cookie'), null);
    assert.equal(options.headers.get('Accept'), 'image/svg+xml');
    assert.equal(options.redirect, 'manual');
    assert.equal(await response.text(), 'asset');
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal(response.headers.get('x-tirodhan-web-route'), 'apim');
  }
});

test('unrecognized hostnames, unsafe methods and missing credentials stay at the edge', async (t) => {
  const upstream = t.mock.method(globalThis, 'fetch', async () => new Response());
  assert.equal((await worker.fetch(new Request('https://other.qtkservice.com/'), env)).status, 404);
  assert.equal((await worker.fetch(new Request(customer, { method: 'POST' }), env)).status, 405);
  assert.equal((await worker.fetch(new Request(manager), {})).status, 503);
  assert.equal(upstream.mock.callCount(), 0);
});

test('the Worker preserves HEAD and status codes and suppresses origin redirects and cookies', async (t) => {
  const upstream = t.mock.method(globalThis, 'fetch', async () =>
    new Response(null, { status: 404, headers: { 'Set-Cookie': 'test=1' } }),
  );
  const response = await worker.fetch(new Request(manager + '/missing', { method: 'HEAD' }), env);
  assert.equal(upstream.mock.calls[0].arguments[1].method, 'HEAD');
  assert.equal(response.status, 404);
  assert.equal(response.headers.get('Set-Cookie'), null);
  upstream.mock.mockImplementation(async () =>
    Response.redirect(gateway + '/elsewhere', 302),
  );
  assert.equal((await worker.fetch(new Request(customer), env)).status, 502);
});
