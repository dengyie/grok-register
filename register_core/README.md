# register_core — 分层通用注册框架

以 **ai-register-machine monorepo** 为主仓，把 Grok / MiMo（及后续产品）挂到同一套层边界上。

```text
CLI / register.sh
       │
       ▼
  pipeline（编排：count、fail-fast、verify、sink）
       │
       ├─ providers/*   产品注册（grok / mimo）— 黑盒适配已验证 runner
       ├─ email/*       邮箱来源（allocate + poll_otp）— 供 in-process 使用
       ├─ verify/*      注册后能力探测（诚实 deferred / shape）
       ├─ sink/*        结果落盘（JSONL 0600）
       └─ contracts + errors
```

## 生产权威 vs 编排入口

| 路径 | 用途 |
|------|------|
| `./register.sh grok` / `register_cli.py` | **Grok 生产权威** |
| `./register.sh mimo` / `providers/mimo/run-register.sh` | **MiMo 生产权威** |
| `./register.sh core` / `python -m register_core` | 分层编排 + 结果归因；**非**替代上述 runner 的内核 |

`grok` / `mimo` 适配器是 **black-box**：`email_source` 只能是 `provider`（适配器内部邮箱）。传入 `tinyhost` 等会 **直接报错**，避免假分层。

## 层职责

| 层 | 职责 | 不做 |
|----|------|------|
| **contracts** | 统一结果；`to_public_dict` 脱敏 password/secret | 业务逻辑 |
| **email** | 一号一箱 + OTP；可插拔 | 打开注册页（black-box 时） |
| **providers** | signup；成功必须 **本轮归因**（ledger/RESULT_JSON/文件增量） | 读历史 tail 当成功 |
| **verify** | key 形态 / 账本存在；live chat 仍走 cpa_xai | 假装 chat 已通 |
| **sink** | JSONL `O_CREAT|0600` | 改 provider |
| **pipeline** | count + fail-fast；verify 失败一律 `ok=False` | 产品 DOM |

## 成功归因（硬规则）

- **MiMo：** `RESULT_JSON:` 行，或 `accounts.jsonl` / `success_keys.txt` 的 **调用前 offset 之后增量**；禁止单独用历史文件末行。
- **Grok：** `accounts_cli` **增量** 或 `+ 注册成功: email`；exit=0 无本轮邮箱 → **失败**。
- 子进程 **timeout 杀进程组**（`start_new_session` + `killpg`），降低 Chrome 孤儿。

## CLI

```bash
python -m register_core list
python -m register_core run -p mimo -n 1 --sink output/core-mimo.jsonl
python -m register_core run -p grok -n 1 --no-verify
python -m register_core run -p chatgpt -n 1 --email-source gmail_imap
# 错误示例（会 exit 2）：
python -m register_core run -p mimo --email-source tinyhost
```

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
4. 若 in-process：可接 `EmailSource`；若 black-box：加入 pipeline 黑名单并文档说明
5. 单测覆盖：历史污染、exit0 无增量、public 脱敏、fail-fast

## 测试

```bash
make test-unit
# or
python test_register_core_layers.py
python -m pytest tests/unit test_register_core_layers.py -q
```
