# 设计：IP 隔离层 + IP 质量信号采集

> 范围说清楚：本轮**只把数据底座和隔离结构抛好**，不动 rotation 的 weight 决策消费。
> 下一轮（新 milestone）才把质量信号灌进 rotation 公式。这样符合"先把所有东西抛好再说"。
> 待落地的设计稿；尚未走 writing-plans 出实施计划。

## 起因与现状基线（已核实，非臆测）

我们要接 5 个高质量**住宅代理(residential)** IP，它是目前能解 ChatGPT `registration_disallowed` 的唯一外部路径；现有"普通 IP"是 Clash leaf / 机房代理。三 provider（grok/mimo/chatgpt）共用，加权轮询，住宅优先、普通兜底混入。

核实结论：
- `register_core/nodes/models.py:10-28` `Node` 是唯一节点载体，字段**无 tier / priority / source_pool / quality**；只有 `last_ok / fail_count / cooldown_until`（被动冷却），无成功计数。
- `register_core/util/proxy.py:703` `report_attempt_proxy_result` 已是**质量信号天然入库点**：按 proxy url(`mgr.find_by_url`)找 node、按 kind 分流、`save_nodes` 持久化。现做：成功清 fail_count；`registration_disallowed` 风险冷却 600s；network fail mark+冷却；其他 kind 不动 node。
- `register_core/nodes/catalog.py:76` `save_nodes` 已落盘；import CLI 默认 persist(`--no-save` 关) → 质量信号不会随进程结束丢失。
- `register_core/util/proxy.py:359` `inject_attempt_proxy` 按 pool 原序轮询，**无主动优先级排序/打分**——所有 IP 同权重 + 被动 burn/cool。
- 全树**无任何主动 IP 质量打分/优先级排序/主动淘汰**逻辑。`registration_disallowed` 只软冷却、不计 disallow 次。

## P0/P1 范围（本轮）

P0（阻塞接 IP）：节点隔离层
1. `Node` 加 `tier: int = 0` 字段（0=机房普通默认，向后兼容现有 nodes.json；1=住宅高质量）。导入/profile/单条 JSON 均可标 tier。
2. 住宅池与普通池"概念隔离"通过 `tier` 字段表达，不做独立文件/pool registry（避免 C 方案超范围）。

P0（阻塞质量底座）：质量信号采集
3. `Node` 加 `success_count: int = 0`、`attempt_count: int = 0`、`disallow_count: int = 0` 三个计数控；加**派生** `quality_score()`（`success_count / max(1, attempt_count) - λ * disallow_count`，λ 默认 0.5，环境变量可调，本轮不参与决策只展示/落盘）。
4. 在 `report_attempt_proxy_result` 各回写分支里加计数：
   - 进入函数、找到 node 后（proxy 非空且 node 在 catalog）：`node.attempt_count += 1`。
   - success 分支：`node.success_count += 1`（在现有清 fail_count 处并加）。
   - `registration_disallowed` 分支：`node.disallow_count += 1`（在现有 600s 风险冷却处并加）。
   - network_fail / 其他：不另加 disallow（attempt_count 已 +1）。
   - 持久化复用现有各分支结尾的 `save_nodes`，**不新增 I/O / hook**。

P1（固化）：测试 + 导入透传
5. import 解析路径透传 tier（除非单条 JSON/profile 已带，否则默认 0）；uri_list `--tier N` 整批标注；`nodes add --tier N` 单条标。**新增 `host:port:user:pass` 四段冒号串 → `socks5://user:pass@host:port` 的归一化**（在 `node_from_dict` 字符串分支）。新增 import border 测试：带 tier 的 JSON/uri_list 落到 Node.tier；`hpup` 串归一化为 socks5 URL 后建 Node 不丢 tier。
6. 回归测试：模拟 success / disallow / network-fail 各分支，断言三个计数正确、quality_score 派生正确、节点持久化字段对（attempt/success/disallow 落盘，tier 不丢）。

## 不做的 P2/P3 / Manual-required

- **不碰 rotation weight 公式**（层2决策消费）——下一 milestone。本轮 tier 加进去但 `inject_attempt_proxy` 仍按原序轮询，tier 字段"在场不参与"。这点要在测试里显式断言：tier 不影响轮询顺序（防现轮引入未验证的优先级行为）。
- 不做"主动按 score 淘汰/降权"（score 只采集+展示）。
- 不为 tier 加新的 egress backend / new preflight；preflight 复用现有 `preflight_nodes_for_register`，不分 tier 探活（健康与否与 tier 正交）。
- 不写独立 Pool registry / 分池文件（C 方案，超本轮）。
- 不引入归因里"这个 IP 被禁 vs 这个 IP 质量差"的新二分——`registration_disallowed` 的 disallow_count 既是被禁信号也是质量负信号，本轮合并记、下一轮再细化区分。
- Manual-required：5 个住宅凭证导入后，真机 `./register.sh chatgpt 1` 用住宅池跑，确认 `registration_disallowed` 出现率下降（本轮 shell 改完即 Manual 触发，非阻塞）。**1024proxy 账号 `cchv57025` 的流量配额 / 订阅有效期是外部约束**，代码无法判断；配额耗尽需用户在后台续费/换账号后重新导入新凭证组，本轮不自动处理。

## 关键设计决策

1. **回写键是 Node id（= proxy url），不是 last_ip**。last_ip 会变（同一住宅代理首次出站的真实 IP 较稳但代理端可能轮转出口），proxy url 才稳定。沿 `mgr.find_by_url(proxy)` 现有检索，不新开 IPIP 路径。`last_ip` 仅诊断展示。

2. **tier 是隔离标签 + 质量档位双语义**：隔离（pool 边界由 tier 划分）、质量档位（下一轮 weight 公式会读）。本轮只落地前者语义、后者"在场不消费"。

3. **质量信号采集改在现成 hook，不引新通路**。`pipeline.py:459` `_feedback_proxy → report_attempt_proxy_result` 已被每个 attempt 结果调用（含成功路径），是唯一回写点。本轮只是"在那里多记三笔"，不动调用方。

4. **tier 的 import 标注入口**：profile.yaml 节点段可 `tier: 1` 单条标；uri_list 导入加 `--tier N` 整批标；单条 JSON 节点 `{"url":..., "tier":1}`。三入口都解析，默认 0。5 个住宅 IP 到手时，用其中一种导入即可隔离标好（你给我 IP 时告诉我格式，我配对应入口）。

5. **加权轮询的"消费"留下一轮**：本轮 `inject_attempt_proxy` 不读 tier/score。下一 milestone 才把它改成"按 tier+score 算 weight、住宅 weight 高、池内轮询、高 tier 优先命中"。这样本轮纯采集、零 risk 改 rotation、零 risk 烧住宅池。

## 住宅代理凭证特性（1024proxy，region-Rand + sticky sid，转运导入影响）

用户提供的 5 个凭证格式 `hostname:port:username:password`：
```
us.1024proxy.io:3000:cchv57025-region-Rand-sid-<rand>-t-5:tyfnvdhr
```
- 它们**不是 5 个固定出口 IP**，是 **5 个住宅代理凭证**。username 内含粘性会话句柄：
  - `region-Rand` = 出口地区随机（每次会话轮换地区）。
  - `sid-<token>-t-5` = sticky 会话 5 分钟；5 min 后 sid 释出、下一次该凭证出口换新 IP。
- 账号 `cchv57025` 与密码 `tyfnvdhr` 是 1024proxy 订阅凭证，**有效期取决于该账号流量配额**（用户在 1024proxy 后台可知，代码无法判断）；sid token 本身不"过期失效"，只 5 min 换 IP。**不需每 5 min 重新生成**；账号配额耗尽时需后台续费/换账号 → 届时再导新凭证组即可。
- 标准化形态：`socks5://<username>:<password>@us.1024proxy.io:3000`。

**对设计的两点深化**（不是问题，是 advantage）：

6. **凭证粒度质量评估更贴合 region-Rand**：因出口 IP 每 5 min 随机漂移，**按出口 IP 记质量会错乱**；按凭证(url)记则跨多次出口累积成功率——正好契合决策 1"回写键是 Node id (=url)"。`quality_score` 在凭证粒度上语义 = "这 5 个住宅凭证整体住宅路径的平均成功率"，而非"单个出口 IP 质量"。这是住宅 region-Rand 天然决定的，不是我们选的，但与现有按 url 回写设计天然吻合。
7. **import border 必须做 `hpup` → socks5 URL 归一化**：现有 `node_from_dict`（`models.py:86`）的字符串分支把整行原样当 `url`，**不识别 `host:port:user:pass` 四段串**，下游用 url 当 proxy 时会解析失败。本轮 import border 必须加归一化：识别"恰好四段冒号、第三段含 `region-`/`sid-`/credentials 特征、本机可走 HTTP/SOCKS"的串，转成 `socks5://user:pass@host:port`。

## 导入这 5 个住宅凭证的具体方式（本轮实现后）

最干净:**单文件 5 行 JSON**，每行带 `tier:1`，归一化由 border 自动转 socks5 URL：
```json
[{"url":"us.1024proxy.io:3000:cchv57025-region-Rand-sid-pA5pnsjK-t-5:tyfnvdhr","tier":1},
 ... 共 5 行 ...]
```
import 文件后 catalog 落 5 个 Node，`tier=1`、`url` 已归一化为 `socks5://...`。

import CLI 入口需补：`node_from_dict` 字符串分支判定四段冒号 + `cmd_add`/`--tier` 选项透传。**此入口完善属本轮 P1**（见 P1 范围第 5 项的"导入透传 tier"）。

## 验证（端到端）


1. 构建/类型：`python -m compileall register_core/nodes register_core/util/proxy.py`。
2. 单元/集成：`python -m pytest -q` 全绿；新增 6~7 个测试：
   - import border：tier 从 JSON/uri_list 落到 Node。
   - report success：attempt+1、success+1、fail_count 清零、save_nodes 调用。
   - report disallow：attempt+1、disallow+1、600s 风险冷却不变、持久化。
   - report network fail：attempt+1、不增 disallow、mark_result 不变。
   - report non-proxy kind：attempt+1（仅）、其他不变。
   - quality_score 派生：构造计数 → 断言公式。
   - tier 不影响轮询顺序（固化"在场不消费"）。
3. 手动（Manual-required，非阻塞）：5 个住宅 IP 到手后导入（带 tier=1）、`./register.sh chatgpt 1` 跑，看日质量计数随结果增长、`registration_disallowed` 率下降。

## 回滚

- `Node` 新增字段均为 `= 0` / `= ""` 默认值，向后兼容现有 nodes.json（旧文件无这些键加载后取默认）。回滚即把字段与各 `+= 1` 三行删掉，不动 rotation 因为本轮没改它。
- import 的 `--tier` 与单条 JSON 的 `tier` 为可选键；解析端默认 0，旧导入不受影响。

## 后续（下一 milestone，不在本轮）

- 层2决策消费：`inject_attempt_proxy` 读 tier+score 算 weight，住宅高权、加权轮询、score 差的 IP 降权/踢出。
- epoch 持久化 + 历史 window quality（看 N 次注册成功率，不是全期）。
- registration_disallowed 的 disallow 与"质量差"细区分（让 `registration_disallowed` 不烧号但降 tier 权重）。
