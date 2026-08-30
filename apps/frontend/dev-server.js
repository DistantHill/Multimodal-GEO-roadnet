import { createReadStream, existsSync, readFileSync, watch } from 'node:fs';
import { createServer, request as httpRequest } from 'node:http';
import { extname, join, normalize, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));
const port = Number(process.env.PORT || 5173);
const host = process.env.HOST || '0.0.0.0';
const clients = new Set();

const contentTypes = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
};

function safePath(urlPath) {
  const pathname = decodeURIComponent(urlPath.split('?')[0]);
  const candidate = normalize(join(root, pathname === '/' ? 'index.html' : pathname));
  const rootWithSeparator = root.endsWith(sep) ? root : `${root}${sep}`;
  return candidate === root || candidate.startsWith(rootWithSeparator) ? candidate : null;
}

function proxyApi(request, response) {
  const upstream = httpRequest(
    { hostname: 'backend', port: 8000, path: request.url, method: request.method, headers: request.headers },
    (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    },
  );
  upstream.on('error', () => response.writeHead(502).end('Backend unavailable'));
  request.pipe(upstream);
}

const server = createServer((request, response) => {
  if (request.url === '/__hmr') {
    response.writeHead(200, {
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
      'Content-Type': 'text/event-stream',
    });
    response.write(': connected\n\n');
    clients.add(response);
    request.on('close', () => clients.delete(response));
    return;
  }

  if (request.url?.startsWith('/api/')) {
    proxyApi(request, response);
    return;
  }

  const filePath = safePath(request.url || '/');
  if (!filePath || !existsSync(filePath)) {
    response.writeHead(404).end('Not found');
    return;
  }

  const contentType = contentTypes[extname(filePath)] || 'application/octet-stream';
  response.writeHead(200, { 'Content-Type': contentType });
  if (filePath.endsWith('index.html')) {
    const html = readFileSync(filePath, 'utf8').replace(
      '</body>',
      '<script>new EventSource("/__hmr").onmessage = () => location.reload();</script></body>',
    );
    response.end(html);
  } else {
    createReadStream(filePath).pipe(response);
  }
});

watch(join(root, 'src'), { recursive: true }, () => {
  for (const client of clients) client.write('data: reload\n\n');
});

server.listen(port, host, () => {
  console.log(`Frontend dev server listening on http://${host}:${port}`);
});
