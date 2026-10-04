# prdigest

把 GitHub PR 翻译成大白话：PR 链接丢进来，**一句话总结 / 主要变更 / 潜在风险 / 值得问作者的问题**直接出来。

一个轻量的代码评审小助手：只拉公开 PR 的元数据 + diff（公开仓库不需要 GitHub token），
配任意 OpenAI 兼容的 `/chat/completions` 接口就能用。Python 标准库，零 pip 依赖。

## 快速开始

```bash
# 需要一个 OpenAI 兼容接口的 key（只走你自己的模型，不经过任何第三方）
export OPENAI_API_KEY=sk-...

# 解读一个 PR
python -m prdigest encode/httpx/3764

# 或者给完整 URL
python -m prdigest https://github.com/encode/httpx/pull/3764

# 先看会发什么给模型（不花钱）
python -m prdigest --dry-run encode/httpx/3764
```

## 用法

```bash
python -m prdigest [--dry-run] [--json] [--lang en] [--model ...] [--base-url ...] [--api-key ...] [--fixture ...] owner/repo/123
```

| 参数 | 说明 |
|---|---|
| `--dry-run` | 只拉取 PR 并打印 prompt，不调用模型 |
| `--json` | 输出 JSON（元数据 + 解读全文），方便接脚本 |
| `--lang en` | 英文输出（默认中文） |
| `--model` / `--base-url` / `--api-key` | 覆盖 `OPENAI_MODEL` / `OPENAI_BASE_URL` / `OPENAI_API_KEY` |
| `--fixture` | 用本地 fixture JSON 代替拉取 GitHub（离线/测试用，见 `examples/`） |

环境变量：

- `OPENAI_API_KEY` —— 必需（或 `--api-key`）
- `OPENAI_BASE_URL` —— 默认 `https://api.openai.com/v1`，第三方兼容接口改这里
- `OPENAI_MODEL` —— 默认 `gpt-4o-mini`
- `GITHUB_TOKEN` —— 可选；公开 PR 不需要，设置后可提高 GitHub API 限额。**token 永不打印到任何输出。**

## diff 截断策略（诚实版）

PR 太大时 diff 会被截掉，规则是死的、会明确告诉你：

- 每个文件最多保留 **6000** 字符，超出部分截断并在 prompt 里标注 `[已截断]`
- 全部 diff 总上限 **30000** 字符
- 生成类文件（`package-lock.json`、`*.min.js`、`dist/` 等）的 diff 直接跳过，只记文件名
- `--dry-run` 会列出 `truncated_files` / `skipped_files`，一眼看清哪些没进模型

## 输出示例

```
一句话总结：这个 PR 让 httpx 在 URL path 中对 | 做百分号编码。

主要变更：
- httpx/_urlparse.py：pchar 字符集加入 |
- tests/models/test_url.py：新增覆盖用例
…

潜在风险：
- 【推测】某些服务端可能依赖未编码的 |，升级后行为变化
…

值得问作者的问题：
1. query 部分是否也需要同样处理？
…
```

## 诚实说明

- **解读质量完全取决于你用的模型**：小模型可能漏掉跨文件逻辑、编造不存在的调用；风险部分里凡是推测都会标 `【推测】`，但模型也可能漏标——关键结论请自己点开 diff 核对。
- 大 PR 的 diff 会被截断（见上），模型没看到的部分它不知道，别当全量评审用。
- 只读公开 PR；私有仓库需要 `GITHUB_TOKEN` 且 token 要有对应权限。

## License

MIT — 见 [LICENSE](LICENSE)。
