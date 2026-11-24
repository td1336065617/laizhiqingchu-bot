"""页面「回收孤儿」两次点击的请求体契约（审计必修：执行分支曾不可达）。"""

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
const el = (id) => ({ id, style: {}, classList: { add() {}, remove() {} }, textContent: "", onclick: null });
const nodes = { btnOrphan: el("btnOrphan"), btnQRestore: el("btnQRestore"), btnQPrune: el("btnQPrune") };
global.document = { getElementById: (id) => nodes[id] || el(id) };
global.window = { confirm: () => true };
global.setTimeout = () => 0;
global.clearTimeout = () => {};
const calls = [];
let dryFiles = 3;
const A = {
  apiPost: async (endpoint, body) => {
    calls.push(JSON.parse(JSON.stringify(body || {})));
    if (body && body.dry_run !== false) return { dry_run: true, files: dryFiles, move_mb: 1.2, batch: "b1" };
    return { dry_run: false, done: 3, move_mb: 1.2, batch: "b1" };
  },
};
const src = [grab("orphanAction"), grab("orphanSummary"), grab("bindOrphanButtons")].join("\n");
const build = () => new Function("A", "show", "nodes", src + "\nbindOrphanButtons();\nreturn nodes;");
(async () => {
  const bound = build()(A, () => {}, nodes);
  await bound.btnOrphan.onclick();
  await bound.btnOrphan.onclick();
  const twoClicks = calls.slice();
  calls.length = 0;
  dryFiles = 0;
  const bound2 = build()(A, () => {}, nodes);
  await bound2.btnOrphan.onclick();
  await bound2.btnOrphan.onclick();
  console.log(JSON.stringify({ twoClicks: twoClicks, emptyCalls: calls.slice() }));
})();
"""


def test_orphan_button_second_click_executes(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    script = tmp_path / "orphan.js"
    script.write_text(HARNESS, encoding="utf-8")

    result = subprocess.run(["node", str(script), str(PAGE)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[:500]
    data = json.loads(result.stdout.strip().splitlines()[-1])

    first, second = data["twoClicks"][0], data["twoClicks"][1]
    assert first.get("dry_run") is True, first
    assert second.get("dry_run") is False, "第二次点击必须直接执行"
    assert second.get("confirm") == "orphans", second
    assert all(c.get("dry_run") is not False for c in data["emptyCalls"]), "没有可回收时不得执行"