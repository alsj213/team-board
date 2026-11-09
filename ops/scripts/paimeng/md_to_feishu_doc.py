#!/usr/bin/env python3
"""把 Markdown 文件写成飞书云文档（docx）。

用法: python3 md_to_feishu_doc.py <md文件> <文档标题>
输出: 文档 URL

支持的 Markdown 子集: # / ## / ### 标题、段落、- 与 * 列表、> 引用、``` 代码块、--- 分隔线、表格（转成文本行）。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path("/home/liu/.hermes/profiles/paimeng/.env")
BASE = "https://open.feishu.cn/open-apis"


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def call(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def token() -> str:
    e = env()
    r = call("POST", "/auth/v3/tenant_access_token/internal",
             body={"app_id": e["FEISHU_APP_ID"], "app_secret": e["FEISHU_APP_SECRET"]})
    if r.get("code") != 0:
        raise SystemExit(f"取 token 失败: {r}")
    return r["tenant_access_token"]


def text_block(block_type: int, key: str, content: str, extra: dict | None = None) -> dict:
    block = {"block_type": block_type, key: {"elements": [{"text_run": {"content": content}}]}}
    if extra:
        block[key].update(extra)
    return block


def parse(md: str) -> list[dict]:
    blocks: list[dict] = []
    lines = md.split("\n")
    i = 0
    in_code = False
    code_buf: list[str] = []
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            if in_code:
                blocks.append(text_block(14, "code", "\n".join(code_buf),
                                         {"style": {"language": 1, "wrap": True}}))
                code_buf = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("# "):
            blocks.append(text_block(3, "heading1", stripped[2:]))
        elif stripped.startswith("## "):
            blocks.append(text_block(4, "heading2", stripped[3:]))
        elif stripped.startswith("### "):
            blocks.append(text_block(5, "heading3", stripped[4:]))
        elif stripped.startswith("#### "):
            blocks.append(text_block(6, "heading4", stripped[5:]))
        elif stripped in ("---", "***", "___"):
            blocks.append({"block_type": 22, "divider": {}})
        elif stripped.startswith("|"):
            # 表格行 -> 纯文本（保留可读性）
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= {"-", ":"} and c for c in cells):
                i += 1
                continue
            blocks.append(text_block(12, "bullet", " ｜ ".join(cells)))
        elif stripped.startswith(("- ", "* ")):
            blocks.append(text_block(12, "bullet", stripped[2:]))
        elif stripped.startswith(">"):
            blocks.append(text_block(2, "text", stripped.lstrip("> ").strip()))
        else:
            blocks.append(text_block(2, "text", stripped))
        i += 1
    if in_code and code_buf:
        blocks.append(text_block(14, "code", "\n".join(code_buf), {"style": {"language": 1}}))
    return blocks


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    md_path, title = sys.argv[1], sys.argv[2]
    tok = token()

    created = call("POST", "/docx/v1/documents", tok, {"title": title})
    if created.get("code") != 0:
        print("建文档失败:", created)
        return 1
    doc_id = created["data"]["document"]["document_id"]
    print(f"文档已建: {doc_id}")

    blocks = parse(Path(md_path).read_text(encoding="utf-8"))
    print(f"共 {len(blocks)} 个块，分批写入…")
    doc_root = call("GET", f"/docx/v1/documents/{doc_id}", tok)
    root_id = doc_root["data"]["document"]["document_id"]

    written = 0
    for start in range(0, len(blocks), 40):
        chunk = blocks[start:start + 40]
        resp = call("POST", f"/docx/v1/documents/{doc_id}/blocks/{root_id}/children",
                    tok, {"children": chunk, "index": written})
        if resp.get("code") != 0:
            print(f"第 {start//40+1} 批写入失败:", resp)
            return 1
        written += len(chunk)
        print(f"  已写 {written}/{len(blocks)}")

    print(f"完成。https://feishu.cn/docx/{doc_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
