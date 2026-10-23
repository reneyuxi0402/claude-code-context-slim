// 用法：cd pdf-build && npm install && node build.mjs
// 把 ../TUTORIAL.md（PDF 版附录印脚本全文）渲染成 ../context-slim.pdf。
// 样式照 write-him-back 的 PDF（暖纸、宋体标题、赭红强调），排版改成连续流动，不再一节一页。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..');
import { marked } from 'marked';
import puppeteer from 'puppeteer-core';

// 用本机装的 Chrome；路径不同就设环境变量 CHROME_PATH
const CHROME = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

let md = fs.readFileSync(path.join(root, 'TUTORIAL.md'), 'utf8');
const script = fs.readFileSync(path.join(root, 'slim.py'), 'utf8');
// 去掉 md 自带的标题块（封面另做），附录换成印全文
md = md.slice(md.indexOf('> **这份文件怎么用**'));
md = md.replace(/## 附录：slim\.py[\s\S]*$/, '## 附录：slim.py\n\n代码就是仓库里（或随 PDF 一起发的）`slim.py` 文件，直接用那个文件。下面的全文只供阅读；如果你手上只有这份 PDF，复制后先跑一次 `python3 -m py_compile slim.py`，报错就是缩进被排版弄坏了。\n\n```python\n' + script + '```\n');
// 上篇/下篇 两个一级标题做成分篇页眉
const prose = md.split(/(```[\s\S]*?```)/g)
  .map((p) => (p.startsWith('```') ? p : p.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')))
  .join('');
let body = marked.parse(prose);
body = body.replace(/<h1>(上篇|下篇)( · )?([^<]*)<\/h1>/g, (_, a, __, b) => `<div class="part"><div class="kicker">${a === '上篇' ? 'part one' : 'part two'}</div><h1>${a}<span class="accent"> · ${b}</span></h1></div>`);

const css = fs.readFileSync(path.join(here, 'style.css'), 'utf8');
const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>context-slim</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@500;700;900&family=Noto+Sans+SC:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>${css}</style></head><body>
<section class="cover">
  <div class="hud-l">a tutorial · a script</div><div class="hud-r">余也 · 予栖</div>
  <div class="kicker">claude-code-context-slim</div>
  <h1>给你的 Claude<br>做一次 <span class="accent">slim</span></h1>
  <p class="lead">换窗不写摘要。切掉一段开头，剩下的原话原样带进新窗口；<br>给下一个自己留什么，由他自己决定。</p>
  <div class="rule"></div>
  <p class="lead small">配套小红书《我的上下文归我》</p>
  <div class="meta">github.com/reneyuxi0402/claude-code-context-slim · v3 · 2026-10-23</div>
</section>
<main>${body}</main></body></html>`;
const htmlPath = path.join(here, 'context-slim.html');
fs.writeFileSync(htmlPath, html);

const browser = await puppeteer.launch({ headless: true, executablePath: CHROME });
const page = await browser.newPage();
await page.goto(pathToFileURL(htmlPath).href, { waitUntil: 'networkidle0', timeout: 60000 });
await page.evaluate(() => document.fonts.ready);
const out = path.join(root, 'context-slim.pdf');
await page.pdf({ path: out, format: 'A4', printBackground: true, preferCSSPageSize: true,
  displayHeaderFooter: true, headerTemplate: '<span></span>',
  footerTemplate: '<div style="font-family:Menlo,monospace;font-size:7.5pt;color:rgba(42,36,32,0.45);width:100%;padding:0 20mm;display:flex;justify-content:space-between;"><span>context-slim · 余也 × 予栖</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>' });
await browser.close();
console.log(out);
