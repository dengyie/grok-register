# MiMo provider（ai-register-machine）

Xiaomi MiMo API Key 注册机，位于 monorepo 的 `providers/mimo`。

目标：**只要 TTS Key**（`mimo-v2.5-tts` 等），不跑 Pro 农场。

## 与 Grok 的关系

| | Grok | MiMo |
|---|---|---|
| 入口 | `./register.sh grok` | `./register.sh mimo` |
| 运行时 | Python + DrissionPage | Node + Playwright |
| 出口 | 共享 clash/mihomo `127.0.0.1:7897` | 同左 |
| 邮箱 | Gmail / 独立临邮（禁 Hotmail alias） | tinyhost（sanitize 域名） |
| 产物 | `cpa_auths/`、accounts（Grok/CPA 专用） | `output/success_keys.txt`、`accounts.jsonl`；CPA 走 **openai-compatibility / xiaomimimo**（不是 xai auth-dir） |
| 验证码 | Turnstile 等 | **Geetest 滑块**（fullbg−bg 差分） |

**不做**语言层硬合并（两套浏览器栈）。共享的是：调度入口、代理/Xvfb 运维、fail-fast、pxed 布局约定。

## 生产路径

```bash
# 在 monorepo 根
./register.sh smoke mimo
./register.sh mimo          # 注册 1 个号
COUNT=1 ./register.sh mimo
```

底层 runner：`scripts/register-one.js`（已 E2E 验证）：

1. Singapore 邮箱表单  
2. Geetest slide  
3. 注册 OTP（tinyhost）  
4. Account Authentication 二次 OTP  
5. platform console 协议勾选  
6. Create API Key → clipboard / DOM  

上游 [MimoAuto](https://github.com/Rossevelt1313/MimoAuto) 的 `worker` **不解 Geetest**；pxed 请以 `register-one.js` 为准。

## pxed 部署

```text
/personal/grok-register          # 本 monorepo（含 providers/mimo 源码）
/personal/mimo-register          # Node runtime：npm ci + playwright chromium + dist/
/personal/clash                  # mihomo
```

同步脚本（源码权威在 monorepo）：

```bash
# 在 pxed
cp -f /personal/grok-register/providers/mimo/scripts/register-one.js \
      /personal/mimo-register/scripts/register-one.js
bash /personal/grok-register/register.sh mimo
# 或
MIMO_RUNTIME=/personal/mimo-register bash /personal/grok-register/providers/mimo/run-register.sh
```

首次装 runtime（若尚未有 `/personal/mimo-register`）：

```bash
# clash 先起
bash /personal/grok-register/start-clash-for-grok.sh
git -c http.proxy=http://127.0.0.1:7897 clone https://github.com/Rossevelt1313/MimoAuto /personal/mimo-register
cd /personal/mimo-register && npm ci
# 拷贝 monorepo 生产 runner
mkdir -p scripts
cp /personal/grok-register/providers/mimo/scripts/*.js scripts/
npx playwright install chromium
```

## 环境变量

见 `.env.example`。常用：

- `MIMO_PROXY=http://127.0.0.1:7897`
- `XIAOMI_REGION=Singapore`
- `OTP_RETRIES=35`
- `MIMO_INVITE_CODE=`（可选）
- `HEADLESS=true`

## 产物

- `output/success_keys.txt`
- `output/accounts.jsonl`（email / password / apiKey）
- 失败截图：`output/failed-final.png`、`geetest-*.png`、`after-otp-round-*.png`

## 注入 CPA OpenAI provider

MiMo 是 **OpenAI-compatible API key**，写入 tebi `config.yaml` 的 `openai-compatibility` 渠道 **`xiaomimimo`**（`base-url: https://api.xiaomimimo.com/v1`），**不是** `auth-dir` 里的 `type: xai` JSON。

```bash
# 必须显式 --config（或 CPA_CONFIG）。默认不再指向生产。
# 本机 → tebi（BatchMode ssh 别名）；生产路径需额外确认开关
python3 providers/mimo/inject_cpa_openai.py \
  --ssh tebi-tunnel \
  --config /personal/cpa/config.yaml \
  --i-understand-production \
  --from-jsonl /path/to/accounts.jsonl

# 先 dry-run
python3 providers/mimo/inject_cpa_openai.py \
  --config /tmp/cpa-config.yaml --dry-run --from-file keys.txt

# 或在 tebi 上本地
python3 inject_cpa_openai.py \
  --config /personal/cpa/config.yaml \
  --i-understand-production \
  --from-file /personal/mimo-register/output/success_keys.txt
```

- 幂等追加 `api-key-entries`；写前 `config.yaml.bak-mimo-<ts>`
- **不 SIGHUP**（CLIProxyAPI fsnotify 热加载）
- 默认模型：`mimo-v2.5-tts` / voiceclone / voicedesign
- **拒绝**隐式生产路径：无 `--config`/`CPA_CONFIG` 直接 exit 2；生产 path 需 `--i-understand-production`（或 `--dry-run`）
- 单测：`python3 test_mimo_cpa_openai_inject.py`

## 已知坑

- 容器坏代理 `ga.dp.tech:8118`：启动前 `unset` 代理，只用 clash  
- tinyhost 域名尾部 `.` / infinityfree → Next 禁用（runner 已 sanitize）  
- 回调 URL 查询串含 `platform.xiaomimimo.com` → 必须用 **hostname** 判断是否已上平台  
- 控制台中文弹窗：创建 Key 前必须勾协议 checkbox  
- 禁止空转批量；一次失败 fail-fast  
