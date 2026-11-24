"""下载进度的行为测试：进度取自服务端已发送量（浏览器不给进度）。

把页面里真实的 startDownload / dlRender 源码抽出来，在 Node 里配最小 DOM 跑一遍，
用假的 A.apiGet 依次返回 33% / 67% / done，断言面板文案跟着服务端数据变化。
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
const title = el();
nodes.dlPanel.querySelector = () => title;
const texts = [];
Object.defineProperty(nodes.dlText, "textContent", { set(v) { texts.push(v); }, get() { return texts[texts.length - 1] || ""; } });
let clock = 0;
global.Date = { now: () => (clock += 1000) };
global.document = { getElementById: (id) => nodes[id] || el(), createElement: () => el(), body: { appendChild() {}, removeChild() {} } };
global.setTimeout = (fn) => { if (typeof fn === "function") fn(); return 0; };
const seq = [
  { filename: "demo.zip", state: "downloading", sent: 1 * 1024 * 1024, total: 3 * 1024 * 1024, percent: 33 },
  { filename: "demo.zip", state: "downloading", sent: 2 * 1024 * 1024, total: 3 * 1024 * 1024, percent: 67 },
  { filename: "demo.zip", state: "done", sent: 3 * 1024 * 1024, total: 3 * 1024 * 1024, percent: 100 },
];
let step = 0;
const A = {
  apiGet: async (endpoint) => {
    if (endpoint === "backup/download/progress") return seq[Math.min(step++, seq.length - 1)];
    return {};
  },
  download: () => { global.__downloadCalled = true; },
};
const src = [grab("fmtSize"), grab("dlPanelRefs"), grab("dlRender"), grab("saveBlob"), grab("startDownload")].join("\n");
const fn = new Function("A", "show", src + "\nreturn startDownload;");
const startDownload = fn(A, () => {});
startDownload({ filename: "demo.zip", size: 3 * 1024 * 1024 }).then(() => {
  console.log(JSON.stringify({ texts, downloadCalled: !!global.__downloadCalled }));
});
"""


def test_progress_follows_server_sent_bytes(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")

    result = subprocess.run(["node", str(script), str(PAGE)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[:500]
    data = json.loads(result.stdout.strip().splitlines()[-1])
    joined = " | ".join(data["texts"])

    assert data["downloadCalled"], "必须调用桥接下载"
    assert "33.3%" in joined, joined
    assert "66.7%" in joined, joined
    assert any("已接收" in t for t in data["texts"]), joined