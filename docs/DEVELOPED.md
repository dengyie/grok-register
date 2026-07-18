# 已落地功能一览（ai-register-machine）

本索引汇总**已在 main 落地**的功能开发，便于一眼看清「开发了什么」。每条附代表 commit；对应的设计文档（design spec / plan）已归档至 [`docs/archive/`](archive/)。

> 生产现状：三个 `./register.sh` 生产入口（`grok | mimo | chatgpt`）统一经 `register_core` Pipeline 调度（attribution / strategy burn-cool / 节点 L1+L2 preflight / 代理轮换 / 验证器 / JSONL sink）。Grok/MiMo adapter 仍 shell-out 到 legacy runner（`register_cli.py` + `grok_register_ttk.py` / `providers/mimo` Node runner）作为 adapter 目标 + 回滚后路（`GROK_LEGACY=1` / `MIMO_LEGACY=1` / `CHATGPT_LEGACY=1`）。in-process 重写（B 方案）未启动。下表「归档 spec/plan」指设计稿，不代表代码落点。

| 功能 | 交付内容 | 代表 commit | 归档 spec/plan |
|---|---|---|---|
| 多 provider hub + 目标架构 | register_machine 多产品中心、register_core 分层库（contracts/pipeline/providers/nodes/verify/sink）、MiMo provider、embedded mihomo | `4c0da27`、`96b641c`（改名为 ai-register-machine） | — |
| ChatGPT code-align | protocol/OTP 诊断、soft cool、mail-proxy 拆分、PKCE 最佳努力残留、mint_method 戳、产品退出诚实计数、软/硬回收 + 致命停止不空转 | `4229542`、`97064a3`、`c02283f`、`cd46a2a`、`0eb97d8`、`691ce9f` | [chatgpt-code-align](archive/specs/2026-07-16-chatgpt-code-align-design.md) |
| CPA mid-tier probe | 直连 vs CPA 双通道 chat gate 策略、hybrid mid-tier probe 接入 mint/export/backfill | `dc4002d`、`51689b4` | [cpa-mid-tier-probe](archive/specs/2026-07-16-cpa-mid-tier-probe-design.md) |
| nodes target preflight | target-aware L1(ipify) + L2(provider 域名) 节点预检、import/batch 前只发 healthy、smart_order、urllib status 探针 | `9866e34`、`d2d1ef6`、`af3ceba` | [nodes-target-preflight](archive/specs/2026-07-17-nodes-target-preflight-design.md) |
| register_profile_config（M1–M4） | profile 驱动 mailbox∥decode∥strategy；M2 burn+cool 策略；M3/M4 MiMo/Grok FIXED_EMAIL 注入；M2–M4 review 修复 | `ee668c5`、`57d620d`、`b520035` | [register-profile-config](archive/specs/2026-07-17-register-profile-config-design.md) |
| Clash egress pinning | 权威 Clash egress 探针、pin ChatGPT 组/优选、真实 backend 标签、bare-curl 假 CN 判别 | `f3a65ab`、`8023f95`、`9525177` | [nodes-target-preflight](archive/specs/2026-07-17-nodes-target-preflight-design.md) |
| ChatGPT human-pace | protocol API 步骤间 ~10s±1s 拟人节拍 | `f421837` | [chatgpt-code-align](archive/specs/2026-07-16-chatgpt-code-align-design.md) |
| SPA-stuck browser_boot 回收 | pre-email「您正在登录」粘滞 → `AccountRetryNeeded browser_boot` slot-retry；legacy wording 兼容归类 | `773404c`、`e635334`、`530f770` | — |
| Hotmail plus-alias 关停 | 别名农场 kill-switch（mode=off / allow=false），别名耗尽立即致命停止不空转 | `78fd894` | [chatgpt-code-align](archive/specs/2026-07-16-chatgpt-code-align-design.md) |
| register_core 迁移 A（生产入口切 Pipeline） | `./register.sh grok\|mimo\|chatgpt` 统一经 register_core Pipeline 调度（attribution/strategy/节点 preflight/代理轮换/验证器/sink）；Grok/MiMo 仍 shell-out legacy；`GROK_LEGACY`/`MIMO_LEGACY`/`CHATGPT_LEGACY=1` 回滚；退出码映射 0/1/2；路由静态门禁 `test_router.py` | `5c10946`、`18f2976`、(phase-3) | — |

## 待开发（backlog，未启动，本轮不做）

- **register_core 迁移收尾**：**A 已落地 / B in-process 未启动**。A（生产入口切 register_core 外壳）完成：`./register.sh grok|mimo|chatgpt` 经 Pipeline 调度，Grok/MiMo shell-out legacy 保留。B（把 `grok_register_ttk.py` + `cpa_xai` 改 in-process、保留 batch 并发）未启动。
- **CF `token长度=0` 日志噪声**：P3，非阻塞。
- 节点 catalog / Clash 产物进一步瘦身（`nodes.json` 2.2MB；可选）。

## Manual-required（需外部介入，本轮不解）

- **免费 Build chat 403 / console.x.ai entitlement**：账号侧权限，不 remint、不 soft inject；需 console 解锁或 grokbuild-proxy 服务端中转。
- **OpenAI `registration_disallowed`**：IP/风控侧，与 chat 403 同属 external。**量化复核（2026-07-18，pxed 全 ChatGPT sink）**：77 attempts / 0 ok；error_kind 分布 `registration_disallowed` 59 / `mail_miss` 9 / `provider` 6 / `fatal` 2 / `verify` 1。8 个 distinct 数据中心 IP 轮询全 disallowed（IP 非单一封禁，是 ASN/供应商集合门控）；canonical Clash GVPS(35.212.179.13)`https://auth.openai.com/` 返回 **403**（Google Cloud ASN 被 Cloudflare 预拒，未到 create_account）。`validate_otp` 全 200 / `mail_miss=0`（IP 轮询轮）→ 邮件/OTP 链无问题。**根因：当前代理池无未被 OpenAI 列禁的住宅 ISP ASN**；需住宅代理资产，本轮环境无解，不再烧号实跑。

验证入口：mint 后本地 probe `/v1/responses` 观察 403 body；ChatGPT 注册观察 create_account 是否仍 `registration_disallowed`（再测前先接住宅代理 ASN，否则 77/0 已充分）。

## 路由门禁验证入口（migrate milestone A）

生产入口外壳正确性（无需真机/真号）：

```bash
# dry Pipeline 编排冒烟（仅验证外壳打通，不期望产真号）：
SKIP_CLASH_PREFLIGHT=1 ./register.sh grok 1     # 经 register_core Pipeline + grok_adapter shell-out
SKIP_CLASH_PREFLIGHT=1 ./register.sh chatgpt 1  # 经 register_core profile 路径（in-process）
SKIP_CLASH_PREFLIGHT=1 ./register.sh mimo 1     # 经 register_core Pipeline + mimo_adapter shell-out
# 日志应出现 [register_core.pipeline] StrategyEngine precheck + 节点 preflight + 验证器 + sink 写入
```

回滚（单 provider 一行 env，复产号路径）：

```bash
GROK_LEGACY=1 ./register.sh grok N T          # 回到 run-register.sh → register_cli.py legacy 并发
MIMO_LEGACY=1 ./register.sh mimo N            # 回到 providers/mimo/run-register.sh Node runner
CHATGPT_LEGACY=1 ./register.sh chatgpt N       # 回到 providers/chatgpt/run-register.sh env 驱动
```

静态门禁：`python -m pytest test_router.py -q`（断言：三入口默认路由 register_core，`*_LEGACY=1` 回滚，`core` 子命令不变，chatgpt 按 `CHATGPT_EMAIL_SOURCE` 选 profile 并恢复 timeout 900 / proxy-rotate / sink 的 env override）。

环境变量契约（chatgpt 外壳切 register_core 后保留的 legacy 旋钮）：
- `CHATGPT_EMAIL_SOURCE`（默认 `cloudflare`）选 profile：`cloudflare→chatgpt-cf`、`tinyhost→chatgpt-tinyhost`、`gmail_imap→chatgpt-gmail`。**默认从 legacy `auto` 改为 `cloudflare`**（匹配 legacy runner 的 `CHATGPT_EMAIL_SOURCE=cloudflare` 默认）。
- `CHATGPT_EMAIL_DOMAIN`：tinyhost profile 不再钉 `publicvm.com`，由 adapter 读 env（huychau.online 等 override 生效；cf/gmail profile 本不钉 domain）。
- `CHATGPT_TIMEOUT`（默认 `900`）→ `--timeout`（修复 argparse 默认 1200 的静默覆盖）。
- `CHATGPT_PROXY_ROTATE_MODE` / `CHATGPT_PROXY_ROTATE_EVERY` → `--proxy-rotate` / `--proxy-rotate-every`（legacy 已转发，切外壳找回）。
- `CHATGPT_SINK`：仅显式设置时传 `--sink`；否则用 profile 的 `sink.path`（不再恒覆盖）。
- `REGISTER_EGRESS` / `CHATGPT_PROXY` / `CHATGPT_PROXY_LIST`：按既有行为转发；未设时 profile 默认（chatgpt profile 为 `clash:7897`，与 legacy Clash egress 一致）。

egress 边界说明（Grok）：grok-tinyhost profile 钉 `strategy.egress.mode: clash proxy 127.0.0.1:7897`，使 `profile_to_job` 设 `extra["proxy"]` → grok_adapter force-set 子进程 `PROXY/CPA_PROXY`（attempt proxy 胜过 ambient shell env），Pipeline 真正持有 Grok egress（而非靠 `run-register-core.sh` 继承的旧 `PROXY` env）。Pipeline 在此 backend 持有 attribution / strategy burn-cool / GrokChatVerifier / sink；Clash 叶子健康仍由 `preflight-clash-nodes.sh` 探（`nodes.json` L1/L2 catalog preflight 是 `list|auto` 独立 backend，非 Grok 默认）。

生产冒烟（Manual-required，非阻塞）：pxed 上 `./register.sh grok 1` 真实节点产 Grok reg+mint 确认切外壳后仍产出；ChatGPT 仍 `registration_disallowed`（外部依赖）。

### Grok fatal 契约修复（phase-3 收尾，pxed smoke 发现）

pxed 上 `./register.sh grok 1` 切外壳后真跑暴露一个回归 bug 并已修复（commit `4d7812a`）：

- 现象：register_cli 在邮件 OTP 阶段超时（tinyhost 桥 30s×5 轮询无码），exit=1，SUMMARY_JSON `"fatal":false,"fatal_reason":""`；但 grok_adapter 旧的致命探测 `any(k in lower(out) for k in ("alias","耗尽","exhausted","fatal","fail-fast","致命"))` 命中了 SUMMARY_JSON body 中**始终存在**的键 `"fatal"`/`"fatal_reason"` → 把一次可重试的 OTP 超时误升为 `FailFastError` → Pipeline `fail-fast stop` + exit=2（致命契约），整批停。
- 根因：致命判定应走 register_cli 权威退出码契约，而非子串匹配。register_cli contract：`exit 2 = fatal`（`_fatal_stop` set，SUMMARY_JSON `"fatal":true` + `"fatal_reason"`）；`exit 1 = 可重试 not-product`（OTP 超时/验证失败等）；`exit 0 = product ok`。
- 修复：grok_adapter 改用 `exit==2` 为致命主信号，SUMMARY_JSON `"fatal"`/`"fatal_reason"` 作交叉校验（JSON 内 `fatal:true` 即使 exit=1 也致命，以防契约漂移），仅当 register_cli 无任何输出（spawn 前 crash / 无 summary）时才落老逻辑判致命。致命时携带脱敏后的 register_cli 输出尾（边界证据日志，生产失败可诊断）。
- 5 个契约测试固化（`test_register_core_layers.py::TestGrokFatalContract`）：exit-1+fatal:false 不致命（pxed 回归）、exit-2 致命、summary fatal:true 升级、无输出 spawn 致命、带 Traceback 的 OTP 超时不致命。本地 + pxed 均 pass。
- 真实验证（pxed）：切外壳后 `./register.sh grok 1` 现在在可重试邮件超时下退出契约 1（not-product），而非契约 2（fatal）——Pipeline 不再因单次瞬时邮件失败停整批；账号本身是否产出仍取决于 tinyhost 邮件送达（外部依赖）。

### shell↔python exit-契约收口（milestone A review 修复）

`run-register-core.sh` 把 `register_core` 运行结果映射回 legacy 0/1/2 时，旧实现 grep 日志正文里的 `fail_fast|fatal:|Traceback|...` 推断致命，漏判两类 batch-stop：

- strategy 域名 burn 硬停：`stopped_reason="strategy: domain burned: ..."`，无 `fail_fast`/`fatal:` 子串 → 旧 grep 落 exit 1（not-product），但实际是域已不可用、需运维换域的 batch-stop，应是 exit 2。
- post-loop `fail_fast` 停（读 `result.error_kind`）：`stopped_reason` 为 `registration_disallowed` 之类（非 `"fatal"` 字面）→ 同样被降级为 exit 1。

修复：`register_core.cli.cmd_run` 增权威行 `CONTRACT_EXIT:N`，N 由 `_contract_exit_from_stats(stats)` 唯一推导——以 `stats.stopped_reason` 非空为 batch-stop 信号（Pipeline 内每条 early-break 路径都 set 它；可重试耗尽干净退出留空）。`run-register-core.sh` 改读该行，无该行时按 python 退出码兜底（`code==2`→fatal、`code==0`→ok、`code==1` 且有 Traceback→fatal 否则 not-product）。属 [[feedback-decode-once-at-boundary]] 同类反模式的 shell 侧收口：负责人信号只解一次。

- 7 个契约回归测试固化（`test_register_core_layers.py::TestContractExit`）：ok→0、可重试耗尽→1、loop-fatal→2、strategy domain-burn→2、post-loop kind-stop→2、mail_miss→2、unexpected→2。

