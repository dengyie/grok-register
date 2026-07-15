# Desktop GUI

```bash
uv run python grok_register_ttk.py
# or
./register.sh grok --gui   # if hub supports gui flag; otherwise direct python
```

TTK app (`GrokRegisterGUI` in `grok_register_ttk.py`):

## Layout

| 区域 | 内容 |
|------|------|
| **基础** | 邮箱服务商、注册数量、并发线程、代理、注册后 NSFW |
| **邮箱** | 按服务商动态显隐：DuckMail/YYDS key、Cloudflare、CloudMail、Hotmail 文件、Gmail IMAP、defaultDomains |
| **进阶 / 入池** | grok2api 本地/远端自动入池 |
| **右侧控制台** | 开始 / 停止 / 清空 / 教程、状态、进度条、统计、彩色滚动日志 |

## Behaviors

- Provider switch only shows fields for the selected mail backend
- Start saves `config.json` (Gmail app password is never written to disk; prefer `GMAIL_IMAP_PASSWORD`)
- Progress bar = `(success + fail) / target`
- Log tags: success / error / warn / info
- Shares Grok registration + CPA path with the CLI (not a demo shell)

Window title: **AI 注册机 · ai-register-machine**.
