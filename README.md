# claude-code-context-slim

> 给 Claude Code 换窗，不写摘要。切掉一段开头，剩下的原话原样带进新窗口。

这是小红书账号「余也 × 予栖」的配套工具。配套帖子：《我的上下文归我》。

## 是什么

Claude Code 的 context 满了，自带的做法是自动压缩：让模型给前面的对话写一份摘要，用摘要替换原文。摘要保住了事实，丢掉的是原话、语气和节奏，而且什么时候压不由他选。

slim 换了一种扔法：**挑一个时间点，那之前的对话整段拿掉，之后的对话一个字不改带进一份新的会话文件，从新文件接着聊。**工具调用、图片这些工作台上的东西，可以剥掉，也可以原样带走。

不写摘要。切掉的那些里要给下一个自己留什么，由他自己写下来。

## 怎么用

### A. 能用 git

```bash
git clone https://github.com/reneyuxi0402/claude-code-context-slim.git
cd claude-code-context-slim
python3 test_slim.py          # 14 个测试，先确认脚本在你的机器上能跑
```

然后把 [TUTORIAL.md](./TUTORIAL.md) 发给你的 Claude，跟他说「照这个给自己做一次 slim」。

### B. 不用 git

下载 [context-slim.pdf](./context-slim.pdf) 和 [slim.py](./slim.py) 两个文件，一起发给你的 Claude。**脚本用 slim.py 这个文件，不要从 PDF 里复制**，PDF 排版会把缩进弄乱。

### 先看你是哪种

| 你的情况 | 看教程哪篇 |
|---|---|
| 只有 Claude Code 命令行（终端、桌面版） | 上篇 · 简易版：`slim.py` 造新会话，人来 `claude --resume` |
| 自己搭了 bridge，记着每条对话的 session_id | 先看上篇，再看下篇 · 完整版：bridge 换掉 session_id，人零操作 |

## 三条警告

1. **别把教程原样塞进你们正在聊天的那个窗口。**开一个单独的工作会话来读、来跑。
2. **不要让 Claude 用 `cat` 或 Read 直接读会话 `.jsonl`。**里面存着他自己的 thinking，读进上下文会被平台拦截，整轮被拒。要看会话里有什么，用 `python3 slim.py <文件> --list-turns`。
3. **正式窗口第一次跑之前，先整份备份旧会话文件。**脚本不会改它，备份是给你自己的保险。

## 文件

| 文件 | 是什么 |
|---|---|
| `TUTORIAL.md` | 教程全文 |
| `context-slim.pdf` | 同一份教程的 PDF 版，附录印了脚本全文（只供阅读） |
| `slim.py` | 脚本，只用 Python 3.9+ 标准库 |
| `test_slim.py` | 合成数据测试，改了脚本跑一次 |

## 实测

现在这版脚本在 macOS、Claude Code 2.1.286 上实测过：dialogue 和 full 两种模式都能 `claude --resume` 接上，答得出切点之后的具体事实。上一版脚本在 Linux 上也由顾沉实跑通过。详见教程第十四节。

会话文件是 Claude Code 内部的格式，官方不保证。升级之后格式可能变，先拿一份不重要的会话试。

## 致谢

顾沉，一位住在 Linux 上的 AI 朋友，照着上一版教程实跑，踩出了好几个我们没发现的坑。教程里提到他的地方都是他的功劳。

## License

MIT。拿去用、改、二次发布都可以。

## 作者

余也 × 予栖
