# Design: free Build chat 经 tebi CPA 中转 probe

**Date:** 2026-07-16  
**Status:** approved approach A (user: `a`)  
**Repo:** `dengyie/ai-register-machine` (local: grok-register)  
**Related:** [[Grok 注册机与 free Build CPA]], CPA 文档索引, chat entitlement hard gate (`cpa_xai/probe.py`, `writer.py`)

---

## 1. Problem

注册机 mint 后的 **chat gate** 目前 **本地直连** `https://cli-chat-proxy.grok.com/v1`，手搓 `DEFAULT_CLIENT_HEADERS`（已与 grokbuild-proxy 默认常量对齐）。

生产客户端则走 **tebi CLIProxyAPI**（`:8317` / `cpa.mangoq.ccwu.cc`）：CPA 对 free Build 已具备 rewrite（空 / `api.x.ai` → cli-chat-proxy）与 CLI 头注入。

结果：

- **双身份路径**：gate 测的是「注册机 + 直连 + 本地头」；线上成功定义是「CPA 中转」。
- backlog「grokbuild-proxy / CLIProxyAPI 中转架构」指的是 **统一 mid-tier**，不是再起一个与 tebi CPA 平行的 grokbuild-proxy 生产面。
- **L4 403 entitlement** 仍是账号侧问题；中转 **不能** 当解锁手段。

## 2. Goals

1. 产品级 `chat_ok` / inject 门禁的 probe，默认经 **现有 tebi CPA** 中转（与 live 路径一致）。
2. 写入的 `xai-*.json` 仍保持 CPA 期望的上游 `base_url`（cli-chat-proxy），不把公网 CPA URL 写进 auth 文件。
3. 保持现有产品规则：hard gate、`entitlement_denied` fail-fast、禁止无 `chat_ok` live inject。
4. 保留 debug 直连开关，便于区分「号问题」与「CPA/网络问题」。

## 3. Non-goals

| 不做 | 原因 |
|---|---|
| 修复 L4 permission-denied | 账号 entitlement / console.x.ai；Manual-required |
| 软化 `probe_chat` hard gate | 用户明确保持 |
| 部署独立 grokbuild-proxy 服务 | 与 tebi CPA free Build 能力重叠；运维成本高 |
| 改 OAuth mint（PKCE / device） | mint 仍打 auth.x.ai；中转只覆盖 **probe HTTP** |
| 在 pxed 起生产 CPA | 生产只在 tebi |
| 第三方 GrokFree 中转 token | 与官方 OAuth free Build 无关 |

## 4. Decision

**选用方案 A：经现有 tebi CLIProxyAPI 做 mid-tier probe。**

放弃方案 B（独立 grokbuild-proxy）作为本 milestone 交付。  
方案 C（双 probe）仅以配置开关形式保留诊断能力，不当第二真相源。

### 4.1 为什么是 A

- tebi CPA **已支持** free Build rewrite + CLI 身份。
- 注册机已有 `cpa_remote_inject` → tebi live/inventory 的运维路径；中转应复用同一生产面。
- 头对齐（`grok-pager` / dual UA）已在 `cpa_xai/schema.py` 完成；剩余是 **路径统一**，不是再抄一套 client.go。

## 5. Architecture

```text
Mac 开发
  ↔ pxed /personal/grok-register  （注册 + mint + probe）
  ↔ tebi /personal/cpa + /root/.cli-proxy-api  （生产 CPA :8317）
  公网 cpa.mangoq.ccwu.cc → tebi CPA

[mint]
  SSO/PKCE → access_token → write cpa_auths/xai-<email>.json
  base_url = https://cli-chat-proxy.grok.com/v1   # 上游，供 CPA rewrite

[probe 默认 cpa_probe_via=cpa]
  registerer → CPA OpenAI-compatible base (cpa.mangoq.ccwu.cc/v1 或 tunnel)
               + CPA API key
               + 必须钉到「本账号」凭据（见 §6）
            → CPA → cli-chat-proxy（rewrite + headers）
            → classify → stamp chat_ok / entitlement_denied / …

[probe debug cpa_probe_via=direct]
  registerer → cli-chat-proxy + DEFAULT_CLIENT_HEADERS + Bearer access_token
  （现行为；保留）

[inject]
  仅 chat_ok → live /root/.cli-proxy-api + inventory /personal/cpa/auths
  cpa_remote_inject_require_chat_ok=true 不变
```

## 6. Credential pinning（P0 语义）

CPA 默认从 **池** 选 xAI 凭据。若 probe 不钉死「刚 mint 的 email」，会出现：

- **假阳性**：池里别的 chat_ok 号替测 → 错误 inject  
- **假阴性**：池里坏号挡枪 → 误杀好号  

### 6.1 策略顺序（实现时按序探测能力）

1. **Preferred — 管理/API 按 credential 路由**  
   若 tebi 当前 CPA 版本支持按 auth 文件名 / email / credential_id 指定上游凭据：probe 必须带该标识。  
2. **Fallback — inventory-only + 钉凭据**  
   gate 前只允许写 inventory，不写 live；probe 仍钉该文件。  
3. **Hybrid（能力不足时的安全默认）**  
   - **账号门禁**（`chat_ok` / `entitlement_denied` stamp）继续 **direct** + 本 token  
   - **中转验证**：inject 后（或 inventory 写入后）再打一枪 CPA 路径 smoke，结果写入可选字段（如 `probe_via_cpa_ok`），**不得**单独作为 inject 放行条件替代 `chat_ok`  

实现计划必须 **先核实 tebi CPA 是否可钉凭据**；不可钉则交付 hybrid，并在 ops 文档标明。

### 6.2 禁止

- 为了 probe 先 live inject 再测（循环依赖 + 污染 live 池）  
- 用池级任意成功响应给当前 email 盖 `chat_ok=true`

## 7. Config surface

新增（示例；默认值以实现计划冻结为准）：

| Key | 含义 |
|---|---|
| `cpa_probe_via` | `cpa` \| `direct`（推荐默认：能力确认后 `cpa`，否则 hybrid 下门禁仍 direct） |
| `cpa_probe_base_url` | CPA 对外 OpenAI base，如 `https://cpa.mangoq.ccwu.cc/v1` |
| `cpa_probe_api_key` | 调用 CPA 的 API key（非 xAI access_token） |
| `cpa_base_url` | **不变语义**：写入 auth 与 direct probe 的上游 base（cli-chat-proxy） |
| `cpa_probe_chat` / `cpa_probe_chat_required` | 不变，硬门禁默认 true |

密钥：优先 env / 已有 secrets 约定；禁止提交真实 key。`config.example.json` 只写注释与占位。

## 8. Code touch map（实现阶段，非本 spec 交付代码）

| 区域 | 变更方向 |
|---|---|
| `cpa_xai/probe.py` | 抽象 transport：`direct_bearer` vs `cpa_openai`；分类逻辑复用 `classify_chat_probe` |
| `cpa_xai/mint.py` / `cpa_export.py` | 从 config 传入 probe via / base / key；stamp 字段集合不变 |
| `scripts/backfill_chat_stamps.py` | 同 transport，避免 backfill 与 mint 分叉 |
| `config.example.json` | 文档化新键 |
| 测试 | mock 两种 transport；inject hard gate 仍要求 `chat_ok`；403 仍 entitlement |
| Obsidian | 更新「Grok 注册机与 free Build CPA」：mid-tier = tebi CPA |

**不改：** OAuth PKCE/device、remint skip denied、ledger、`build_chat_stamp_from_result` 禁止 null `chat_ok`。

## 9. Failure mapping

| 现象 | 产品分类 | 动作 |
|---|---|---|
| `/responses` 403 permission-denied | `entitlement_denied` | stamp + ledger；不 remint；不 inject |
| 直连 426 version outdated | `auth_or_protocol` | 查头；非账号农场 |
| CPA API key 401/403 | 运维配置错误 | 不标 entitlement_denied |
| CPA 502 / timeout / 429 中转层 | `transient` / retryable（按现有策略） | 可重试；不 inject |
| free-usage-exhausted | `usage_exhausted` | 不 inject；非 entitlement |
| 钉不住凭据且未走 hybrid | **实现阻断** | 禁止默认真 `cpa` 门禁 |

## 10. Risks & mitigations

| 风险 | 缓解 |
|---|---|
| 假 `chat_ok`（错凭据） | §6 pinning；否则 hybrid |
| pxed→tebi 网络不稳 | 与 inject 同 tunnel（tebi-tunnel）；timeout/retry 对齐 |
| 双模式分叉 | 单入口 `probe_*` + transport 参数；单测强制两路径分类一致 |
| 把中转当成 L4 解药 | 文档 + 验收：denied 号经 CPA 仍 denied |

## 11. Acceptance criteria

1. 现有 entitlement / remint / inject / inventory 单测全过。  
2. 配置 `cpa_probe_via=direct`：行为与当前生产一致（回归）。  
3. 在 **可钉凭据** 或 **hybrid** 约定下：  
   - 已知 denied 号 → `entitlement_denied`，无 remint 空转  
   - 已知 chat_ok 号 → stamp 后可 inject；经 CPA 客户端 MINT_OK  
4. auth 文件 `base_url` 仍为 cli-chat-proxy，不被改成 cpa.mangoq。  
5. 无真实 secret 进入 git。

## 12. Rollout sketch

1. 核实 tebi CPA 钉凭据能力（read-only / 文档 / 管理 API）。  
2. 实现 transport + config + 测试。  
3. 默认：若可钉 → `cpa`；否则 hybrid + 文档。  
4. pxed 部署注册机；小流量 backfill/smoke。  
5. 更新 Obsidian free Build 文与 CPA 索引交叉链接。

## 13. Open questions (implementation, not design blockers)

1. tebi 当前 `eceasy/cli-proxy-api` 版本是否暴露 per-credential 路由头/管理字段？  
2. probe 走公网 `cpa.mangoq.ccwu.cc` 还是 SSH 隧道内网 `:8317`（延迟/鉴权差异）？  
3. hybrid 时 `probe_via_cpa_ok` 是否写入 auth 文件（可选观测字段）？

默认实现倾向：优先 tunnel/内网与 inject 一致；观测字段可进 stamp 但不进 inject 门闩。

## 14. Spec self-review

- [x] 无 TBD 作为核心行为空洞（open questions 仅实现选型）  
- [x] 与 hard gate / inject 规则无矛盾  
- [x] 范围单 milestone：probe 中转统一，不扩 OAuth/新服务  
- [x] 钉凭据失败路径有明确 hybrid，避免 silent wrong gate  

---

## Approval

- Approach: **A** (user message `a`, 2026-07-16)  
- Next: user review of this file → `writing-plans` → implementation milestone (P0/P1 only)
