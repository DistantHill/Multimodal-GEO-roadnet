import { createReadStream, watch } from 'node:fs';
import { readFile, stat } from 'node:fs/promises';
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
  if (pathname.includes('\0')) throw new URIError('Invalid path');
  const candidate = normalize(join(root, pathname === '/' ? 'index.html' : pathname));
  const rootWithSeparator = root.endsWith(sep) ? root : `${root}${sep}`;
  return candidate === root || candidate.startsWith(rootWithSeparator) ? candidate : null;
}

async function serveStatic(request, response) {
  let filePath;
  try {
    filePath = safePath(request.url || '/');
  } catch {
    response.writeHead(400).end('Invalid path');
    return;
  }
  try {
    if (!filePath || !(await stat(filePath)).isFile()) {
      response.writeHead(404).end('Not found');
      return;
    }

    const headers = { 'Content-Type': contentTypes[extname(filePath)] || 'application/octet-stream' };
    if (filePath.endsWith('index.html')) {
      const html = (await readFile(filePath, 'utf8')).replace(
        '</body>',
        '<script>new EventSource("/__hmr").onmessage = () => location.reload();</script></body>',
      );
      response.writeHead(200, headers).end(html);
    } else {
      const stream = createReadStream(filePath);
      stream.on('error', (error) => {
        if (response.headersSent) response.destroy(error);
        else response.writeHead(error.code === 'ENOENT' ? 404 : 500).end('File unavailable');
      });
      response.on('close', () => stream.destroy());
      stream.on('open', () => {
        response.writeHead(200, headers);
        stream.pipe(response);
      });
    }
  } catch (error) {
    response.writeHead(['ENOENT', 'ENOTDIR'].includes(error.code) ? 404 : 500).end('File unavailable');
  }
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

  void serveStatic(request, response);
});

watch(join(root, 'src'), { recursive: true }, () => {
  for (const client of clients) client.write('data: reload\n\n');
});

server.listen(port, host, () => {
  console.log(`Frontend dev server listening on http://${host}:${server.address().port}`);
});
