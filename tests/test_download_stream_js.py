"""下载进度必须来自「前端实际接收字节」，而不是服务端已发送量。

做法：把页面里真实的 startDownload / dlRender / saveBlob 源码抽出来，
在 Node 里配最小 DOM 与模拟 ReadableStream 跑一遍，断言进度文案随接收字节递增。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "pages" / "manage" / "index.html"

HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
function grab(name) {
  let i = html.indexOf("function " + name + "(");
  if (i < 0) throw new Error("missing " + name);
  if (html.slice(Math.max(0, i - 6), i) === "async ") i -= 6;
  let depth = 0, started = false;
  for (let j = i; j < html.length; j++) {
    const c = html[j];
    if (c === "{") { depth++; started = true; }
    else if (c === "}") { depth--; if (started && depth === 0) return html.slice(i, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
const el = () => ({ style: {}, classList: { add() {}, remove() {} }, textContent: "", dataset: {},
                    appendChild() {}, remove() {}, click() {} });
const nodes = {};
["dlPanel", "dlFill", "dlText"].forEach((k) => { nodes[k] = el(); });
nodes.dlPanel.querySelector = () => el();
const texts = [];
Object.defineProperty(nodes.dlText, "textContent", { set(v) { texts.push(v); }, get() { return texts[texts.length - 1] || ""; } });
global.document = { getElementById: (id) => nodes[id] || el(), createElement: () => el(), body: { appendChild() {}, removeChild() {} } };
global.window = { ReadableStream: ReadableStream };
global.URL = { createObjectURL: () => "blob:x", revokeObjectURL() {} };
global.Blob = class { constructor(parts) { this.size = parts.reduce((a, p) => a + p.byteLength, 0); } };
global.localStorage = { getItem: () => "test-token" };
global.sessionStorage = { getItem: () => null };
global.setTimeout = () => 0;
global.fetch = async () => {
  const MB = 1024 * 1024; let n = 0;
  const stream = new ReadableStream({ pull(controller) {
    if (n >= 3) { controller.close(); return; }
    n += 1; controller.enqueue(new Uint8Array(MB));
  } });
  return new Response(stream, { status: 200, headers: { "content-length": String(3 * MB) } });
};
const A = {
  apiGet: async (e) => e === "backup/download/url" ? { url: "https://x/dl" } : { state: "idle" },
  download: () => { throw new Error("should not fall back"); },
};
const src = [grab("fmtSize"), grab("dlPanelRefs"), grab("dlRender"), grab("saveBlob"), grab("startDownload")].join("\n");
const fn = new Function("A", "show", src + "\nreturn startDownload;");
const startDownload = fn(A, () => {});
startDownload({ filename: "demo.zip", size: 3 * 1024 * 1024 }).then(() => {
  console.log(JSON.stringify(texts));
});
"""


def test_download_progress_comes_from_received_bytes(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")

    result = subprocess.run(
        ["node", str(script), str(PAGE)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr[:500]
    texts = json.loads(result.stdout.strip().splitlines()[-1])

    joined = " | ".join(texts)
    assert "33.3%" in joined, joined
    assert "66.7%" in joined, joined
    assert "100.0%" in joined, joined
    assert any("已接收" in t for t in texts), "结束时必须报告已接收量"
    # 进度必须是递增的，且每一步都来自接收字节（1/3、2/3、3/3）
    pcts = [t for t in texts if "%" in t]
    assert pcts[0].startswith("33.3%"), pcts
    assert pcts[-1].startswith("100.0%"), pcts