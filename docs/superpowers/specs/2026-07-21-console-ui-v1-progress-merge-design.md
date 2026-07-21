# Console UI · v1 进度元素合并设计

**Date:** 2026-07-21  
**Status:** Draft v2 — design self-review amendments applied; awaiting user approval  
**Product:** ai-register-machine Web control plane (`apps/web` + `apps/control_api`)  
**Related:** `docs/superpowers/specs/2026-07-21-web-control-plane-design.md`（基线架构仍有效）  
**Baseline commits / surfaces:**
- **v1（c44ac22）:** top-nav Overview / Config / Import / Runs；状态卡片 + JSON 预览
- **console4（当前）:** sidebar 注册 / 账号池 / 邮箱接码 / 节点池 / 导入 / 设置；右侧 pills + status-card + log
- **Backend 已就绪:** `apps/control_api/progress.py` → `build_progress` 已产出 `steps[]` / `timeline[]` / KPI 计数，UI 未消费

**Self-review (2026-07-21):** §14 为完整 review 结论；§3.4 / §4.1.4 / §5.7–5.9 / §9 已按「功能给全 + UI 好看」修订。

---

## 1. Problem

console 重写保留了更好的 **IA 骨架**（侧栏多页、注册表单 + 日志分栏、账号/节点/邮箱运维面），但把 **进度呈现** 退化成：

| 当前 console4 | 问题 |
|---------------|------|
| `run-pills` 一行 chip | 信息密度低，没有「还差多少 / 完成百分比」的视觉权重 |
| `status-card` 纯文本 phase | 有 `phase_title`/`phase_detail`，但没有步骤轨、没有双进度条 |
| 无 KPI 网格 | `complete/goal`、`batch_gained/target`、`product_ok`、`remain` 后端有，前端未做卡片 |
| 无 step rail | `GET /api/runs/current` 已 flatten `steps[]`（done/active/pending），UI 完全忽略 |
| 无 timeline | `timeline[]` 已有，UI 未展示 |
| 丢 v1 启动面 | kind / product / `SKIP_CLASH_PREFLIGHT` 在 API 与 v1 Runs 表单存在，console 写死 `grok_supervisor`+`grok` |

v1 的优点不是「截图里的配色」，而是 **一眼能读完任务健康度**（status card 一行：`product_ok · run=ALIVE · complete · zero`）+ **Runs 启动参数完整**。console 的优点是 **骨架与运维页**。本设计只做 **注入与归位**，不重做整站视觉、不生搬参考图。

---

## 2. Goals / Non-goals

### 2.1 Goals

1. **保留 console 骨架**：sidebar 六页、注册 split（左表单 / 右状态+日志）、账号池 / 邮箱 / 节点 / 导入 / 设置。
2. **注入 v1 级进度可读性**：KPI 卡片 + 双进度条 + 步骤轨 + 精简 timeline，全部吃现有 `build_progress` / `run_status` 字段，**不新开后端契约**（除非 UI 缺字段再补，见 §6）。
3. **恢复 v1 丢掉的控制面能力**（不丢产品功能）：
   - multi-kind 启动：`grok_supervisor` | `register_sh`
   - multi-product：`grok` | `mimo` | `chatgpt`（`register_sh` 时）
   - `SKIP_CLASH_PREFLIGHT` 表面（allowlist 已有）
   - Overview 级「全局一眼」信息（可并入注册页顶栏，不必强行加第七页，见 §4.1）
4. **产品契约硬约束 UI 不得反向诱导**：
   - mint 路径 `CPA_REMOTE_INJECT=false` / `CPA_PROBE_CHAT=false`（supervisor 硬编码）
   - 批边界导入仅 `CPA_BATCH_END_INJECT` + EVERY knobs
   - disk-first：主链路止于 `cpa_auths` 落盘
   - fail-fast：zero  streak / stuck 必须醒目
   - 不全局 `pkill`；stop 只打 registry/lock pid + process group
5. **静态 vanilla HTML/CSS/JS**，无 Node 构建；asset 版本 bump（如 `?v=console5`）。

### 2.2 Non-goals

- 不做参考截图像素级复刻 / 换皮肤大赛
- 不引入 React/Vue/npm
- 不重写 `progress.py` 相位机（可微调文案，不改算法优先级）
- 不做 job 队列 / 多 host / 实时 SSE 强制升级（仍 4s poll；可选后续）
- 不启用线上 `CPA_BATCH_END_INJECT` 默认 true
- 不改生产登录 `mango / …`；bootstrap admin 仍仅空用户库
- 不把 coinbot / 无关进程纳入 stop 范围
- 不做 Turnstile 浏览器池接入（工具栏 WIP 按钮保持 disabled）

---

## 3. Feature inventory

### 3.1 v1（c44ac22）有、console 弱化或丢失

| 能力 | v1 | console4 | 本设计 |
|------|----|----------|--------|
| Overview hub | 独立页：status card + full JSON | 无独立页；注册页偶尔拉 overview 只取 `product_ok` | **注册页顶 KPI + 可选折叠 JSON**；不强制新 sidebar 项 |
| 状态一行可读 | `product_ok · ALIVE · complete · zero` | pills 散点 | **KPI 网格 + 进度条** 取代纯 pills 为主展示 |
| kind 选择 | `grok_supervisor` / `register_sh` | 写死 supervisor | **高级启动折叠区** 恢复 |
| product 选择 | grok/mimo/chatgpt | 写死 grok | **register_sh 时显示 product** |
| SKIP_CLASH_PREFLIGHT | 表单字段 | 无 | **高级启动折叠区** checkbox |
| Runs 独立页 | 有 | 合并进「注册」 | **保持合并**（骨架优先）；启动控件在注册左栏 |
| Config 页 | 精简字段 | 设置 + 注册表单双写 | **保持** 设置全量 + 注册协议子集 |

### 3.2 console 已有、必须保留

| 页 | 保留内容 |
|----|----------|
| 注册 | 邮箱/域名/target/threads/mode/tag、Turnstile 超时、CHUNK、sso-only、proxy_rotate、proxy_list、batch-end inject + EVERY、probe 强制关、开始/停止/刷新、log which/tail/follow |
| 账号池 | complete 列表、筛选分页、soft-delete |
| 邮箱接码 | provider/domains/secrets、Hotmail 凭证导入、plus-alias 禁默认 |
| 节点池 | Clash 叶子 + catalog 双 tab、测活、筛选 |
| 导入 | nodes / mail / auths / pack（auth 默认 no-remote） |
| 设置 | config 全量、bearer token、selfcheck/cleanup |
| 登录门 | cookie session + optional bearer |

### 3.3 后端已提供、前端未用（本设计消费）

来自 `run_status` / `build_progress`（已 flatten 到 `GET /api/runs/current` 的 `run`）：

| 字段 | UI 用途 |
|------|---------|
| `alive`, `pid`, `kind`, `meta.tag` / `tag` | 状态 pill + KPI |
| `complete`, `goal_complete`, `baseline_complete`, `remain` | 总进度条 + KPI「全局 complete」 |
| `batch_gained`, `target`/`target_new`, `batch_remain` | 本批进度条 + KPI「本批」 |
| `consecutive_zero`, `sub`, `chunk`, `mode` | KPI / warn pill |
| `phase`, `phase_title`, `phase_detail` | status-card 标题与副文案 |
| `stuck`, `stuck_reason` | danger 态 + 顶栏告警 |
| `steps[]` `{id,title,desc,state}` | **步骤轨** |
| `timeline[]` `{source,phase,title,line}` | **时间线**（最近 N 条） |
| `recent_writes` | 可选小列表「最近落盘」 |
| `supervisor_log`, `worker_log` | 路径 hint（已有） |
| overview.`product_ok`, `nodes.{total,enabled,healthy}` | KPI disk + 节点健康摘要 |
| `summary` (SUMMARY_JSON) | phase 卡副信息：reg_success / mint / fatal_reason |
| `recent_writes` | **必做**「最近落盘」chip 列表（review 从可选升为 P0） |
| `last_lines` / `worker_last_lines` | idle 或无 log-follow 时的兜底快照（P1） |
| `GET /api/runs` → `list_runs` | **运行历史**折叠表（v1 有 runs 面；console 丢） |

### 3.4 功能完备矩阵（review 补全 — 必须做满）

相对 allowlist `EXTRA_ENV_ALLOWLIST` 与 v1/console API，UI **必须暴露**（隐藏=功能不全）：

| 能力 | API / 后端 | console4 | 设计要求 |
|------|------------|----------|----------|
| kind `grok_supervisor` / `register_sh` | `StartRunRequest.kind` | 写死 | **P0** 高级启动 |
| product grok/mimo/chatgpt | `StartRunRequest.product` | 写死 | **P0** register_sh 时 |
| mode ordinary/residential | 已有 | 有 | 保留；register_sh 时 mode 可灰显+说明「supervisor 专用」 |
| target / threads / tag | 已有 | 有 | 保留 |
| SKIP_CLASH_PREFLIGHT | allowlist | 无 | **P0** |
| SUPERVISOR_CHUNK | allowlist | 有 | 保留 |
| CPA_BATCH_END_INJECT | allowlist | 有 | 保留，默认关 |
| CPA_BATCH_IMPORT_EVERY | allowlist | 有 | 保留 |
| CPA_BATCH_IMPORT_SIZE | allowlist | **无** | **P0** 高级启动数字框，默认 100 |
| CPA_BATCH_IMPORT_PAUSE | allowlist | **无** | **P0** 高级启动数字框，默认 3 |
| NODE_SCORE 0/1 | allowlist | 无 | **P0** 高级 select，默认「不传」 |
| CPA_PROBE_CHAT | allowlist | disabled false | 保留强制关 |
| EMAIL_PROVIDER / DEFAULT_DOMAINS via extra_env | allowlist | 仅 config 保存 | **P1** 开始时可选「把当前邮箱表单同步进 extra_env」checkbox（默认开）— 保证 register_sh 不读 config 时仍能带邮箱 |
| 进度 steps/timeline/bars/KPI | progress flatten | 未用 | **P0** |
| recent_writes | progress | 未用 | **P0** |
| stuck / zero 醒目 | progress | pill 弱 | **P0** |
| 运行历史 list_runs | `GET /api/runs` | **无 UI** | **P1** 注册页底或设置旁折叠「最近运行」 |
| overview project_root / nodes | overview | 仅 product_ok | **P0** KPI + 页脚一行 root |
| log which/tail/follow | logs API | 有 | 保留；log 区 `flex:1` 不被进度挤没 |
| stop process_group | stop API | 有 | 保留；结果展示 `source/mode` |
| 账号 soft-delete / 筛选 | accounts API | 有 | 保留 + summary KPI 皮 |
| 邮箱配置 + hotmail import | config+import | 有 | 保留 |
| 节点 Clash+catalog | nodes API | 有 | 保留 |
| 导入四件套 | import API | 有 | 保留；卡片 grid 美化 |
| 设置 secrets 掩码 | config | 有 | 保留 |
| selfcheck / cleanup | ops | 有 | 保留；注册顶栏 + 设置双入口 OK |
| 测代理 | nodes test | 有 | 保留 |
| 登录 / logout / bearer | auth | 有 | 保留 |
| GitHub 仓库链 | 静态 | 有 | 保留 |
| 标题/品牌 | 「Grok 注册机」 | 有 | **P1** 副标改为「AI 注册机 · multi-product」避免 mimo/chatgpt 违和 |

**明确不做（仍非缺口）：** Turnstile 池按钮、SSE、多 host、mid-mint inject 开关、全局 pkill。

---

## 4. Page-by-page layout（骨架不变）

### 4.1 注册页（主战场）

**骨架：** `page-head` toolbar 不变；`split` = 左 `form-panel` + 右 `log-panel`。

#### 4.1.1 右侧：进度栈（注入点）

自上而下固定顺序（替换/增强现有 pills + status-card 区域）：

```
┌─ run-header ─────────────────────────────────────────┐
│ [ALIVE|idle]  tag=…  pid=…  mode=ordinary            │
│ (stuck 时整条红底：stuck_reason)                       │
└──────────────────────────────────────────────────────┘
┌─ kpi-grid (2×3 或 3×2) ──────────────────────────────┐
│ complete/goal   │ 本批 gained/target │ disk product_ok │
│ remain          │ sub · zero         │ nodes healthy*  │
└──────────────────────────────────────────────────────┘
┌─ bars ───────────────────────────────────────────────┐
│ 全局 complete ████████░░░░  630 / 1190  (53%)          │
│ 本批   gained  ██░░░░░░░░  2 / 562                      │
└──────────────────────────────────────────────────────┘
┌─ step-rail (横向可换行) ─────────────────────────────┐
│ ●批次  ●节点  ●子批  ●浏览器  ◉表单  ○盾  ○OTP …     │
│ state: done=绿勾  active=高亮脉冲  pending=灰          │
└──────────────────────────────────────────────────────┘
┌─ status-card ────────────────────────────────────────┐
│ phase_title                                          │
│ phase_detail（单行截断，hover/展开全文）               │
└──────────────────────────────────────────────────────┘
┌─ timeline (可折叠，默认展开最近 8 条) ────────────────┐
│ · supervisor | mint | wrote xai-…                    │
│ · worker | otp | …                                   │
└──────────────────────────────────────────────────────┘
┌─ log-toolbar + #run-log (现有) ──────────────────────┐
```

\* `nodes healthy` 来自 overview，poll 失败时显示 `—`，不阻塞进度渲染。

**pills 去留：** 不再作为主信息源；可保留 1 行极简 chips（ALIVE / stuck / mode）或完全并入 `run-header`。**禁止**再把 complete/goal 只塞进小 pill。

**百分比计算（前端纯函数）：**

```
goalPct   = goal>0 && complete!=null ? clamp(complete/goal*100,0,100) : null
batchPct  = target>0 && gained!=null ? clamp(gained/target*100,0,100) : null
```

`null` 时条显示 indeterminate/空轨 + 文案 `—`，不伪造 0%。

#### 4.1.2 左侧：启动参数恢复

在现有 `reg-form` **底部**（或「数量/线程/模式」区块旁）增加 **「高级启动」`<details>`**，默认折叠：

| 控件 | 绑定 | 说明 |
|------|------|------|
| kind | `select` | `grok_supervisor`（默认）/ `register_sh` |
| product | `select` | grok/mimo/chatgpt；**仅 kind=register_sh 时启用**，supervisor 时锁 grok |
| SKIP_CLASH_PREFLIGHT | checkbox | → `extra_env.SKIP_CLASH_PREFLIGHT=1|0` |
| NODE_SCORE | optional select 0/1 | allowlist 已有；默认不传（吃环境/配置） |

`startRun()` body 改为读这些控件；**默认值与今日生产一致**（supervisor + grok + 不 skip preflight），避免误触。

**高级启动完整字段（§3.4 P0）：**

| 控件 | 默认 | → |
|------|------|---|
| kind | grok_supervisor | body.kind |
| product | grok（supervisor 锁定） | body.product |
| SKIP_CLASH_PREFLIGHT | 关 | extra_env `0`/`1` 仅当勾选传 `1` |
| NODE_SCORE | （不传） | 选 0/1 才写入 extra_env |
| CPA_BATCH_IMPORT_SIZE | 100 | extra_env（仅 batch-end 开时也可始终传） |
| CPA_BATCH_IMPORT_PAUSE | 3 | extra_env |
| 同步邮箱到 extra_env | 开 | `EMAIL_PROVIDER` + `DEFAULT_DOMAINS` 从当前表单 |

现有 batch-end / CHUNK / probe 契约不变：

- 保存配置仍强制 `cpa_remote_inject=false`、`cpa_probe_chat=false`
- `CPA_BATCH_END_INJECT` 只走 `extra_env`，不写 config intent 为 true 当默认
- kind 切换时：supervisor 显示 CHUNK/batch-end/mode；register_sh 隐藏 CHUNK 与 batch-end（无 supervisor 语义），显示 product

#### 4.1.3 顶栏「全局一眼」（v1 Overview 精神）

不强制新 sidebar「Overview」页（避免和注册抢注意力）。在注册 `page-head` hint 行或 KPI 第一格体现：

`product_ok=N · run=ALIVE|idle · complete=C/G · zero=Z`

与 KPI 网格同源数据，避免第三套计数。

**可选（P2）：** 设置页或导入页不显示 live run；若用户强烈需要独立 Overview，再加 sidebar「总览」——本设计 **默认不做**，以减少页数。

#### 4.1.4 右侧布局与空间（好看 + 不挤没 log）

进度栈变高后必须保住日志可读性：

1. `log-panel` 使用 `display:flex; flex-direction:column; min-height:0`；`#run-log` `flex:1; min-height:12rem`。
2. 进度区包在 `#run-progress` 内，**默认可滚动**，`max-height: min(52vh, 28rem)`；避免整页只剩进度、log 缩成一条。
3. **折叠优先级（本地 localStorage）：**
   - timeline 默认展开 6 条（review：8→6 减噪）
   - step-rail 默认展开
   - 「原始 run JSON」默认折叠
   - 窄屏（&lt;1100px）split 改单列：进度在上、表单次之、log 仍可 sticky 底
4. **sticky run-header：** 在 log-panel 内滚动时 ALIVE/stuck 条始终可见。

### 4.2 账号池 / 邮箱 / 节点 / 导入 / 设置

业务逻辑不改；**视觉与信息架构要与注册进度区同一套组件语言**（否则整站仍「一半好看一半简陋」）。

| 页 | 必须（本轮） | 做法 |
|----|--------------|------|
| 账号池 | summary 三数字：complete / incomplete / total | 复用 `.kpi-grid` 单行；表格 zebra + hover |
| 节点池 | Clash summary：healthy / dead / pool size | 同 KPI 皮；健康列色点对齐 step 色 |
| 导入 | 四卡 **2×2 grid**，统一图标点 + 主按钮 primary | 去掉「裸 file input 堆叠」廉价感 |
| 邮箱 | 分区标题「通道 / 密钥 / Hotmail」 | fieldset 视觉分组 |
| 设置 | 分区「网络 / CPA 契约 / 密钥 / 运维」 | 同邮箱；危险项（inject intent）用 warn 边框说明 disk-first |
| 全局 | 空状态插画不要；用简洁 empty：图标+一句+按钮 | — |

### 4.3 不生搬参考图

| 参考图可能有的 | 我们的选择 |
|----------------|------------|
| 大面积插画 / 营销 hero | **不要** |
| 多列复杂 dashboard widget | **只要** KPI + 双 bar + step rail |
| 仿 Grok 官网字体/紫粉渐变 | **沿用 console CSS 变量**（`--bg/--panel/--ok/--danger`） |
| 假进度动画 | **只绑定真实 complete/gained**；无数据不转圈骗进度 |
| 把「协议注册」做成向导多 step 表单 | **保留单页表单**；步骤轨只描述 **运行管线** |

---

## 5. Component specs（前端）

全部 vanilla：`app.js` 渲染函数 + `app.css` class。无新依赖。

### 5.1 `renderKpiGrid(run, overview)`

- 容器 `#run-kpi`（新建）
- 每卡：`label` + `value` + 可选 `sub`
- stuck 时 zero 卡加 `.danger`
- alive 时 complete 卡加 `.ok` 边框（轻量）

### 5.2 `renderBars(run)`

- `#run-bars`
- 两条 `.bar-row`：label、track、fill（width%）、caption `a / b`
- `prefers-reduced-motion` 下取消 fill transition

### 5.3 `renderStepRail(steps)`

- `#run-steps`
- 水平 flex wrap；每步 `.step` + `.done|.active|.pending`
- active 显示 title；pending 可只显示短 title
- 空 `steps` 时隐藏整轨

### 5.4 `renderTimeline(timeline)`

- `#run-timeline`，默认 cap 8；「展开全部」到 24（API 已 limit 24）
- 每行：`source` chip + `title` + 截断 `line`
- 与 log tail **互补**：timeline 过滤后的人话事件，log 是原文

### 5.5 `renderRunStatus` 重写

现有 pills-only 函数改为编排：

```
renderRunHeader → renderKpiGrid → renderBars → renderStepRail
→ status-card 文案 → renderTimeline
```

**Poll 契约保持：** `refreshRegister({reloadForm:false})` 每 4s；**禁止** poll 重载表单（`regFormDirty` 逻辑保留）。

### 5.6 CSS tokens

在现有 console 变量上扩展即可：

```css
--bar-track: …;
--bar-fill: var(--ok or accent);
--step-done / --step-active / --step-pending
```

不新增第二套主题文件。

### 5.7 Visual system（「尽量好看」的可执行规格）

目标：运维工具的 **清晰、沉稳、密度够**，不是营销站。沿用 console 深色 token，做 **层次与节奏**。

#### 5.7.1 层次

| 层 | 用法 |
|----|------|
| L0 背景 | `--bg` |
| L1 面板 | `--panel` 卡片/表单 |
| L2 凹陷 | `--panel-2` / `--bg` 输入、log、bar track |
| 强调 | 1px `--border`；active/ok 用半透明 accent/ok 边，不用粗描边 |

#### 5.7.2 KPI 卡

```
.kpi-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:0.55rem; }
.kpi {
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 0.55rem 0.7rem;
  min-height: 4.25rem;
}
.kpi .label { font-size:0.72rem; color:var(--muted); letter-spacing:0.02em; text-transform:uppercase; }
.kpi .value { font-size:1.35rem; font-weight:700; font-variant-numeric:tabular-nums; margin-top:0.2rem; }
.kpi .sub   { font-size:0.75rem; color:var(--muted); margin-top:0.15rem; }
.kpi.ok     { border-color: rgba(91,212,154,0.35); }
.kpi.danger { border-color: rgba(212,91,91,0.45); background: rgba(212,91,91,0.06); }
.kpi.warn   { border-color: rgba(212,177,91,0.4); }
```

数字用 `tabular-nums`，complete 变化时 **不**弹跳缩放（避免花哨）。

#### 5.7.3 进度条

```
.bar-row { display:grid; grid-template-columns: 4.5rem 1fr auto; gap:0.5rem; align-items:center; }
.bar-track {
  height: 0.55rem; border-radius:999px;
  background: rgba(255,255,255,0.06);
  overflow:hidden;
}
.bar-fill {
  height:100%; border-radius:999px;
  background: linear-gradient(90deg, var(--accent-2), var(--ok));
  transition: width 0.35s ease;
}
.bar-fill.warn  { background: linear-gradient(90deg, var(--warn), #d48a5b); }
.bar-fill.danger{ background: linear-gradient(90deg, #a04040, var(--danger)); }
.bar-caption { font-size:0.78rem; color:var(--muted); font-variant-numeric:tabular-nums; min-width:7rem; text-align:right; }
```

规则：

- 全局条：正常 accent→ok；`remain==0 && alive` 全 ok 绿
- 本批条：同
- `stuck` 时全局条改 `.danger`，本批条 `.warn`
- `pct==null`：track 空 + caption `—`，**不要** 0% 假满

#### 5.7.4 步骤轨

- 每步：圆点 10px + 短标题（中文 2–4 字：批次/节点/子批/浏览器/表单/盾/OTP/Mint/汇总/注入）
- `done`：实心 ok + 连线 ok
- `active`：accent 环 + 极轻 `pulse`（`@media (prefers-reduced-motion: reduce)` 关闭）
- `pending`：空心 muted
- 10 步可 wrap；不横向强制滚动（小屏两行）

#### 5.7.5 run-header / stuck

- 常态：左 status 点（绿/灰）+ `ALIVE|idle` 字重 650 + meta chips
- stuck：整条 `background: rgba(212,91,91,0.12); border-color: rgba(212,91,91,0.5)`，文案 `stuck_reason` 单行省略，title 悬停全文
- zero≥4 但未 stuck 字段：zero KPI `.warn`（与 progress 启发式一致）

#### 5.7.6 timeline / recent_writes

- timeline：左边 2px accent 竖线；行高紧凑 1.35；`source` 用 mini chip
- recent_writes：横向 wrap chips，文件名 mono 0.75rem；最多 5（API 已截）

#### 5.7.7 日志区

- 背景比 panel 更深（已有 `#0a0e14` 感）
- toolbar 与 log 之间 1px border
- follow 开启时右上角小绿点「live」

#### 5.7.8 表单与高级启动

- `<details class="advanced">`：summary 用 muted「高级启动 · kind / product / 批末波次」
- open 时边框 accent 淡
- kind=register_sh 时 product 行高亮提示

#### 5.7.9 动效预算

只允许：

1. bar width 0.35s  
2. active step 轻脉冲  
3. button hover brightness  

禁止：页面进场 cascade、数字 slot machine、骨架屏闪烁超过 1 次。

### 5.8 `renderRecentWrites(writes)`

- `#run-writes`
- 空则隐藏整块
- chip 点击不强制打开文件（无 API）；仅展示路径 basename

### 5.9 `renderRunHistory(runs)`（P1）

- 数据：`GET /api/runs` → `runs[]`
- 注册页最底部折叠「最近运行」：run_id / kind / pid / 时间（若有）
- 无数据隐藏；失败不阻塞主进度

---

## 6. Backend

### 6.1 默认：零 API 变更

`GET /api/runs/current` 与 `GET /api/overview` 字段已够用。

### 6.2 仅当实现中发现缺口再加（非本设计必须）

| 缺口 | 处理 |
|------|------|
| `tag` 不在 flatten 顶层 | 读 `meta.tag` 或 progress 解析；缺则 UI `—` |
| overview 无 goal | 不强制 overview 带 progress；注册页以 runs/current 为准 |
| `register_sh` progress 弱 | step rail 可能多 pending；status-card 仍显示 last lines — 可接受 |

**禁止**为本 UI 改 supervisor 日志格式或 mint 契约。

---

## 7. Product / security invariants（UI 验收必须过）

1. **disk-first：** 文案与默认勾选继续强调「只产落盘」；batch-end inject 默认关。
2. **probe：** supervisor 路径 UI 保持 disabled + 说明强制 false。
3. **start 409：** 锁占用时结果区展示 API detail，不连环重试。
4. **stop：** 仅 API stop；前端不引入「杀全部 chromium」按钮。
5. **auth：** 401 → 登录门；poll 不刷爆登录错误 toast。
6. **secrets：** 设置/邮箱 key 掩码逻辑不变。
7. **plus-alias：** 邮箱页默认不勾选；文案保留「生产勿开」。
8. **healthy-only import：** 导入页 auth 默认 no-remote；不在 UI 推广盲注入。

---

## 8. Implementation plan（实现阶段，本文件只设计）

| Phase | 内容 | 验收 |
|-------|------|------|
| **A** | DOM 槽位 + CSS（kpi/bars/steps/timeline）空壳 | 静态打开无 JS 错误 |
| **B** | `renderRunStatus` 接真实 fields；poll 刷新 | 本地 mock 或 pxed 只读：条与数字随 complete 动 |
| **C** | 高级启动 kind/product/SKIP_CLASH | start body 正确；默认路径与现网一致 |
| **D** | stuck/zero 视觉 + timeline 折叠 | zero≥4 或 stuck 红态可见 |
| **E** | 回归：表单 dirty、probe 强制、batch-end env | 现有 unit 不破；手测注册页 |

顺序建议 **A→B→D→C→E**（先可读性，后启动面）。

**文件预期：**

- 修改：`apps/web/index.html`、`apps/web/assets/app.js`、`apps/web/assets/app.css`
- 可选测试：前端无自动化则用 control_api 既有 pytest；可加轻量 HTML fixture 或纯 JS 百分比函数的 node-less 测（若项目无 runner则手测清单）
- **不改** `progress.py` 除非发现 bug

**部署：** 与此前 control plane 相同 tar+scp pxed；**部署前 pause supervisor 可选**——纯静态 UI 热更通常不需 pause；若同发 API 再 pause。

---

## 9. Acceptance criteria

### 9.1 进度与可读性

1. 注册页右侧能同时看到：**ALIVE、KPI 六格、complete/goal 条、本批 gained/target 条、step rail、phase、recent_writes（有则）、timeline、log tail**。
2. `steps[]` 的 active 步与 `phase` 一致（允许 1 个 poll 延迟）。
3. stuck 或 consecutive_zero≥4 时危险/警告态无需读 log。
4. log 区 `min-height ≥ 12rem`，进度再高也不能把 log 挤没。
5. 无数据时条为 `—`，不显示假 0% 满条。

### 9.2 功能完备（对照 §3.4）

6. 高级启动可发 `register_sh` + mimo/chatgpt；`SKIP_CLASH_PREFLIGHT` / `NODE_SCORE` / `CPA_BATCH_IMPORT_SIZE` / `CPA_BATCH_IMPORT_PAUSE` 进入 allowlist env。
7. 默认开始仍为 `grok_supervisor` + grok + disk-first env；batch-end 默认关。
8. 4s poll **不**重置左侧表单编辑。
9. 账号/邮箱/节点/导入/设置功能回归无回退；导入四卡 2×2、账号/节点 summary 用 KPI 皮。
10. `GET /api/runs` 历史折叠区存在（空则隐藏）。
11. stop 结果展示 `source`/`mode`（若 API 返回）。

### 9.3 观感

12. KPI / bar / step 使用 §5.7 token；无营销 hero、无假进度动画、无第三套主题。
13. 品牌副标体现 multi-product（P1）。
14. 文案不宣称 mid-mint tebi 注入。

---

## 10. Mapping: v1 精神 → console 落点

| v1 精神 | console 落点 |
|---------|--------------|
| Overview 一行 status card | 注册 KPI + run-header |
| Overview JSON | 可选 `<details>`「原始 run JSON」调试用（P2） |
| Runs 表单 kind/product/skip | 注册左栏「高级启动」 |
| Runs log tall | 右栏现有 log（保留 which/tail/follow） |
| Config 页 | 设置 + 注册协议子集（已有） |
| Import 四卡 | 导入页（已有，保留） |

---

## 11. Open questions（需用户拍板则标）

设计默认已选定，无需阻塞文档；若反对再改：

1. **独立 Overview 页？** 默认 **否**（KPI 进注册页）。若要「总览」sidebar，实现阶段加一页即可。
2. **timeline 默认展开？** 默认 **展开 8 条**；若嫌吵可默认折叠。
3. **NODE_SCORE UI？** 默认高级区可选；不传则后端/环境默认。

---

## 12. Out of scope follow-ups（记录，不进本交付）

- flock 被 mihomo 继承导致假锁（运维坑，非 UI）
- SSE 替代 poll
- Turnstile 池接入工具栏按钮
- 多 product complete 分计数（MiMo/ChatGPT sinks）
- register_core 迁移外壳与 UI 文案同步（另一 milestone）

---

## 13. Summary

**做：** 在 console4 侧栏骨架上，把 v1 的「一眼进度」做成 KPI + 双进度条 + 步骤轨 + timeline + recent_writes，恢复 kind/product/SKIP_CLASH/**SIZE/PAUSE/NODE_SCORE** 启动面；运维页统一 KPI/卡片视觉；吃满 `build_progress` + `list_runs`。  
**不做：** 换皮抄图、新框架、改 mint/CPA 契约、删运维页。  
**下一步：** 用户确认 §14 review 修订 → 写 implementation plan → 再动 UI。

---

## 14. Design self-review（功能给全 + UI 好看）

> 对照：v1 `c44ac22`、console4 DOM/JS、`progress.py`、`EXTRA_ENV_ALLOWLIST`、`GET /api/runs`、基线 control-plane spec。  
> 结论：**方向对，v1 稿功能偏瘦、观感规格偏虚**；已在上文 §3.4 / §4.1.x / §5.7–5.9 / §9 改成可实施。下列为 review 明细。

### 14.1 总评

| 维 | 判定 | 说明 |
|----|------|------|
| 骨架取舍 | **通过** | 侧栏六页 + 注册 split 正确；不恢复 top-nav 四页 |
| 进度数据 | **通过** | 后端已够；问题在 UI 未消费 |
| 功能完备 | **原稿不足 → 已补** | allowlist 半数未进 UI；list_runs/recent_writes 漏 |
| 观感可执行性 | **原稿不足 → 已补** | §5.7 给出 KPI/bar/step/动效预算 |
| 产品契约 | **通过** | disk-first / probe / stop 边界清楚 |
| 范围 | **通过** | 无 Node、无抄图、无改 mint |

**推荐：** 按 v2 稿进入 implementation plan；**不要**按未修订的「仅双条+steps」最小集开工。

### 14.2 功能缺口（原稿 → 处置）

| 缺口 | 影响 | 处置 |
|------|------|------|
| `CPA_BATCH_IMPORT_SIZE` / `PAUSE` 无 UI | 批末导入波次无法从控制面调，功能不全 | **P0** 高级启动 |
| `NODE_SCORE` 仅文档 optional | ordinary 生产常用却不可点 | **P0** |
| `recent_writes` 标 optional | 丢掉「刚写出哪些号」的最强反馈 | **P0** |
| `GET /api/runs` 无 UI | v1 Runs 面丢失历史 | **P1** 折叠表 |
| register_sh 不带邮箱 env | 一键 mimo/chatgpt 可能用错邮箱通道 | **P1** 同步 extra_env |
| kind 切换不隐藏 CHUNK/batch-end | 误导：one-shot 无 supervisor 语义 | **P0** 交互 |
| stop `source/mode` 不展示 | 排障弱 | **P1** 结果区 |
| 品牌写死 Grok | multi-product 违和 | **P1** 副标 |
| 运维页「仅小修」 | 进度区变好看、其它页仍土，整体仍差 | **本轮必须** 统一 KPI/导入 grid（§4.2） |
| log 被进度挤没风险 | 运维主工具失效 | **P0** §4.1.4 flex+max-height |
| 双拉 overview+current 每 4s | 可接受；可后续合并 | 记录，不阻塞 |
| `tag` 仅 meta | 显示 `—` 可接受 | 已写 §6.2 |
| mid-mint inject 开关 | 故意不做 | 正确 |

### 14.3 UI / UX 质量意见

**原稿优点**

- 右侧信息栈顺序正确（状态 → 数 → 条 → 步 → 细节 → 事件 → 日志）
- 百分比 null 不装 0% — 诚实
- 明确反抄图、反假进度

**原稿问题 → 已用 §5.7 钉死**

1. 「沿用 CSS 变量」太虚，实现会各写各的丑 → **KPI/bar/step 有 class 级规格**  
2. 10 步横向轨在窄右栏易碎 → **允许 wrap + 短中文标签**  
3. timeline 默认 8 + 全栈展开易压 log → **cap 6 + progress 区 max-height**  
4. stuck 只有「红底」不够 → **header + KPI + bar 三处联动**  
5. 导入四卡竖堆 file input 仍廉价 → **2×2 grid**  
6. 动效未预算 → **只准 bar + pulse + hover**

**观感原则（验收用语）**

- 密度：注册右栏 5 秒内读完「是否 ALIVE / 还差多少 / 卡在哪一步」  
- 克制：无插画、无紫粉营销渐变铺满  
- 一致：数字 `tabular-nums`，危险只用 `--danger` 系  
- 日志优先：进度再炫也不能剥夺 tail

### 14.4 与中间任务 #269 / #271 的关系

历史上 step view 与 KPI 卡任务在 console3 重写中实质丢失。本设计是 **把那两件事 + v1 启动面 + 视觉系统一次做对**，不是第三次换皮。

### 14.5 实现顺序修订（相对 §8）

| Phase | 内容 |
|-------|------|
| **A** | CSS token + DOM 槽位（kpi/bars/steps/writes/timeline/history/advanced） |
| **B** | render 接 `runs/current`+overview；poll；log flex 保底 |
| **C** | stuck/zero/bar 色态 + recent_writes + timeline |
| **D** | 高级启动全字段 + kind 显隐 + start body |
| **E** | 运维页 KPI 皮 + 导入 2×2 + 品牌副标 |
| **F** | 历史 list_runs + 回归手测清单 §9 |

### 14.6 风险

| 风险 | 缓解 |
|------|------|
| 右栏过高 | §4.1.4 max-height + log flex |
| register_sh progress 贫瘠 | step 多 pending 可接受；phase_detail+log 兜底 |
| 高级区误触 skip preflight | 默认关；summary 文案标明风险 |
| 只改注册页导致整站割裂 | §4.2 本轮必须统一皮 |

### 14.7 最终建议

- **Safe to implement after user ack of v2**  
- 功能以 §3.4 矩阵为门禁；观感以 §5.7 为门禁  
- 用户若只要「进度条」最小集，会再次出现「功能丢、其它页仍丑」— **不推荐砍 §3.4/§4.2**
