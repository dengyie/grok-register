# MiMo @ pxed（register-machine monorepo）

**源码权威**：部署目录下 `providers/mimo`（常见 `/personal/grok-register` 或 `register-machine`）  
**Node runtime**：`/personal/mimo-register`（`npm ci` + playwright + 上游 `dist/`）

上游参考：[Rossevelt1313/MimoAuto](https://github.com/Rossevelt1313/MimoAuto)  
生产 runner：`scripts/register-one.js`（Geetest + 双 OTP + Create Key；**不要**只靠上游 worker）

## 一键（推荐）

```bash
# monorepo hub
bash /personal/grok-register/register.sh smoke mimo
bash /personal/grok-register/register.sh mimo
```

## 依赖

- clash：`bash /personal/grok-register/start-clash-for-grok.sh`（7897）
- Xvfb `:99`
- 禁止 `ga.dp.tech:8118`；git 用 `git -c http.proxy=http://127.0.0.1:7897`

## 产物

- `/personal/mimo-register/output/success_keys.txt`
- `accounts.jsonl` / 失败截图

## 仅 TTS

Key 后调 `mimo-v2.5-tts`，无需 Pro。
