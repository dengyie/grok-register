# register_core — 分层通用注册框架

以 **ai-register-machine monorepo** 为主仓，把 Grok / MiMo / ChatGPT（及后续产品）挂到同一套层边界上。

```text
CLI / register.sh / --profile profiles/*.yaml
       │
       ▼
  config/*     register.v1 profile → job + composite mail
  pipeline（编排：count、fail-fast、verify、sink、StrategyEngine）
       │
       ├─ providers/*   产品注册（chatgpt in-process；mimo/grok shell + FIXED_EMAIL inject）
       ├─ mailbox/*     只开箱（allocate/release）
       ├─ decode/*      只拉信解 OTP（wait_otp）
       ├─ email/*       兼容层 + CompositeEmailSource
       ├─ strategy/*    burn/cool + fail_fast_kinds
       ├─ verify/*      注册后能力探测
       ├─ sink/*        结果落盘（JSONL 0600）
       └─ contracts + errors
```

## 生产权威 vs 编排入口

| 路径 | 用途 |
|------|------|
| `./register.sh grok` / `register_cli.py` | **Grok 生产权威** |
| `./register.sh mimo` / `providers/mimo/run-register.sh` | **MiMo 生产权威** |
| `./register.sh core` / `python -m register_core` | 分层编排 + 结果归因；profile 驱动 mailbox∥decode∥strategy |

**邮箱真共享（M3/M4）：** `mimo` / `grok` 适配器通过 `prepare_mail_inject` 写入 `FIXED_EMAIL` + `EMAIL_PROVIDER=fixed`，OTP 经 `OTP_HELPER` / `register_core.tools.poll_otp` 回读 core 邮箱。Profile 中 `mailbox`+`decode` 对三家产品均可用。

## 层职责

| 层 | 职责 | 不做 |
|----|------|------|
| **contracts** | 统一结果；`to_public_dict` 脱敏 password/secret | 业务逻辑 |
| **mailbox / decode** | 开箱 ∥ OTP；`CompositeEmailSource` 对外仍是 EmailSource | 打开注册页 |
| **providers** | signup；成功必须 **本轮归因**（ledger/RESULT_JSON/文件增量） | 读历史 tail 当成功 |
| **strategy** | fail_fast_kinds；burn track ip\|domain\|proxy；soft cool | 产品 DOM |
| **verify** | key 形态 / 账本存在；live chat 仍走 cpa_xai | 假装 chat 已通 |
| **sink** | JSONL `O_CREAT|0600` | 改 provider |
| **pipeline** | count + strategy precheck/on_result；verify 失败一律 `ok=False` | 产品 DOM |

## 成功归因（硬规则）

- **MiMo：** `RESULT_JSON:` 行，或 `accounts.jsonl` / `success_keys.txt` 的 **调用前 offset 之后增量**；禁止单独用历史文件末行。
- **Grok：** `accounts_cli` **增量** 且含 SSO；仅邮箱无 SSO → **失败**（pending）；exit=0 无本轮邮箱 → **失败**。
- 子进程 **timeout 杀进程组**（`start_new_session` + `killpg`），降低 Chrome 孤儿。

## CLI

```bash
python -m register_core list
# Preferred: one profile drives mailbox + decode + strategy + provider
python -m register_core run --profile profiles/chatgpt-cf.example.yaml -n 1
python -m register_core run --profile profiles/chatgpt-tinyhost.example.yaml
python -m register_core run --profile profiles/mimo-tinyhost.example.yaml -n 1
python -m register_core run --profile profiles/grok-tinyhost.example.yaml -n 1
# Legacy flags still work
python -m register_core run -p mimo -n 1 --sink output/core-mimo.jsonl
python -m register_core run -p grok -n 1 --no-verify
python -m register_core run -p chatgpt -n 1 --email-source gmail_imap
```

Profile schema: `docs/superpowers/specs/2026-07-17-register-profile-config-design.md`

## Strategy（burn / cool）

Profile `spec.strategy` → `job.extra["_strategy"]` → `StrategyEngine`：

- `fail_fast` + `fail_fast_kinds`（默认含 `registration_disallowed` / `unsupported_email` / `fatal` / `verify`）
- `burn.enabled` + `track: [ip, domain, proxy]` + `on_kinds` + optional `state_path`（JSON 0600）
- `cool_soft_seconds`：IP 不在 hard burn track 时软冷却

## Egress nodes（项目自有，不依赖 Clash）

```bash
cp nodes.example.json nodes.json   # 填入你控制的 HTTP/SOCKS 代理
python -m register_core nodes list
python -m register_core nodes check
python -m register_core nodes add 'http://user:pass@host:port' --label us1
```

- 目录：`register_core/nodes/`（catalog / manager / health）
- 接线：`register_core/util/proxy.py` 在 `PROXY_LIST` 为空时从 `nodes.json` 拉池并自动 `list` 轮换
- 关闭：`REGISTER_NODES=0`
- 详见 `register_core/nodes/README.md`

## 新增产品

完整清单见仓库根 [docs/ADDING_PROVIDER.md](../docs/ADDING_PROVIDER.md) 与 [ARCHITECTURE.md](../ARCHITECTURE.md)。

1. 复制 `providers/_template` → `providers/<name>`
2. 实现 `RegisterProvider.register_one` → 本轮可验证的 `RegisterResult`
3. 注册到 `providers/registry`
4. 邮箱：in-process 直接 `EmailSource`；shell runner 用 `prepare_mail_inject` + FIXED_EMAIL / OTP_HELPER
5. 单测覆盖：历史污染、exit0 无增量、public 脱敏、fail-fast、strategy burn

## 测试

```bash
make test-unit
# or
python test_register_core_layers.py
python test_register_profile.py
python test_register_strategy.py
python -m pytest tests/unit test_register_core_layers.py test_register_profile.py test_register_strategy.py -q
```
