const API_HEADERS = {'Content-Type': 'application/json', 'X-Requested-With': 'cloudshell-web-admin'};

async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: API_HEADERS,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401) { window.location.href = '/login'; throw new Error('unauthenticated'); }
  return resp.json();
}

async function loadStatus() {
  const data = await api('GET', '/api/status');
  document.getElementById('proxy-link').textContent = data.proxy_link || '(未知)';
  const qrContainer = document.getElementById('qr-code');
  qrContainer.innerHTML = '';
  if (data.proxy_link && window.QRCode) {
    new QRCode(qrContainer, {text: data.proxy_link, width: 200, height: 200});
  }
}

async function loadAccounts() {
  const data = await api('GET', '/api/accounts');
  const tbody = document.querySelector('#accounts-table tbody');
  tbody.innerHTML = '';
  for (const a of data.accounts) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${a.name}</td><td>${a.email || ''}</td><td>${a.status}</td>` +
      `<td>${a.proxy_label || '直连'}</td><td>${a.is_current ? '✓' : ''}</td>` +
      `<td><button data-name="${a.name}" class="delete-account">删除</button></td>`;
    tbody.appendChild(tr);
  }
  for (const btn of document.querySelectorAll('.delete-account')) {
    btn.addEventListener('click', async () => {
      if (!confirm(`确定删除账号 ${btn.dataset.name}？`)) return;
      await api('DELETE', `/api/accounts/${btn.dataset.name}`);
      loadAccounts();
    });
  }
}

async function loadProxies() {
  const data = await api('GET', '/api/proxies');
  const tbody = document.querySelector('#proxies-table tbody');
  tbody.innerHTML = '';
  const select = document.getElementById('new-account-proxy');
  select.innerHTML = '<option value="">直连</option>';
  for (const p of data.proxies) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${p.label}</td><td>${p.url}</td>` +
      `<td><button data-id="${p.id}" class="delete-proxy">删除</button></td>`;
    tbody.appendChild(tr);
    const opt = document.createElement('option');
    opt.value = p.id; opt.textContent = p.label;
    select.appendChild(opt);
  }
  for (const btn of document.querySelectorAll('.delete-proxy')) {
    btn.addEventListener('click', async () => {
      if (!confirm('确定删除这个代理？')) return;
      await api('DELETE', `/api/proxies/${btn.dataset.id}`);
      loadProxies();
    });
  }
}

async function loadLogs() {
  const data = await api('GET', '/api/logs');
  document.getElementById('log-tail').textContent = data.lines.join('\n');
}

document.getElementById('logout-btn').addEventListener('click', async () => {
  await fetch('/logout', {method: 'POST'});
  window.location.href = '/login';
});

document.getElementById('force-failover-btn').addEventListener('click', async () => {
  await api('POST', '/api/force-failover');
  alert('已请求强制 failover');
});

document.getElementById('add-proxy-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const label = document.getElementById('proxy-label').value;
  const url = document.getElementById('proxy-url').value;
  const result = await api('POST', '/api/proxies', {label, url});
  if (result.error) { alert(result.error); return; }
  document.getElementById('proxy-label').value = '';
  document.getElementById('proxy-url').value = '';
  loadProxies();
});

const dialog = document.getElementById('add-account-dialog');
document.getElementById('add-account-btn').addEventListener('click', () => {
  document.getElementById('login-output').textContent = '';
  document.getElementById('login-code-form').style.display = 'none';
  document.getElementById('add-account-form').style.display = 'block';
  dialog.showModal();
});
document.getElementById('cancel-add-account').addEventListener('click', () => dialog.close());

let pollTimer = null;
document.getElementById('add-account-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('new-account-name').value;
  const proxy_id = document.getElementById('new-account-proxy').value || null;
  const result = await api('POST', '/api/accounts', {name, proxy_id});
  if (result.error) { alert(result.error); return; }
  document.getElementById('add-account-form').style.display = 'none';
  document.getElementById('login-code-form').style.display = 'block';
  pollTimer = setInterval(async () => {
    const out = await api('GET', `/api/accounts/${name}/output`);
    document.getElementById('login-output').textContent = out.output;
    if (out.done) {
      clearInterval(pollTimer);
      alert(out.success ? '登录成功' : '登录失败，请查看输出');
      if (out.success) { dialog.close(); loadAccounts(); }
    }
  }, 1500);
});
document.getElementById('login-code-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('new-account-name').value;
  const text = document.getElementById('login-code').value;
  await api('POST', `/api/accounts/${name}/input`, {text});
  document.getElementById('login-code').value = '';
});

loadStatus();
loadAccounts();
loadProxies();
loadLogs();
setInterval(loadLogs, 5000);
