# AI 通用注册机（ai-register-machine）

在常见「多模型注册脚本合集」之上，做成 **可维护、可验收、可 UI 操作** 的 monorepo：分层编排 + 多 provider 生产路径 + **Web control plane**（配置 / 导入 / 启停与状态）。

对标开源 [ThinkerWen/ai-register](https://github.com/ThinkerWen/ai-register) 一类项目：**方向相同，实现更偏生产可用性**——本轮结果归因、fail-fast、契约脱敏、CPA/OIDC 门禁诚实、统一 hub、项目内 Web 控制面，而不是只堆脚本。

| Provider | 入口 | 栈 | 产物方向 |
|----------|------|----|----------|
| **Grok / xAI** | `./register.sh grok` · **Web UI** | Python + DrissionPage + turnstilePatch | SSO 账本 → CPA OIDC mint → chat 探针（可选远端 live） |
| **Xiaomi MiMo** | `./register.sh mimo` | Node + Playwright | `sk-` API Key（TTS 等）；CPA 侧按 **OpenAI-compat** 导入，非 xai auth |
| **分层编排** | `./register.sh core` | `register_core/` | 邮箱 / 注册 / 验证 / 落盘 编排（不替代产品内核） |

> **Education / personal automation only.** Not a free-quota farm, not for mass account creation, spam, resale, or bypassing paid limits. Grok path **detects** free Build chat entitlement — it does **not** grant it. See [DISCLAIMER.md](DISCLAIMER.md).

> **安全提示：** 不要提交 `config.json`、`mail_credentials.txt`、`accounts_*.txt`、`cpa_auths/*.json`、`backups/`、`.env`、`logs/`、`screenshots/`、`providers/*/output/`。仓库已 gitignore；只用 `*.example*` 模板。  
> **合规提示：** 可能违反第三方服务条款。见 [DISCLAIMER.md](DISCLAIMER.md) / [SECURITY.md](SECURITY.md)。**MIT，无担保。**  
> **命名：** [`dengyie/ai-register-machine`](https://github.com/dengyie/ai-register-machine)（历经 `grok-register` → `register-machine`）。本地/pxed 目录可仍叫旧名，以本仓代码为准。

| 文档 | 说明 |
|------|------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | **Monorepo 骨架与分层边界**（canonical） |
| [docs/ADDING_PROVIDER.md](docs/ADDING_PROVIDER.md) | 如何新增 provider |
| [docs/LAYOUT.md](docs/LAYOUT.md) | 目录速查 |
| [docs/DEVELOPED.md](docs/DEVELOPED.md) | 已落地功能一览（历史设计见 `docs/archive/`） |
| [DISCLAIMER.md](DISCLAIMER.md) | 免责声明 |
| [SECURITY.md](SECURITY.md) | 密钥与泄露处理 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发 / 测试 / PR |
| [CHANGELOG.md](CHANGELOG.md) | 版本（当前 **v1.3.0**） |
| [LICENSE](LICENSE) | MIT |
| [register_core/README.md](register_core/README.md) | 分层通用框架 |
| [providers/README.md](providers/README.md) | 产品包一览 |
| [providers/mimo/README.md](providers/mimo/README.md) | MiMo TTS Key 注册 |
| [apps/README.md](apps/README.md) | CLI / Web control plane 入口图 |
| `Makefile` | `make test` / `syntax` / `doctor` / `example` |
| `config.simple.example.json` | **Grok 简易配置**（推荐新人） |
| `config.example.json` | Grok 全量字段 + 注释 |
| `scripts/setup_simple.sh` | 一键配置 + 环境 doctor |
| `scripts/doctor_secrets.sh` | 本地密钥/跟踪卫生检查（不打印内容） |

## 如何使用（TL;DR）

```bash
# 1) 安装依赖并生成简易配置 + 环境检查
git clone https://github.com/dengyie/ai-register-machine.git
cd ai-register-machine
bash scripts/setup_simple.sh

# 2) 编辑 config.json（Grok 路径）
#    - proxy: 本地代理，如 http://127.0.0.1:7890
#    - 默认 email_provider=duckmail → 填 duckmail_api_key
#    - 或改 hotmail → 填 mail_credentials.txt（邮箱----密码----ClientID----refresh_token）
#    - 禁止 Hotmail plus-alias 农场；一号一箱

# 3) 统一入口
./register.sh help
./register.sh grok 1 1                 # xAI / Grok CLI
./register.sh mimo                     # Xiaomi MiMo API Key（需 Node runtime）
./register.sh core list
./register.sh core run -p mimo -n 1

# 3b) Web control plane（配置 / 导入 / 启停 batch + 状态日志）
export CONTROL_API_SESSION_SECRET="$(openssl rand -hex 32)"
export CONTROL_API_TOKEN="$(openssl rand -hex 32)"   # 可选，给 curl/脚本
uv run python scripts/control_api_user.py set admin  # 创建操作员账密
./scripts/run_control_api.sh
# 浏览器打开 http://127.0.0.1:8787 → 登录

# 4) 看结果
ls accounts_cli.txt cpa_auths/                 # Grok
ls providers/mimo/output/ 2>/dev/null || true  # MiMo（或 pxed /personal/mimo-register/output）

# 5) 可选：本地密钥卫生（不打印文件内容）
bash scripts/doctor_secrets.sh
```

### 相对 ai-register 类项目：我们多做了什么

| 维度 | 常见 ai-register 脚本合集 | **ai-register-machine** |
|------|---------------------------|-------------------------|
| 架构 | 多脚本并列，边界模糊 | `register_core` 分层：email / providers / verify / sink / pipeline |
| 入口 | 各产品各敲命令 | `./register.sh` 统一 hub + CLI + **Web control plane** |
| 成功判定 | 易用历史日志/exit 0 误判 | **本轮归因**（ledger 增量 / `RESULT_JSON` / 文件 offset） |
| 失败策略 | 容易空转重试 | **fail-fast**；超时杀进程组 |
| 产物契约 | 常混 SSO / token / sk | Grok→xai OIDC+chat 门禁；MiMo→OpenAI-compat `sk-`，文档写清 |
| 安全 | 密钥易进仓 | doctor、gitignore、public 脱敏、sink 0600 |
| UI | 多为纯 CLI | **项目内 Web 控制面**：账密登录 + Config / Import / Runs，默认 `127.0.0.1` |

### Web control plane（配置 / 导入 / 启停）

```bash
export CONTROL_API_SESSION_SECRET="$(openssl rand -hex 32)"
uv run python scripts/control_api_user.py set admin
./scripts/run_control_api.sh
# http://127.0.0.1:8787 → 用户名/密码登录
```

FastAPI + console10 SPA（`apps/control_api` + `apps/web` Vite/Preact；`npm run build` → `apps/web/dist`）：

- **登录**：操作员账密（scrypt + HttpOnly session）；脚本可用 Bearer token
- **Overview**：product_ok 计数、当前 batch 状态
- **Config**：编辑 `config.json`（备份后写；密钥脱敏）
- **Import**：节点/代理、邮箱凭证、auth dump、配置包
- **Runs**：`launch_batch_supervisor` / `./register.sh` 启停 + 日志 tail

与 CLI 共用同一套注册与 CPA 链路；桌面 TTK 已移除。详见 [apps/README.md](apps/README.md)。

### Monorepo 骨架

对标 [ThinkerWen/ai-register](https://github.com/ThinkerWen/ai-register) 的 `register/<product>` + 共享 util，以及 LiteLLM 式 **ARCHITECTURE / Makefile / registry**，本仓固定为：

```text
apps/                 入口图（CLI / control_api / web）
register_core/        分层库：email → providers → verify → sink → pipeline
providers/            产品包：mimo（生产）、grok（说明）、_template（复制起步）
docs/ examples/ tests/ scripts/
register.sh Makefile ARCHITECTURE.md
```

```text
./register.sh
     │
     ├─ grok  → register_cli / grok_register_ttk(engine) / cpa_xai   （生产权威，暂根目录）
     ├─ mimo  → providers/mimo/run-register.sh               （生产权威）
     └─ core  → python -m register_core                      （编排 + 本轮归因）

./scripts/run_control_api.sh → apps/control_api + apps/web   （项目内 Web 控制面）
```

| 层 | 包路径 | 说明 |
|----|--------|------|
| 邮箱来源 | `register_core/email` | in-process allocate+OTP；grok/mimo 黑盒时仅 `provider` |
| 产品注册 | `register_core/providers` | 适配器：本轮 ledger/RESULT_JSON/文件增量归因 |
| 验证 | `register_core/verify` | key shape / 账本存在；live chat 仍走 cpa_xai |
| 落盘 | `register_core/sink` | JSONL 0600；public 脱敏 |
| 编排 | `register_core/pipeline` | count + fail-fast；verify 失败必失败 |

详见 [ARCHITECTURE.md](ARCHITECTURE.md) · [docs/ADDING_PROVIDER.md](docs/ADDING_PROVIDER.md) · [register_core/README.md](register_core/README.md)。

部署布局示例（pxed）：代码仓 `/personal/grok-register`（或 `ai-register-machine`）+ Node runtime `/personal/mimo-register` + **Clash/mihomo mixed-port `:7897`（Grok 生产权威出口）**。

**Grok 生产出口（pxed）≠ monorepo `nodes.json`：** 批前 leaf 健康探测走 `bash run-register.sh` → `preflight-clash-nodes.sh` → `scripts/probe_clash_nodes.py`（mihomo delay API，重写 `🎯Grok注册` 等组，钉 `GROK_NODE`，重启 mihomo）。`nodes.json` / `list|auto` / `PROXY_LIST` 是另一套 catalog 预检后端，见 [ARCHITECTURE.md · Dual egress](ARCHITECTURE.md#dual-egress-backends-do-not-confuse)。调试可 `SKIP_CLASH_PREFLIGHT=1`。

**Python：** 需要 **3.13**（`pyproject.toml` → `requires-python`）。请用 [uv](https://docs.astral.sh/uv/)：`uv python install 3.13 && uv sync`。系统自带 3.11/3.12 不够。

| 你想… | 看哪里 |
|--------|--------|
| 从零跑通本地链路 | [最短路径上手](#最短路径上手对外简易模式) |
| **UI 配置 / 导入 / 启停** | [Web control plane](#web-control-plane配置--导入--启停) · `./scripts/run_control_api.sh` |
| 成功/失败怎么判断 | [什么叫「成功」](#什么叫成功) |
| Hotmail 四段凭证 | [邮箱：Hotmail / Outlook](#邮箱hotmail--outlook) |
| 全量配置项 | [配置说明](#配置说明) · `config.example.json` |
| CLI 参数 | [CLI 参数速查](#cli-参数速查register_clipy) |
| 推远端 CPA live | [生产模式](#生产模式可选--远端-live-注入) |
| 卡住了 | [常见卡点](#常见卡点) · [故障排查](#故障排查) |
| MiMo TTS Key | [providers/mimo/README.md](providers/mimo/README.md) · `./register.sh mimo` |
| 分层通用框架 | [register_core/README.md](register_core/README.md) · `./register.sh core` |
| Monorepo 骨架 / 新 provider | [ARCHITECTURE.md](ARCHITECTURE.md) · [docs/ADDING_PROVIDER.md](docs/ADDING_PROVIDER.md) · `make test-unit` |

---

## 功能特性

| 能力 | 说明 |
|------|------|
| 批量注册 | CLI / GUI，可并发；推荐有头 Chromium（Turnstile 更稳） |
| Hotmail / Outlook 收码 | 四段凭证 + plus alias；优先 Office REST，失败回退 XOAUTH2 IMAP |
| 其他邮箱通道 | CloudMail / Cloudflare Worker / DuckMail 等（见配置） |
| CPA OIDC 铸造 | SSO cookie → 纯 HTTP Device Flow（`curl_cffi`）；失败再回退有头浏览器 consent |
| 免费 Grok 4.5 门禁 | 产出 `type=xai` 文件，并默认探针 `/v1/responses`；**403 不算可用** |
| 远端注入（可选） | mint 成功后 SSH 写入远端 CPA `auth-dir` |
| 本地备份 | 成功后刷新 `backups/latest`，可打时间戳快照 |
| 存量回填 | 已有账本 SSO 时走与注册相同的 `cpa_export` 管线（含可选远端注入） |
| SSO 归一化 | 账本/cookie 前导多余 `-`（`-eyJ…`）在 mint 内核与写账本时自动剥离 |

---

## 硬约束：SSO ≠ OIDC

| 产物 | 用途 | 路径 |
|------|------|------|
| **SSO** | grok.com / grok2api Web 池 | 账本第三段 |
| **OIDC（CPA xAI）** | 免费 **Grok 4.5**（Grok Build / cli-chat-proxy） | `cpa_auths/xai-<email>.json` |

免费 Grok 4.5 **不能**用账本里的 sso JWT 直接打 API；必须再走 `accounts.x.ai` device-auth 铸 OIDC。本仓库用 **SSO cookie 自动完成** 这一步（优先协议路径，无需再弹浏览器）。

OIDC 相关代码自包含在 `cpa_xai/`：

| 路径 | 说明 |
|------|------|
| `cpa_xai/protocol_mint.py` | SSO → 纯 HTTP Device Flow（verify / approve / token）；extract/set 时 normalize |
| `cpa_xai/mint.py` | 协议优先，失败回退浏览器；入口统一 `normalize_sso_cookie` |
| `cpa_xai/accounts.py` | 账本解析、SSO normalize、`format_account_line`、plus-alias skip 键 |
| `cpa_xai/browser_confirm.py` | 有头 Chromium 完成 consent |
| `cpa_export.py` | 注册 / backfill 共用 hook（写文件 / 可选远端注入 / 本地备份） |
| `scripts/backfill_cpa_xai_from_accounts.py` | 存量补 CPA（默认走 `cpa_export`，与注册同链路） |
| `scripts/export_cpa_xai_from_grok_auth.py` | 从 `~/.grok/auth.json` 导出 |
| `scripts/backup_registered_accounts.py` | 手动触发本地备份 |

---

## 整链示意

```
[邮箱 Hotmail/Outlook 或 CloudMail 等]
       ↓  注册 accounts.x.ai / grok
 accounts_cli.txt                     email----password----sso
       ↓
 grok2api 池 (可选)                   SSO → Web 模型
       ↓
 OIDC mint（协议优先 → 浏览器回退）
       ↓
 cpa_auths/xai-<email>.json           【主导出】
       ↓ 可选
 远端 CPA auth-dir / 本地 hotload     cpa_remote_inject / cpa_copy_to_hotload
       ↓
 CLIProxyAPI 或直连 cli-chat-proxy    model=grok-4.5
       ↓
 backups/latest                       本地凭证快照（gitignore）
```

协议 mint 流程：

```
注册成功拿到 sso cookie
        ↓
【优先】protocol_mint：curl_cffi + sso
   device/code → verify → approve → token 轮询
        ↓ 成功
  cpa_auths/xai-<email>.json   mint_method=protocol
        ↓ 失败
【回退】browser_confirm：有头 Chromium + turnstilePatch
   同一套 device-auth，页面点「允许」
        ↓
  cpa_auths/xai-<email>.json   mint_method=browser
```

实测：协议路径约数秒级（含 probe）；浏览器回退约 40–60s/号。密集 mint 可能遇到 `rate_limited` / `slow_down`，可事后用 backfill + `--sleep` 补齐。

---

## 环境要求

| 依赖 | 说明 |
|------|------|
| macOS / Linux | 注册建议有桌面会话（有头浏览器过 Turnstile） |
| Python **3.13** | 见 `pyproject.toml` / `uv.lock` |
| [uv](https://docs.astral.sh/uv/) | 包管理（推荐） |
| Chromium / Chrome | 注册必需 |
| 本地代理 | 访问 xAI 通常需要，如 `http://127.0.0.1:7890` |
| 收码邮箱 | Hotmail 四段凭证，或 DuckMail / CloudMail 等 |

---

## 最短路径上手（对外简易模式）

目标：**本地**跑通「注册 1 个号 → 写出 CPA → chat 探针」，**不**连远端 tebi。  
（装齐 uv / Python 3.13 / Chrome / 代理 / 邮箱后，大约 **15 分钟**量级，不是魔法 5 分钟。）

### 1. Clone + bootstrap（含 doctor）

```bash
git clone https://github.com/dengyie/ai-register-machine.git
cd ai-register-machine
bash scripts/setup_simple.sh
```

会：生成 `config.json`（来自 `config.simple.example.json`）、可选 `mail_credentials.txt`、`uv sync`，并检查 Python / Chrome / 代理端口 / 邮箱占位。

### 2. 填最小配置

默认 **`email_provider=duckmail`**（比 Hotmail 四段凭证省事）：

```json
{
  "proxy": "http://127.0.0.1:7890",
  "email_provider": "duckmail",
  "duckmail_api_key": "你的_DuckMail_Key"
}
```

若用 Hotmail，再改：

```json
{
  "email_provider": "hotmail",
  "hotmail_accounts_file": "mail_credentials.txt"
}
```

```text
# mail_credentials.txt 每行（勿提交）
邮箱----密码----ClientID----Microsoft_refresh_token
```

改完可再跑一次 `bash scripts/setup_simple.sh` 看 doctor 是否还报 `[warn]`。

### 3. 注册 1 个号

```bash
uv run python -u register_cli.py --extra 1 --threads 1 --no-headless --fast
ls accounts_cli.txt cpa_auths/
```

Web UI：`./scripts/run_control_api.sh` → http://127.0.0.1:8787

### 什么叫「成功」

| 结果 | 含义 | 你该做什么 |
|------|------|------------|
| `CPA成功` 且统计里有 **chat可用** | models 含 grok-4.5 **且** `/v1/responses` 通过 | 可用 free Build / 可注入 CPA |
| `chat无权限` / `entitlement_denied` | models 可能 200，但 chat **403** | **不要 remint**；换获权渠道或付费 API |
| 只有 `accounts_cli.txt` 有 SSO | 注册成功，OIDC/chat 未过 | SSO 可给 grok2api；**≠** free Build |

默认（产号主链路 · disk-first）：

- `cpa_export_enabled=true` — 注册后 mint OIDC，写出完整 `xai-*.json`（含 refresh）  
- `cpa_probe_chat=false` — **不**在主链路打 chat；成功 = 落盘 complete auth  
- `cpa_remote_inject=false` — **不**自动推远端 CPA；注入是独立后续链路  
- supervisor 强制：`CPA_EXPORT_ENABLED=true` / `CPA_PROBE_CHAT=false` / `CPA_REMOTE_INJECT=false`

> **边界：** 注册机主链路到「账号信息完整落盘」为止。CPA healthy 筛选与 tebi 注入单独做，勿绑进注册成功判定，以免 chat 403 / SSH 拖死产号。

> **重要：** 本工具不能「生成」xAI free Build 权限。权限由 xAI 服务端授予；chat probe / 导入链路只负责检测。

### 常见卡点

| 现象 | 处理 |
|------|------|
| doctor：proxy port closed | 启动本地代理，改 `config.json` → `proxy` |
| doctor：duckmail_api_key empty | 填 DuckMail key，或改用 hotmail + 真凭证 |
| doctor：mail placeholder | 换掉 `your@hotmail.com----...----refresh-token` 占位行 |
| Turnstile / 注册页卡住 | `--no-headless` 有头浏览器；换 IP/代理 |
| `entitlement_denied` / chat 403 | **号无 free Build 权**，勿 remint；见上文权限说明 |
| Python 版本错误 | 需要 **3.13** + [uv](https://docs.astral.sh/uv/) |

### CPA 注入（独立链路 · 非注册主路径）

注册机默认 **不** 注入远端。需要进 tebi live 时，在**另一条链路**对已落盘 auth 做 healthy-only 导入（先 chat probe，仅 scp healthy）：

| 键 | 建议（仅独立注入工具/手动步骤） |
|----|------|
| 候选源 | pxed `cpa_auths/xai-*.json`（complete + refresh） |
| 门禁 | 仅 chat healthy；403 / reauth / incomplete **不进** CPA |
| `cpa_remote_inject` | 注册主链路保持 `false`；注入用独立脚本/手动 scp |
| `cpa_remote_ssh_host` 等 | 仅注入工具侧配置（如 `tebi-tunnel`） |

```bash
# 产号（主链路）— supervisor 已 disk-first
logs/launch_batch_supervisor.sh ordinary N

# 注入（独立，用户触发）— 先 probe healthy，再只导入 healthy 列表
# 勿在 register_cli 成功路径里一键绑死
```

开发自检：

```bash
uv sync --extra dev
uv run python -m pytest -q
```

---

## 邮箱：Hotmail / Outlook

```json
{
  "email_provider": "hotmail",
  "hotmail_accounts_file": "mail_credentials.txt",
  "hotmail_max_aliases_per_account": 200,
  "hotmail_mail_fetch_modes": "rest,imap"
}
```

凭证文件（勿提交）：

```bash
cp mail_credentials.example.txt mail_credentials.txt
```

**每行格式（四段，`----` 分隔）：**

```text
邮箱----密码----ClientID----Token
```

| 段 | 含义 |
|----|------|
| 邮箱 | Hotmail / Outlook 主邮箱 |
| 密码 | 邮箱登录密码（注册机侧保留；收码走 OAuth） |
| ClientID | 微软应用（Azure AD）Client ID |
| Token | Microsoft OAuth2 **refresh_token**（REST / XOAUTH2 IMAP） |

示例：

```text
your@hotmail.com----mailPassword----xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx----0.AXcA...refresh_token...
```

运行时行为：

- 默认先用原邮箱，后续用随机 plus alias（如 `name+k8s2p9qa@domain`）
- 收码优先 Outlook Office REST，失败再 IMAP（`outlook.office365.com` → `imap-mail.outlook.com`）
- refresh_token 若轮换会**自动回写** `mail_credentials.txt`
- 成功 / 失败 / 占用中的 alias 参与去重与 `hotmail_max_aliases_per_account` 计数

其他邮箱提供方见 `config.example.json` 中 `cloudmail_*` / `cloudflare_*` / `duckmail_*` 注释。

---

## 配置说明

1. 复制模板并编辑（模板内 `"//…"` 键是注释，加载时忽略）：

```bash
cp config.example.json config.json
```

2. **字段详解以 `config.example.json` 内注释键为准。**

### 代理优先级

| 字段 | 作用 |
|------|------|
| `proxy` | **注册** Chromium + 邮箱等 HTTP |
| `cpa_proxy` | **OIDC mint**（协议 HTTP + 回退浏览器 + probe） |

```
cpa_proxy  >  proxy  >  环境变量 https_proxy / http_proxy
```

配置优先于 shell 环境变量，避免「config 写了代理却被环境变量盖掉」。

### 代理/出口 IP 轮换（注册级，不动整机出口）

三种模式（`proxy_rotate_mode` / `PROXY_ROTATE_MODE` / `--proxy-rotate`）：

| 模式 | 行为 | 整机影响 |
|------|------|----------|
| `off` | 不轮换 | 无 |
| `list` | 轮换 `proxy_list` 里的代理 URL，仅注册 Chromium / 注册 HTTP 使用 | 无（其他 app 不走该代理） |
| `clash` | 在 Clash 专用策略组 `GROK-REG` 上轮换节点；`DOMAIN-SUFFIX,x.ai,GROK-REG` 等规则只让 xAI/Grok 流量走该组 | 仅命中域名的流量；主策略组（如「宝可梦」）永不修改 |

- **list 模式**：`proxy_list` 支持 `http://a:1,http://b:2` / 换行 / `["a","b"]` / `.txt` 文件；带 `user:pass` 自动走 `LocalAuthProxyBridge`。
- **clash 模式**（推荐本机）：首次运行自动在 `clash-verge.yaml` 注入 `GROK-REG` 组 + xAI 域名规则并 `force reload`；进程退出自动恢复专用组原节点。**绝不修改主策略组**。
- 每多少次注册换一次：`proxy_rotate_every` / `PROXY_ROTATE_EVERY` / `--proxy-rotate-every`（默认 1 = 每号换）。

```bash
# Clash 域名规则轮换（每号换 IP，仅 grok.com/x.ai 走新节点，主组不动）
PROXY_ROTATE_MODE=clash python3 register_cli.py --extra 5

# 或纯 CLI
python3 register_cli.py --proxy-rotate clash --proxy-rotate-every 2 --extra 10

# 自定义命中域名
python3 register_cli.py --proxy-rotate clash --clash-domains x.ai,grok.com,accounts.x.ai

# 显式代理池轮换（list 模式，仅注册浏览器）
PROXY_ROTATE_MODE=list PROXY_LIST="http://u:p@1.2.3.4:8080,http://u:p@5.6.7.8:8080" \
  python3 register_cli.py --extra 5
```

### CPA 关键项

| 字段 | 默认 / 建议 | 含义 |
|------|-------------|------|
| `cpa_export_enabled` | `true` | 注册成功后是否 mint OIDC |
| `cpa_prefer_protocol` | `true` | 有 SSO 时先走纯 HTTP 协议 mint |
| `cpa_protocol_only` | `false` | `true`=协议失败也不回退浏览器（调试用） |
| `cpa_protocol_poll_timeout_sec` | `90` | 协议路径 token 轮询超时 |
| `cpa_auth_dir` | `./cpa_auths` | 主导出目录 |
| `cpa_base_url` | `https://cli-chat-proxy.grok.com/v1` | 免费 Build **必须**此上游 |
| `cpa_headless` | **`false`** | 回退浏览器建议有头 |
| `cpa_force_standalone` | **`true`** | 回退时独立 Chromium |
| `cpa_mint_cookie_inject` | `true` | 回退时注入注册 cookie |
| `cpa_mint_required` | `false` | mint 失败是否整号失败 |
| `cpa_probe_after_write` | `true` | 写文件后探测 `/models` 是否含 grok-4.5 |
| `cpa_copy_to_hotload` | `false` | 是否复制到本机 CPA 热加载目录 |
| `cpa_hotload_dir` | 空 | 本机 CPA `auth-dir` |
| `cpa_auth_priority` | `1000` | 写入 `xai-*.json` 的 CPA 路由权重（mint/写盘/注入统一） |
| `cpa_remote_inject` | `false` | 单号 mint 后是否立即 SSH 注入（生产 bulk **务必 false**）。bulk supervisor 另读此值作「批末统一导入」意图：产号全程仍强制 inject off，**整批 target 达成后**再跑 `import_cpa_auth_dir.py`（batch5 + healthy-only）。显式 env：`CPA_BATCH_END_INJECT=true` |
| `cpa_remote_live_dir` | `/root/.cli-proxy-api` | 一键成功门闩目录（live 池） |
| `cpa_remote_live_required` | `true` | live 注入失败则整次 export 失败（inventory-only 不算一键成功） |
| `cpa_remote_inject_required` | `false` | 所有远端目录都必须成功；比 live 门闩更严 |
| `cpa_remote_ssh_host` | 如 `tebi-tunnel` | ssh 主机别名（写在 `~/.ssh/config`） |
| `cpa_remote_auth_dirs` | live+inventory | 多目录：`/root/.cli-proxy-api,/personal/cpa/auths`（显式设置优先生效） |
| `cpa_remote_auth_dir` | 兼容单目录 | 仅当 **未** 开 inject 且未设 `cpa_remote_auth_dirs` 时使用 |
| `cpa_remote_credentials_file` | `~/.ssh/bohrium_credentials` | 可选密码文件；也可用环境变量 `CPA_REMOTE_SSHPASS` / `SSHPASS` |

**产号主链路：** 注册成功 → mint → 本地 `cpa_auths` complete auth（含 refresh）即结束。  
**CPA 注入（独立 / 批末）：** chat probe → **仅 healthy** 导入 live/inventory。勿在单号 mint 成功路径绑立即 inject。  
- 手动：`python -u scripts/import_cpa_auth_dir.py --src cpa_auths --remote --batch-size 5 --batch-pause 3`  
- bulk 自动：`CPA_BATCH_END_INJECT=true`（或 config `cpa_remote_inject=true` 作意图）→ supervisor 在 **TARGET 达成后** 统一 import 一次；未达目标不导入。  
存量 remint 工具仍可用：

```bash
uv run python -u scripts/remint_expired_and_sync_authdir.py --limit 5
```

布尔配置请写 JSON 布尔或可解析字符串（`true`/`false`/`1`/`0`）；export 路径用 `_config_bool`，字符串 `"false"` **不会**被当成开启。

CLI 与 GUI 都会在注册成功后读这些配置。GUI 下 CPA 导出会串行，避免多窗口抢焦点。

### 账本与文件名

- 账本行：`email----password----sso`；CLI/GUI 写盘前都会 `normalize_sso_cookie`（去掉 JWT 前多余的 `-`）。
- CPA 文件名：`xai-<email>.json`，其中 `+` 等不安全字符会变成 `-`（如 `user+abc@x.com` → `xai-user-abc@x.com.json`）。
- skip-existing 对 **plus-alias 与 sanitize 后的 stem** 对称匹配，避免重复 mint。

### 落盘约定

| 路径 | 是否提交 | 说明 |
|------|----------|------|
| `mail_credentials.txt` | **否** | Hotmail 四段凭证 |
| `accounts_cli.txt` / `accounts_*.txt` | **否** | 主账本 `email----password----sso` |
| `cpa_auths/xai-*.json` | **否** | CPA OIDC 归档 |
| `backups/` | **否** | 本地快照（`latest` + 时间戳目录） |
| `config.json` / `.env` | **否** | 本地实配 |
| `config.example.json` 等 | 是 | 模板 |

---

## 常用命令

前置：代理写在 `config.json` 的 `proxy` / `cpa_proxy`；回退浏览器时需要桌面会话。

### A. 新注册 N 个号（SSO + OIDC）

```bash
# 再注册 1 个（推荐）
uv run python -u register_cli.py --extra 1 --threads 1 --no-headless --fast

# 再注册 20 个
uv run python -u register_cli.py --extra 20 --threads 1 --no-headless --fast

# 总数目标到 100（含已有；已达标则退出）
uv run python -u register_cli.py --count 100 --threads 1 --no-headless
```

成功时：

1. 追加账本 `email----password----sso`
2. 可选：推 grok2api
3. 若 `cpa_export_enabled`：协议 mint（失败则浏览器）→ `cpa_auths/xai-<email>.json`
4. 可选：远端注入 / 本机 hotload / `backups/latest`

### B. 存量号补 CPA（不重新注册）

账本需含 SSO（第三段）。**默认走 `cpa_export.export_cpa_xai_for_account`**，与注册成功后的链路一致：

- SSO normalize（mint 内核 + export）
- 读 `config.json` 的 `cpa_*`（含 `cpa_remote_inject`）
- 可选远端注入 / hotload / 本地 backup

有有效 SSO 时通常**无需**弹浏览器：

```bash
# 试跑 1 个缺失号（会按 config 决定是否远端注入）
uv run python -u scripts/backfill_cpa_xai_from_accounts.py \
  --accounts accounts_cli.txt \
  --limit 1 --probe --timeout 300

# 全量缺失号（建议加间隔，避免 rate limit）
uv run python -u scripts/backfill_cpa_xai_from_accounts.py \
  --limit 0 --probe --timeout 300 --sleep 12

# 只要本地 mint，不要这次 SSH 注入
uv run python -u scripts/backfill_cpa_xai_from_accounts.py \
  --limit 5 --no-remote --sleep 12

# 完全绕过 cpa_export（无 inject / backup hook；调试用）
uv run python -u scripts/backfill_cpa_xai_from_accounts.py \
  --limit 1 --local-only
```

| 参数 | 含义 |
|------|------|
| `--limit N` | 本次最多 N 个缺失号；`0`=全部 |
| `--email x@y` | 只处理指定邮箱 |
| `--out-dir` | 覆盖 `cpa_auth_dir` |
| `--cpa-dir` | 成功后复制到本机 CPA 热加载目录 |
| `--probe` / `--no-probe` | 检查是否列出 `grok-4.5` |
| `--sleep N` | 每个号之间休眠秒数 |
| `--headless` | 回退浏览器时无头（不推荐） |
| `--no-remote` | 本 run 强制 `cpa_remote_inject=false` |
| `--local-only` | 直接 `mint_and_export`，不经 `cpa_export` |
| `--config` | 默认 `./config.json` |

### C. 从 `~/.grok/auth.json` 导出

```bash
uv run python scripts/export_cpa_xai_from_grok_auth.py --out-dir ./cpa_auths
```

### D. 本地备份

注册成功会自动刷新 `backups/latest`。也可手动：

```bash
uv run python -u scripts/backup_registered_accounts.py
# 仅刷新 latest，不打时间戳目录
uv run python -u scripts/backup_registered_accounts.py --no-stamp
```

### E. 手动导入 CPA 热加载

```bash
cp -a ./cpa_auths/xai-USER@domain.json "$CPA_AUTH_DIR"/
chmod 600 "$CPA_AUTH_DIR"/xai-USER@domain.json
```

### F. 调用验证

**方式 1：经本机 CLIProxyAPI / CPA（:8317）**

```bash
KEY="<你的 CPA API KEY>"

curl -sS http://127.0.0.1:8317/v1/models -H "Authorization: Bearer $KEY" | head

curl -sS http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role":"user","content":"Reply with exactly OK"}],
    "stream": false
  }'
```

**方式 2：直连 `cli-chat-proxy`（用 `cpa_auths` 里的 access_token）**

```bash
# 需本机可访问 cli-chat-proxy（通常要代理）
TOKEN=$(python -c "import json;print(json.load(open('cpa_auths/xai-USER@domain.json'))['access_token'])")

curl -sS https://cli-chat-proxy.grok.com/v1/models \
  -H "Authorization: Bearer $TOKEN"

curl -sS https://cli-chat-proxy.grok.com/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role":"user","content":"Reply with exactly OK"}],
    "max_tokens": 32,
    "stream": false
  }'
```

---

## CLI 参数速查（`register_cli.py`）

| 参数 | 含义 |
|------|------|
| `--extra N` | **再新注册 N 个**（推荐） |
| `--count N` | 账号**总数目标**（含已有）；`0`=不限 |
| `--threads N` | 注册并发 1–10；有头建议 1 |
| `--accounts-file` | 账本路径，默认 `accounts_cli.txt` |
| `--fast` / `--no-fast` | 快速模式（默认开）：压缩 sleep、关截图 |
| `--headless` / `--no-headless` | 覆盖 `config.browser_headless`；生产建议 **有头** |
| `--no-browser-reuse` | 每号强制 quit 浏览器 |
| `--browser-recycle-every N` | 复用 N 次后完整回收 |
| `--mint-workers N` | CPA mint 并发；`0`=内联；`-1`=跟 config |
| `--inline-mint` | 强制注册线程内联 mint（调试） |
| `--cookie-snapshot` | 注册成功写 cookie 快照 |

```bash
uv run python register_cli.py -h
```

---

## 故障排查

| 现象 | 原因 / 处理 |
|------|-------------|
| Turnstile / 注册卡住 | 使用 **有头** 浏览器（`--no-headless`）；检查代理与 `turnstilePatch` |
| 协议 `sso invalid` / 落 sign-in | SSO 过期或已失效。v1.1.1+ 会自动剥 JWT 前导 `-`；若仍失败，确认第三段 normalize 后以 `eyJ` 开头，必要时重新登录拿 SSO |
| 协议 verify/approve 失败 | 会话态变化 / 风控；看日志后自动回退浏览器 |
| 一直 `authorization_pending` | 浏览器路径未完成 consent；需到「设备已授权」且 token 200 |
| `rate_limited` / `slow_down` | mint 过密；加大 backfill `--sleep`，稍后重试 |
| Hotmail 收不到码 | 检查四段凭证、ClientID/Token、REST/IMAP、alias 计数上限 |
| 日志出现「可用别名已耗尽」后任务退出 | **预期行为（v1.1.3+）**：硬资源/配置失败直接停批，不空转重试；提高 `hotmail_max_aliases_per_account` 或补充 `mail_credentials.txt` 后重启 |
| 有 token 但无 grok-4.5 | `cpa_base_url` 是否为 `https://cli-chat-proxy.grok.com/v1` |
| 注册成功但无 `cpa_auths` | `cpa_export_enabled`？看 `cpa_auths/cpa_auth_failed.txt` 与日志 |
| 远端注入失败 | `cpa_remote_inject`、SSH host、`sshpass` / 凭据文件、隧道是否通 |
| 活动监视器里很多 Chrome | 成功默认 **复用** 注册浏览器（`clear_session`），不是每号关闭；另有 Helper 子进程。CLI 启动时会清理 PPID=1 的旧 Drission 孤儿进程。若需每号硬关：`--no-browser-reuse` |

调试原则：以 **token 端点返回 `access_token` + `refresh_token`** 为准；probe 看 `/v1/models` 是否含 `grok-4.5`。

---

## 目录结构

```
grok-register/
  register_cli.py                 # CLI 批量注册
  grok_register_ttk.py            # 注册核心 + 邮箱通道（无桌面 GUI）
  apps/control_api/               # Web control plane API
  apps/web/                       # console10 SPA (Vite+Preact; dist preferred)
  scripts/run_control_api.sh
  cpa_export.py                   # 成功 hook：mint / 远端注入 / 备份
  account_backup.py               # 本地 backups/ 快照
  cpa_xai/
    protocol_mint.py              # SSO 纯 HTTP Device Flow
    mint.py                       # 协议 → 浏览器回退
    browser_confirm.py            # 浏览器 consent
    oauth_device.py / schema.py / writer.py / probe.py ...
  scripts/
    setup_simple.sh               # 一键配置 + 环境 doctor
    doctor_secrets.sh             # 密钥/跟踪卫生（不打印内容）
    backfill_cpa_xai_from_accounts.py
    backup_registered_accounts.py
    export_cpa_xai_from_grok_auth.py
    remint_expired_and_sync_authdir.py
  config.example.json             # 配置模板（含注释键）
  config.simple.example.json      # 对外简易模板
  mail_credentials.example.txt
  .env.example
  turnstilePatch/                 # Cloudflare Turnstile 辅助扩展
  pyproject.toml / uv.lock / mise.toml
  test_*.py

  # 以下为本地运行时文件（gitignore，勿提交）
  config.json
  mail_credentials.txt
  accounts_cli.txt
  cpa_auths/xai-*.json
  backups/
  logs/
  screenshots/
```

---

## 开发与测试

```bash
uv sync --extra dev
uv run python -m pytest -q          # 离线单测（CI 同款）
bash -n scripts/setup_simple.sh scripts/doctor_secrets.sh
bash scripts/doctor_secrets.sh      # exit 0 clean / 2 warn / 1 tracked secret
mise run test                       # 若使用 mise
mise run check                      # py_compile
```

Live Hotmail REST（**不要**在 CI 开）：

```bash
GROK_REGISTER_LIVE=1 uv run python test_hotmail_rest_code.py
```

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。GitHub Actions 在 `main` 上跑 shell 语法 + py_compile + 离线 pytest + 密钥路径守卫。

---

## 安全与合规

- **切勿提交密钥：** `config.json`、邮箱 refresh_token、账本密码/SSO、`cpa_auths` 的 access/refresh token
- **本地密钥卫生：**
  - `chmod 600 config.json .env mail_credentials.txt accounts_cli.txt`（若存在）
  - 不要把含 `cpa_auths/` / `mail_credentials.txt` 的目录放进 iCloud/Dropbox 明文同步
  - 提交前：`bash scripts/doctor_secrets.sh`（**不**打印文件内容）
  - 泄露后立刻轮换 Microsoft refresh_token、xAI/SSO、SSH 密码；见 [SECURITY.md](SECURITY.md)
- 分享本项目时只分享代码仓库或去掉运行时目录的干净拷贝
- 本工具**不能**生成 free Build 权限；`entitlement_denied` 时不要 remint 空转
- 免费 Build 有额度与风控；批量注册 / mint 请控速，合理使用
- 完整边界见 [DISCLAIMER.md](DISCLAIMER.md)；泄露处理见 [SECURITY.md](SECURITY.md)

---

## License

[MIT](LICENSE) © 2026 dengyie

---

## 相关

- **CLIProxyAPI / CPA：** 自备；将 `cpa_auths/xai-*.json` 放到 CPA auth-dir 即可热加载
- **免费 Grok 4.5：** 只走 Build OIDC + `cli-chat-proxy`，不是网页 SSO
- **仓库：** https://github.com/dengyie/ai-register-machine
- **版本：** 见 [CHANGELOG.md](CHANGELOG.md)（`pyproject.toml` version **1.3.0**；GitHub Release 标签按需打，勿假设已有 `v1.3.0` tag）
