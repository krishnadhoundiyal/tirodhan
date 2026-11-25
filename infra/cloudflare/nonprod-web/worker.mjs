const gateway = 'https://tirodhan-np-tdnp01-apim.azure-api.net';
const sites = {
  'https://customer-nonprod.qtkservice.com': '/web/customer',
  'https://manager-nonprod.qtkservice.com': '/web/manager',
};
const forwardedHeaders = [
  'accept',
  'accept-language',
  'if-modified-since',
  'if-none-match',
  'if-range',
  'range',
];

function error(status, message, headers = {}) {
  return new Response(message, {
    status,
    headers: { 'Cache-Control': 'no-store', ...headers },
  });
}

export default {
  async fetch(request, env) {
    const incoming = new URL(request.url);
    const prefix = sites[incoming.origin];
    if (!prefix) return error(404, 'Not found');
    if (request.method !== 'GET' && request.method !== 'HEAD')
      return error(405, 'Method not allowed', { Allow: 'GET, HEAD' });
    const key = env.WEB_EDGE_KEY;
    if (typeof key !== 'string' || !/^[a-f0-9]{64}$/.test(key))
      return error(503, 'Service unavailable');

    const upstream = new URL(gateway);
    upstream.pathname = prefix + incoming.pathname;
    upstream.search = incoming.search;
    const headers = new Headers({ 'X-Tirodhan-Web-Edge': key });
    for (const name of forwardedHeaders) {
      const value = request.headers.get(name);
      if (value !== null) headers.set(name, value);
    }

    try {
      const response = await fetch(upstream, {
        method: request.method,
        headers,
        redirect: 'manual',
        cache: 'no-store',
      });
      if (response.status >= 300 && response.status < 400 && response.status !== 304)
        return error(502, 'Unexpected origin response');
      const outbound = new Headers(response.headers);
      outbound.delete('Set-Cookie');
      outbound.delete('X-Tirodhan-Web-Edge');
      outbound.delete('X-Tirodhan-Web-Origin');
      outbound.set('X-Tirodhan-Web-Route', 'apim');
      outbound.set('Cache-Control', 'no-store');
      return new Response(response.body, {
        status: response.status,
        headers: outbound,
      });
    } catch {
      return error(502, 'Origin unavailable');
    }
  },
};
