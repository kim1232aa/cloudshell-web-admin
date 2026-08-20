# cloudshell-web-admin — design spec

## 背景与目标

`cloudshell-web-admin` 是从 `cloudshell-proxy-autostart` 仓库的 `kui-residential`
分支平移出来的独立仓库（保留完整 git 历史，分支改名为 `main`）。原分支已经包含并验证过：

- 基础层：Cloud Shell + xray(vless+ws) + Cloudflare 命名隧道 + `watchdog.sh` 多账号
  探活/轮换/keepalive
- 住宅层：kui（`kui-local-multi-exit` 的 patch 版）在 Cloud Shell 容器内跑 24 槽
  OpenVPN 出口，`/res-NN` 路径接入同一条隧道
- 动态订阅：`subserver.py` 按请求实时拼 Clash YAML，前置节点(Google DC egress) +
  住宅节点(kui) 分组

这些不变、原样保留。这次设计只新增一块：**web-admin** ——把"加/删 Google 账号"从
CLI (`docker compose run --rm watchdog auth <name>`，需要复制URL、粘贴验证码回终端)
变成网页操作，并加两个原来没有的能力：**代理池** 和 **账号绑代理**。

目的：
1. 账号池（`gcloud config configurations`）可以在网页里增删查，不用再进终端。
2. 每个 Google 账号的 `gcloud auth login` 以及后续所有 gcloud 调用（`cloud-shell ssh`、
   `print-access-token`）都走该账号绑定的代理出口，而不是 VPS 裸 IP —— 降低多账号
   从同一 IP 操作被 Google 关联判定的风险。代理列表由用户自己维护（外部现成代理，
   不依赖 kui 自己的住宅槽位）。

因为 web-admin 和 `watchdog.sh` 现在同仓库同一个 `docker-compose.yml`、共享同一个
`./state` bind mount，不存在跨仓库耦合问题，之前讨论过的"要不要共享卷"不再是问题。

公网可访问（VPS 部署，需要域名 + HTTPS）。

## 架构

```
公网
 │ HTTPS(443)
 ▼
caddy（新增 service，自动证书）
 │ 反代
 ▼
web-admin（新增 service，内网监听，不发布到宿主机）
 │ 共享 bind mount ./state（读写：gcloud 凭据、代理池、账号-代理映射、force-flag、日志）
 ▼
watchdog（既有 service，改动：读账号代理映射、写日志文件、轮询 force-flag）
 │ 共享同一 ./state
 ▼
Cloud Shell VM（既有：base xray+cloudflared + kui 住宅层，不改）
```

`web-admin` 和 `watchdog` 都基于 `google/cloud-sdk:slim`（跟现有 `Dockerfile` 一致），
各自都能直接调 `gcloud`。`web-admin` **不挂 docker.sock**——它对 `watchdog` 的唯一控制
手段是往 `./state` 里写一个标记文件，不直接操作容器，公网暴露的服务不给宿主机/容器
控制权。

## 组件

### 1. web-admin 后端

Python 标准库 `http.server`（`ThreadingHTTPServer` + `BaseHTTPRequestHandler`），
不引入 FastAPI/Flask 等三方框架——跟 `subserver.py` 和 `kui-local-multi-exit` 的
`vps/local_api.py` 风格一致（该项目全程零三方 Python 依赖，只用系统包管理装的
二进制）。文件布局：

```
web-admin/
  app.py            # 路由、鉴权、gcloud 子进程管理、账号/代理 CRUD
  Dockerfile         # FROM google/cloud-sdk:slim
  static/
    app.js
    style.css
    qrcode.min.js    # vendored（本地文件，不走CDN）
  index.html          # 单文件模板，跟 kui 的 index.html 同款风格
```

### 2. 登录拿凭据（网页化 `gcloud auth login --no-browser`）

不用 WebSocket/PTY，用轮询——`gcloud auth login --no-browser` 本身不需要真终端，
纯管道够用，跟这个项目"不引入额外协议/依赖"的风格一致：

- `POST /api/accounts` `{name, proxy_id}`
  - 校验 `name` 匹配 `^[a-zA-Z0-9_-]{1,50}$`（不做 shell 拼接，`subprocess.Popen` 传
    argv 数组，杜绝注入）
  - `gcloud config configurations create <name> --quiet`（已存在则跳过）
  - 若 `proxy_id` 非空，从代理池取出对应 URL，子进程 env 里设
    `https_proxy`/`http_proxy` 为该代理；否则不设（直连）
  - 后台起 `gcloud auth login --no-browser --quiet` 子进程（`CLOUDSDK_ACTIVE_CONFIG_NAME=<name>`），
    stdout+stderr 合并写入该账号的内存 ring buffer（同时落盘 `state/gcloud-auth-<name>.log`
    以便刷新页面/重连后能续上）
- `GET /api/accounts/{name}/output` —— 前端每 1.5s 轮询，返回 buffer 全文（含授权
  URL 那一行）
- `POST /api/accounts/{name}/input` `{text}` —— 写入子进程 stdin（`text + "\n"`），
  即粘贴回来的验证码
- 子进程退出后：跑一次 `gcloud auth print-access-token`（同样套用户所选代理）校验，
  成功则账号状态记 `ok`，否则记 `auth-failed` 并保留最后输出供排查
- `DELETE /api/accounts/{name}` —— 二次确认（前端弹窗），`gcloud config
  configurations delete <name> --quiet`，同时删掉 `state/proxy-<name>` 映射文件和
  `state/exhausted-<name>`（如有）

### 3. 代理池

存 `state/proxies.json`（纯文件，跟项目现有 `state/exhausted-<name>`、
`state/current-account` 一样的"平文件"风格，不引入数据库）：

```json
[{"id": "p1", "label": "香港住宅A", "url": "socks5://user:pass@1.2.3.4:1080"}]
```

- `GET/POST/DELETE /api/proxies` —— 增删查，`url` 支持 `http(s)://` 和 `socks5://`
  两种 scheme（gcloud/requests 底层用的是标准 `https_proxy` 环境变量，socks5 需要
  `gcloud` 所在环境装了 PySocks 支持——`google/cloud-sdk:slim` 镜像自带的 Python
  环境需要确认是否已含 `PySocks`；若没有，在 web-admin 和 watchdog 的 Dockerfile
  里各加一行 `pip install pysocks`，这是本设计唯一新增的三方依赖，仅用于 socks5
  代理转发，无法用标准库替代）

### 4. 账号 ⇄ 代理绑定，一直延续到日常轮换

不只是登录那一下用代理，`watchdog.sh` 平时的 `account_ok()`（探活）和
`rebuild_on()`（触发 Cloud Shell 重建）、`tickle()`（keepalive）三处调用 gcloud 的
地方，都要按账号读同一份绑定：

```bash
# watchdog.sh 新增的小函数
account_proxy_env() { # $1=config name; 打印可直接 eval 的 export 语句，无绑定则空
  local f="$STATE_DIR/proxy-$1"
  [ -f "$f" ] && { local p; p=$(cat "$f"); [ -n "$p" ] && printf 'https_proxy=%q http_proxy=%q' "$p" "$p"; }
}
```

三处调用点改成：

```bash
env $(account_proxy_env "$cfg") gcloud ...
```

`state/proxy-<name>` 由 web-admin 在创建/编辑账号时写入（内容就是代理 URL，空文件
或不存在 = 不设代理，走容器全局 `PROBE_PROXY`，跟现在行为一致，向后兼容）。

### 5. 仪表盘（复用现有 state，不是新数据源）

- 当前 proxy 链接 + 二维码（`state/proxy-link.txt`，vendored JS 库客户端渲染）
- 账号列表：名字、Google 邮箱（`gcloud auth list` 拿）、状态(ok/配额冷却到几号/
  auth失效)、绑定代理、是否当前激活(`state/current-account`)、删除按钮
- 日志尾：`watchdog.sh` 的 `log()` 额外 `tee -a "$STATE_DIR/watchdog.log"`
  （新增一行），`GET /api/logs?tail=200` 读文件尾部，前端每 3s 轮询
- Force failover 按钮：`POST /api/force-failover` 写
  `state/force-failover-requested` 空文件；`watchdog.sh` 主循环每次 `sleep` 前后
  检查该文件是否存在，存在则当次立即当作探活失败处理并删除标记（改动在
  `check_once`/循环体，几行）
- `.env` 编辑：读写 compose 用的 `.env`，改完提示"需手动 `docker compose up -d`
  重启"，不自动重启（不给 web-admin 任何容器控制权）

### 6. 鉴权与公网安全

- 单管理员账号，密码走 `.env` 存 bcrypt hash（`ADMIN_PASSWORD_HASH`，配一个一次性
  生成脚本 `web-admin/hash-password.py`）
- 登录成功签发 httponly + secure + samesite=lax 的会话 cookie（HMAC 签名，
  `SESSION_SECRET` 来自 `.env`，标准库 `hmac`+`hashlib` 实现，不引入 JWT 库），
  12 小时过期
- 登录失败次数限制：内存级按源 IP 计数，超过阈值短暂锁定
- 除 `/login` 和静态资源外全部路由要求有效会话
- 状态变更类 POST/DELETE 要求同源 + 自定义 header（简单 CSRF 防护，不用 token 机制）
- `caddy` 反代自动 HTTPS，`.env` 新增 `ADMIN_DOMAIN`；`web-admin` 只在 compose 内网
  监听，不发布端口到宿主机，只有 `caddy` 发布 443/80

### 7. docker-compose.yml 改动

新增两个 service：`web-admin`（build `./web-admin`，无宿主机端口，`./state:/state`
同款 bind mount，读取 `.env` 里 `ADMIN_PASSWORD_HASH`/`SESSION_SECRET`/`ADMIN_DOMAIN`）、
`caddy`（官方镜像，发布 `443:443`/`80:80`，`Caddyfile` 模板化域名，卷放证书数据）。
`watchdog` service 不变（image/build 不动，只是 `watchdog.sh` 脚本内容改）。

## 数据流小结

```
用户浏览器 → caddy(TLS) → web-admin
                              ├─ 账号CRUD/登录轮询 → gcloud子进程(按代理) → state/gcloud
                              ├─ 代理池CRUD → state/proxies.json
                              ├─ force-failover → state/force-failover-requested (标记文件)
                              └─ 仪表盘只读 → state/proxy-link.txt, state/watchdog.log,
                                              state/current-account, state/exhausted-*

watchdog循环 → 读 state/proxy-<name> 决定gcloud走哪个代理
             → 读 state/force-failover-requested 决定是否立即轮转
             → 写 state/watchdog.log（新增）+ 原有 state/status 等
```

## 错误处理

- 代理不可用（连不上/超时）：`gcloud auth login`/`account_ok` 走该代理直接失败，
  账号状态标为 `auth-failed`，前端展示失败原因（子进程最后几行输出），不静默重试、
  不自动换代理——代理池是用户自己维护的，换不换由用户决定
- `state/proxy-<name>` 文件被删/损坏：`account_proxy_env` 视为无绑定，退回容器全局
  `PROBE_PROXY`（不 crash watchdog 循环）
- web-admin 崩溃/重启：session cookie 已签发的仍有效（无服务端 session 存储，纯
  HMAC 校验），账号登录的子进程状态因为落盘了 `state/gcloud-auth-<name>.log`，
  重连页面能看到最后进度，但**子进程本身**如果 web-admin 容器重启会被杀掉——正在
  等你粘贴验证码的登录会中断，需要重新点"添加账号"重来（这是可接受的，OAuth 设备码
  流程本来就是一次性的，不做跨重启断点续传）

## 测试计划

项目目前没有自动化测试框架（`cloudshell-proxy-autostart` 侧只有 shell 脚本，没有
CI）。新增部分照此风格，验证手段：

- `bash -n watchdog.sh proxy-start.sh` —— 语法检查
- `python3 -m py_compile web-admin/app.py subserver.py` —— 语法检查
- 手工验证清单（写进 README，部署后过一遍）：
  1. 起 compose，`curl -k https://<domain>/healthz` 通
  2. 未登录访问 `/` 应 302 到 `/login`
  3. 登录成功后添加一个测试账号（绑代理），走完整授权流程，`gcloud auth
     print-access-token` 校验通过
  4. 代理池增删一条记录，刷新页面数据还在（落盘验证）
  5. 点 force-failover，观察 `docker logs watchdog` 里几秒内触发一次轮转
  6. 删除测试账号，`gcloud config configurations list` 里确认已消失

## 范围外

- kui 住宅层内部逻辑（24 槽 OpenVPN、`subserver.py` 订阅生成）—— 原样保留，不改
- 从 `cloudshell-proxy-autostart` 主仓库同步后续更新——两边现在是独立仓库，不建立
  自动同步机制，需要的话手动 cherry-pick
