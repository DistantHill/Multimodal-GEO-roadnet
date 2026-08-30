import './style.css';

const app = document.querySelector('#app');

app.innerHTML = `
  <section class="shell">
    <p class="eyebrow">MULTIMODAL GEO ROADNET</p>
    <h1>道路网络重建开发控制台</h1>
    <p class="lede">Frontend → Backend → PostgreSQL / Qdrant</p>
    <div class="card">
      <span class="label">Backend</span>
      <strong id="status">检查中…</strong>
    </div>
  </section>
`;

const status = document.querySelector('#status');

fetch('/api/health')
  .then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  })
  .then((payload) => {
    status.textContent = `${payload.service} · ${payload.status}`;
    status.className = 'ok';
  })
  .catch((error) => {
    status.textContent = `不可用 · ${error.message}`;
    status.className = 'error';
  });
