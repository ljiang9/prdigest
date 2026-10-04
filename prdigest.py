#!/usr/bin/env python3
"""prdigest — 用一句话把 GitHub PR 讲明白。

给一个公开仓库的 PR（owner/repo/123 或完整 URL），拉取它的元数据和 diff，
丢给 OpenAI 兼容的 /chat/completions，输出：
  一句话总结 / 主要变更 / 潜在风险 / 值得问作者的问题

只用 Python 标准库。公开仓库不需要 GitHub token；
设置 GITHUB_TOKEN 环境变量可以提高 API 限额（token 永不打印到输出）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

__version__ = "0.1.0"

GITHUB_API = "https://api.github.com"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

# 截断策略
PER_FILE_MAX_CHARS = 6000    # 每个文件最多保留的 diff 字符数
TOTAL_MAX_CHARS = 30000      # 全部 diff 总上限

# 生成类文件：不看 diff，只在报告里记一笔
_GENERATED_SUFFIXES = (
    ".min.js", ".min.css", ".bundle.js", ".bundle.css", ".map",
    ".snap", ".lock", "-lock.json",
)
_GENERATED_BASENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Pipfile.lock", "Gemfile.lock", "Cargo.lock", "go.sum",
}
_GENERATED_DIRS = ("dist/", "build/", "vendor/", "node_modules/", ".next/")


# ---------------------------------------------------------------- 文案（中英）

_STR = {
    "zh": {
        "bad_ref": "error: PR 引用格式不对，应为 owner/repo/123 或 https://github.com/owner/repo/pull/123",
        "not_found": "error: 找不到 PR {ref}（仓库或 PR 编号不存在）",
        "forbidden": "error: GitHub API 拒绝访问（可能是限流）。设置 GITHUB_TOKEN 环境变量可提高限额。",
        "bad_token": "error: GITHUB_TOKEN 无效或已过期（401）。",
        "net": "error: 请求 GitHub 失败：{msg}",
        "no_key": "error: 未找到 API key。请设置 OPENAI_API_KEY 环境变量（或 --api-key）。",
        "llm": "error: 模型调用失败：{msg}",
        "no_ref": "error: 请提供 PR 引用，如 prdigest encode/httpx/3764",
        "dry_title": "PR #{num}：{title}",
        "truncated_note": "[已截断：该文件 diff 过长，仅保留前 {n} 字符]",
        "skipped_note": "[生成类文件，已跳过 diff]",
        "total_truncated": "[整体 diff 超出上限，已截断]",
        "no_body": "（PR 无描述）",
    },
    "en": {
        "bad_ref": "error: bad PR reference, expected owner/repo/123 or https://github.com/owner/repo/pull/123",
        "not_found": "error: PR not found: {ref} (repo or PR number does not exist)",
        "forbidden": "error: GitHub API denied access (likely rate limited). Set GITHUB_TOKEN to raise the limit.",
        "bad_token": "error: GITHUB_TOKEN is invalid or expired (401).",
        "net": "error: GitHub request failed: {msg}",
        "no_key": "error: no API key found. Set OPENAI_API_KEY (or --api-key).",
        "llm": "error: model call failed: {msg}",
        "no_ref": "error: please provide a PR reference, e.g. prdigest encode/httpx/3764",
        "dry_title": "PR #{num}: {title}",
        "truncated_note": "[truncated: file diff too long, kept first {n} chars]",
        "skipped_note": "[generated file, diff skipped]",
        "total_truncated": "[total diff exceeded the cap, truncated]",
        "no_body": "(no PR description)",
    },
}


def T(lang, key, **kw):
    return _STR[lang][key].format(**kw)


# ---------------------------------------------------------------- PR 引用解析

_REF_RE = re.compile(r"^([\w.\-]+)/([\w.\-]+)/(\d+)$")
_URL_RE = re.compile(
    r"^https?://github\.com/([\w.\-]+)/([\w.\-]+)/pulls?/(\d+)(?:[/?#].*)?$"
)


def parse_ref(ref):
    """返回 (owner, repo, number)，格式不对抛 ValueError。"""
    m = _REF_RE.match(ref.strip()) or _URL_RE.match(ref.strip())
    if not m:
        raise ValueError("bad ref")
    return m.group(1), m.group(2), int(m.group(3))


# ---------------------------------------------------------------- GitHub 拉取

def _gh_request(url, accept):
    headers = {"Accept": accept, "User-Agent": "prdigest/0.1.0"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    return urllib.request.Request(url, headers=headers)


def _gh_fetch(url, accept, lang, ref_label):
    try:
        with urllib.request.urlopen(_gh_request(url, accept), timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(T(lang, "not_found", ref=ref_label), file=sys.stderr)
        elif e.code == 401:
            print(T(lang, "bad_token"), file=sys.stderr)
        elif e.code == 403:
            print(T(lang, "forbidden"), file=sys.stderr)
        else:
            print(T(lang, "net", msg="HTTP %d" % e.code), file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(T(lang, "net", msg=str(e.reason)), file=sys.stderr)
        sys.exit(1)


def fetch_pr(owner, repo, num, lang):
    """返回 (pr 元数据 dict, diff 文本)。"""
    base = "%s/repos/%s/%s/pulls/%d" % (GITHUB_API, owner, repo, num)
    label = "%s/%s/%d" % (owner, repo, num)
    meta_raw = _gh_fetch(base, "application/vnd.github+json", lang, label)
    meta = json.loads(meta_raw.decode("utf-8"))
    diff_raw = _gh_fetch(base, "application/vnd.github.diff", lang, label)
    return meta, diff_raw.decode("utf-8", "replace")


def load_fixture(path, lang):
    try:
        with open(path, encoding="utf-8") as f:
            fx = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(T(lang, "net", msg="fixture 读取失败：%s" % e), file=sys.stderr)
        sys.exit(1)
    pr = dict(fx["pr"])
    pr.setdefault("user", {"login": fx.get("pr_author", "unknown")})
    pr.setdefault("head", {"ref": fx.get("pr_head", "?")})
    pr.setdefault("base", {"ref": fx.get("pr_base", "?")})
    return pr, fx["diff"]


# ---------------------------------------------------------------- diff 切分与截断

_DIFF_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.M)


def _is_generated(path):
    p = path.lower()
    if os.path.basename(p) in _GENERATED_BASENAMES:
        return True
    if p.endswith(_GENERATED_SUFFIXES):
        return True
    return any(p.startswith(d) for d in _GENERATED_DIRS)


def split_diff(diff):
    """把整块 unified diff 按文件切开，返回 [(路径, 该文件 diff)]。"""
    matches = list(_DIFF_FILE_RE.finditer(diff))
    out = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
        path = m.group(2)  # b/ 侧的名字（重命名取新名）
        out.append((path, diff[start:end]))
    return out


def smart_truncate(files, lang):
    """返回 (拼好的 diff 文本, 被截断的文件, 被跳过的文件)。"""
    kept, truncated, skipped = [], [], []
    total = 0
    for path, chunk in files:
        if _is_generated(path):
            skipped.append(path)
            continue
        note = ""
        if len(chunk) > PER_FILE_MAX_CHARS:
            chunk = chunk[:PER_FILE_MAX_CHARS]
            truncated.append(path)
            note = T(lang, "truncated_note", n=PER_FILE_MAX_CHARS) + "\n"
        kept.append("--- 文件：%s ---\n%s%s" % (path, note, chunk))
        total += len(chunk)
        if total >= TOTAL_MAX_CHARS:
            kept.append(T(lang, "total_truncated"))
            break
    text = "\n\n".join(kept)
    if skipped:
        text += "\n\n" + "\n".join(
            "--- 文件：%s ---\n%s" % (p, T(lang, "skipped_note")) for p in skipped)
    return text, truncated, skipped


# ---------------------------------------------------------------- prompt 构建

def build_prompt(pr, diff_text, truncated, skipped, lang):
    if lang == "en":
        head = (
            "You are a senior code reviewer. Read the GitHub PR metadata and diff "
            "below and explain it in English.\n\n"
            "PR: %s/%s#%s -- %s\n"
            "Author: %s | State: %s | Branch: %s -> %s\n"
            "Stats: %s files, +%s -%s\n"
            "Description:\n%s\n"
        ) % (pr["_owner"], pr["_repo"], pr["number"], pr["title"],
             pr["user"]["login"], pr["state"],
             pr["head"]["ref"], pr["base"]["ref"],
             pr["changed_files"], pr["additions"], pr["deletions"],
             pr.get("body") or _STR["en"]["no_body"])
        if truncated:
            head += "\nNote: diff truncated for: %s\n" % ", ".join(truncated)
        if skipped:
            head += "Note: diff skipped for generated files: %s\n" % ", ".join(skipped)
        return (
            head + "\nDiff:\n%s\n\n" % diff_text +
            "Output exactly these four sections:\n"
            "1. Summary: one sentence on what this PR does.\n"
            "2. Main changes: bullets grouped by area/module.\n"
            "3. Potential risks: bullets; prefix anything inferred (not directly "
            "shown in the diff) with [speculation].\n"
            "4. Questions for the author: 3-5 concrete review questions."
        )
    head = (
        "你是资深的代码评审助手。请阅读下面的 GitHub PR 元数据和 diff，"
        "用中文输出一份 PR 解读。\n\n"
        "PR：%s/%s#%s —— %s\n"
        "作者：%s｜状态：%s｜分支：%s → %s\n"
        "变更统计：%s 个文件，+%s -%s\n"
        "PR 描述：\n%s\n"
    ) % (pr["_owner"], pr["_repo"], pr["number"], pr["title"],
         pr["user"]["login"], pr["state"],
         pr["head"]["ref"], pr["base"]["ref"],
         pr["changed_files"], pr["additions"], pr["deletions"],
         pr.get("body") or _STR["zh"]["no_body"])
    if truncated:
        head += "\n注意：以下文件的 diff 过长被截断：%s\n" % ", ".join(truncated)
    if skipped:
        head += "注意：生成类文件的 diff 已跳过：%s\n" % ", ".join(skipped)
    return (
        head + "\nDiff：\n%s\n\n" % diff_text +
        "请严格按以下四个部分输出：\n"
        "1. 一句话总结：用一句话说清这个 PR 做了什么。\n"
        "2. 主要变更：按模块/区域分组，分条列出。\n"
        "3. 潜在风险：分条列出；凡是根据代码推测、而非 diff 直接显示的内容，"
        "请在该条开头标注【推测】。\n"
        "4. 值得问作者的问题：3-5 个具体的 review 问题。"
    )


# ---------------------------------------------------------------- 模型调用

def call_llm(prompt, lang, api_key, base_url, model):
    payload = {
        "model": model,
        "temperature": 0.3,
        "messages": [
            {"role": "system",
             "content": "You are a senior code reviewer. "
                        "Answer in Chinese unless asked otherwise."
                        if lang == "zh" else
                        "You are a senior code reviewer. Answer in English."},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        print(T(lang, "llm", msg="HTTP %d %s" % (e.code, detail)), file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(T(lang, "llm", msg=str(e.reason)), file=sys.stderr)
        sys.exit(1)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print(T(lang, "llm", msg="返回结构异常"), file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(
        prog="prdigest",
        description="把 GitHub PR 翻译成大白话：总结 / 变更 / 风险 / 提问")
    p.add_argument("ref", nargs="?",
                   help="PR 引用：owner/repo/123 或完整 PR URL")
    p.add_argument("--dry-run", action="store_true",
                   help="只拉取 PR 并展示 prompt，不调模型")
    p.add_argument("--json", action="store_true",
                   help="以 JSON 输出（元数据 + 解读全文）")
    p.add_argument("--lang", default="zh", choices=["zh", "en"],
                   help="输出语言（默认 zh）")
    p.add_argument("--model", default=None, help="模型名（默认 gpt-4o-mini）")
    p.add_argument("--base-url", default=None, help="API base URL")
    p.add_argument("--api-key", default=None, help="API key（默认读 OPENAI_API_KEY）")
    p.add_argument("--fixture", default=None,
                   help="用本地 fixture JSON 代替拉取 GitHub（测试/离线用）")
    p.add_argument("--version", action="version",
                   version="%%(prog)s %s" % __version__)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    lang = args.lang

    if args.fixture:
        pr, diff = load_fixture(args.fixture, lang)
        owner = pr.pop("_owner", None) or "fixture"
        repo = pr.pop("_repo", None) or "fixture"
        src = "fixture:" + args.fixture
    else:
        if not args.ref:
            print(T(lang, "no_ref"), file=sys.stderr)
            sys.exit(1)
        try:
            owner, repo, num = parse_ref(args.ref)
        except ValueError:
            print(T(lang, "bad_ref"), file=sys.stderr)
            sys.exit(1)
        pr, diff = fetch_pr(owner, repo, num, lang)
        src = "%s/%s/%d" % (owner, repo, num)

    pr["_owner"], pr["_repo"] = owner, repo
    files = split_diff(diff)
    diff_text, truncated, skipped = smart_truncate(files, lang)
    prompt = build_prompt(pr, diff_text, truncated, skipped, lang)

    meta_out = {
        "ref": src,
        "number": pr.get("number"),
        "title": pr.get("title"),
        "author": pr["user"]["login"],
        "state": pr.get("state"),
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "changed_files": pr.get("changed_files"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "files": [f for f, _ in files],
        "truncated_files": truncated,
        "skipped_files": skipped,
    }

    if args.dry_run:
        if args.json:
            print(json.dumps({"meta": meta_out, "prompt": prompt},
                             ensure_ascii=False, indent=2))
        else:
            print(T(lang, "dry_title", num=pr.get("number") or "?",
                    title=pr.get("title")))
            print(json.dumps(meta_out, ensure_ascii=False, indent=2))
            print("\n----- prompt -----\n")
            print(prompt)
        return

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print(T(lang, "no_key"), file=sys.stderr)
        sys.exit(1)
    base_url = args.base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    model = args.model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL

    digest = call_llm(prompt, lang, api_key, base_url, model)
    if args.json:
        print(json.dumps({"meta": meta_out, "digest": digest},
                         ensure_ascii=False, indent=2))
    else:
        print(digest)


if __name__ == "__main__":
    main()
