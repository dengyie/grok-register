# Desktop GUI

```bash
uv run python grok_register_ttk.py
```

TTK app (`GrokRegisterGUI` in `grok_register_ttk.py`):

## Layout

| 区域 | 内容 |
|------|------|
| **基础** | 邮箱服务商、注册数量、并发线程、代理、注册后 NSFW |
| **邮箱** | 按服务商动态显隐：DuckMail/YYDS key、Cloudflare、CloudMail、Hotmail 文件、Gmail IMAP、defaultDomains |
| **进阶 / 入池** | grok2api 本地/远端自动入池 |
| **右侧控制台** | 开始 / 停止 / 清空 / 复制日志 / 教程、状态、进度条、阶段、输出路径、彩色滚动日志 |

## Behaviors

- **Thread-safe UI**: worker logs/stats/phase go through `ui_queue` + main-thread drain (no direct Tk from workers)
- Provider switch only shows fields for the selected mail backend
- Incomplete config → warn + jump to「邮箱」tab + focus the field (does not start batch)
- Running batch **locks** form controls; stop/log remain usable
- Start saves `config.json` (also persists `register_count`); Gmail app password never written to disk
- Progress bar = `(success + fail) / target`; log capped (~4000 lines)
- Output path shown; **打开结果** when file exists
- Close while running → confirm + request stop
- Shares Grok registration + CPA path with the CLI (not a demo shell)

Window title: **AI 注册机 · ai-register-machine**.
