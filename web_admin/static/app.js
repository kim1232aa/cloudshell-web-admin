const API_HEADERS = {'Content-Type': 'application/json', 'X-Requested-With': 'cloudshell-web-admin'};

async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: API_HEADERS,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401) { window.location.href = '/login'; throw new Error('unauthenticated'); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = (data && data.error) ? data.error : `HTTP ${resp.status}`;
    throw new Error(err);
  }
  return data;
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

function renderProvisionCell(a) {
  const p = a.provision || {};
  const state = p.state || 'not_provisioned';
  let badge = '';
  let btnText = '部署';
  if (state === 'ok') {
    badge = '<span style="color:#28a745; font-weight:bold;">已部署</span>';
    btnText = '重新部署';
  } else if (state === 'running') {
    badge = '<span style="color:#007bff; font-weight:bold;">部署中...</span>';
  } else if (state === 'failed') {
    badge = `<span style="color:#dc3545; font-weight:bold;" title="${p.error || ''}">失败</span>`;
    btnText = '重试';
  } else if (state === 'interrupted') {
    badge = '<span style="color:#ffc107; font-weight:bold;">中断</span>';
    btnText = '重新部署';
  } else {
    badge = '<span style="color:#6c757d;">未部署</span>';
  }

  const viewLog = (state !== 'not_provisioned')
    ? `<a href="#" class="view-prov-log" data-name="${a.name}" style="margin-left:6px; font-size:12px;">日志</a>`
    : '';

  const actionBtn = (state === 'running')
    ? ''
    : `<button data-name="${a.name}" class="deploy-account" style="margin-left:6px; font-size:12px;">${btnText}</button>`;

  return `${badge}${viewLog}${actionBtn}`;
}

async function showProvisionLog(name) {
  const box = document.getElementById('provision-log-box');
  const tail = document.getElementById('provision-log-tail');
  const title = document.getElementById('provision-log-account');
  title.textContent = name;
  box.style.display = 'block';
  tail.textContent = '加载日志中...';
  try {
    const data = await api('GET', `/api/accounts/${name}/provision`);
    tail.textContent = data.log || `(暂无日志，状态: ${data.state})`;
  } catch (err) {
    tail.textContent = `加载日志失败: ${err.message}`;
  }
}

document.getElementById('close-provision-log').addEventListener('click', () => {
  document.getElementById('provision-log-box').style.display = 'none';
});

let accountsPollTimer = null;

async function loadAccounts() {
  const [accountsData, proxiesData] = await Promise.all([
    api('GET', '/api/accounts'),
    api('GET', '/api/proxies'),
  ]);
  const tbody = document.querySelector('#accounts-table tbody');
  tbody.innerHTML = '';
  let hasRunningProvision = false;

  for (const a of accountsData.accounts) {
    const tr = document.createElement('tr');
    const options = ['<option value="">直连</option>'].concat(
      proxiesData.proxies.map(p =>
        `<option value="${p.id}"${p.id === a.proxy_id ? ' selected' : ''}>${p.label}</option>`)
    ).join('');

    const provHtml = renderProvisionCell(a);
    if (a.provision && a.provision.state === 'running') {
      hasRunningProvision = true;
    }

    tr.innerHTML = `<td>${a.name}</td><td>${a.email || ''}</td><td>${a.status}</td>` +
      `<td><select class="account-proxy-select" data-name="${a.name}">${options}</select></td>` +
      `<td>${a.is_current ? '✓' : ''}</td>` +
      `<td>${provHtml}</td>` +
      `<td><button data-name="${a.name}" class="delete-account">删除</button></td>`;
    tbody.appendChild(tr);
  }

  // Bind delete handlers
  for (const btn of document.querySelectorAll('.delete-account')) {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.name;
      const row = accountsData.accounts.find(x => x.name === name);
      let promptMsg = `确定删除账号 ${name}？`;
      if (row && row.is_current) {
        promptMsg = `警告：账号 ${name} 是当前 watchdog 正在使用的活跃账号！\n确定删除该账号配置？`;
      }
      if (!confirm(promptMsg)) return;

      try {
        const res = await api('DELETE', `/api/accounts/${name}`);
        if (res.warning) {
          alert(`提示：${res.warning}`);
        }
      } catch (err) {
        alert(`删除失败: ${err.message}`);
      }
      loadAccounts();
    });
  }

  // Bind proxy select handlers
  for (const sel of document.querySelectorAll('.account-proxy-select')) {
    sel.addEventListener('change', async () => {
      try {
        await api('PUT', `/api/accounts/${sel.dataset.name}/proxy`, {proxy_id: sel.value || null});
      } catch (err) {
        alert(`更新代理失败: ${err.message}`);
      }
    });
  }

  // Bind deploy buttons
  for (const btn of document.querySelectorAll('.deploy-account')) {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.name;
      if (!confirm(`确定为账号 ${name} 执行 Cloud Shell 代理栈自动部署？`)) return;
      try {
        await api('POST', `/api/accounts/${name}/provision`);
        loadAccounts();
        showProvisionLog(name);
      } catch (err) {
        alert(`发起部署失败: ${err.message}`);
      }
    });
  }

  // Bind view log links
  for (const link of document.querySelectorAll('.view-prov-log')) {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      showProvisionLog(link.dataset.name);
    });
  }

  // If any deployment is in progress, poll accounts table every 5s
  if (hasRunningProvision && !accountsPollTimer) {
    accountsPollTimer = setInterval(loadAccounts, 5000);
  } else if (!hasRunningProvision && accountsPollTimer) {
    clearInterval(accountsPollTimer);
    accountsPollTimer = null;
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
      try {
        await api('DELETE', `/api/proxies/${btn.dataset.id}`);
      } catch (err) {
        alert(`删除代理失败: ${err.message}`);
      }
      loadProxies();
    });
  }
}

async function loadLogs() {
  try {
    const data = await api('GET', '/api/logs');
    document.getElementById('log-tail').textContent = data.lines.join('\n');
  } catch (e) {}
}

document.getElementById('logout-btn').addEventListener('click', async () => {
  await fetch('/logout', {method: 'POST'});
  window.location.href = '/login';
});

document.getElementById('force-failover-btn').addEventListener('click', async () => {
  try {
    await api('POST', '/api/force-failover');
    alert('已请求强制 failover');
  } catch (err) {
    alert(`请求失败: ${err.message}`);
  }
});

document.getElementById('add-proxy-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const label = document.getElementById('proxy-label').value;
  const url = document.getElementById('proxy-url').value;
  try {
    await api('POST', '/api/proxies', {label, url});
    document.getElementById('proxy-label').value = '';
    document.getElementById('proxy-url').value = '';
    loadProxies();
  } catch (err) {
    alert(`添加代理失败: ${err.message}`);
  }
});

const dialog = document.getElementById('add-account-dialog');
document.getElementById('add-account-btn').addEventListener('click', () => {
  document.getElementById('login-output').textContent = '';
  document.getElementById('login-helper').style.display = 'none';
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
  try {
    await api('POST', '/api/accounts', {name, proxy_id});
  } catch (err) {
    alert(`发起登录失败: ${err.message}`);
    return;
  }
  document.getElementById('add-account-form').style.display = 'none';
  document.getElementById('login-code-form').style.display = 'block';

  pollTimer = setInterval(async () => {
    try {
      const out = await api('GET', `/api/accounts/${name}/output`);
      const outputText = out.output || '';
      document.getElementById('login-output').textContent = outputText;

      // Extract and render clickable Google OAuth URL if present
      const match = outputText.match(/https:\/\/accounts\.google\.com\/o\/oauth2\/auth\?[^\s\r\n]+/);
      const helper = document.getElementById('login-helper');
      const link = document.getElementById('login-auth-link');
      if (match) {
        link.href = match[0];
        link.textContent = match[0];
        helper.style.display = 'block';
      }

      if (out.done) {
        clearInterval(pollTimer);
        pollTimer = null;
        if (out.success) {
          alert(`账号 ${name} 登录成功！系统正在后台自动执行 Cloud Shell 代理栈部署，可在账号列表中查看进度。`);
          dialog.close();
          loadAccounts();
          showProvisionLog(name);
        } else {
          alert('登录失败，请查看输出信息。');
        }
      }
    } catch (e) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 1500);
});

document.getElementById('login-code-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('new-account-name').value;
  const text = document.getElementById('login-code').value.trim();
  try {
    await api('POST', `/api/accounts/${name}/input`, {text});
    document.getElementById('login-code').value = '';
  } catch (err) {
    alert(`提交验证码失败: ${err.message}`);
  }
});

loadStatus();
loadAccounts();
loadProxies();
loadLogs();
setInterval(loadLogs, 5000);
