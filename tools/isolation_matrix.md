# 隔离矩阵（R-IND-01 交付物）

```bash
# ① 隔离 + 真框架：单测 + 零耦门禁 + 按 data.plugins.<name>.main 真导入并注册工具
PYTHON=/path/to/python bash tools/run_single_plugin.sh 来只清楚bot

# ② 无真框架：验证 astrbot 桩真的可用（AI 相关用例必须全绿）
PYTHON=/path/to/python bash tools/run_without_astrbot.sh
```

| 用例 | 期望 | 结果（实测） | 证据 |
|---|---|---|---|
| 只装本插件启动 | 无异常；插件类可导入；工具已注册 | ✅ | 2026-10-15，`插件类：[StickerPlugin]`；真 AstrBot 打印 `Added llm tool: sticker_send / sticker_stats / sticker_list` |
| 全量单测（隔离副本内） | 全绿 | ✅ | `79 passed`（exit=0） |
| 零跨插件 import | 门禁 exit 0 | ✅ | `零耦合门禁通过（17 个文件）` |
| **无真 astrbot 环境** | AI 相关用例全绿 | ✅ 50 passed | `tools/run_without_astrbot.sh`（注入 `find_spec("astrbot") → None`）：`tests/test_ai_tools_sticker.py + test_sticker_refactor.py + test_ai_compliance.py + test_ai_isolation.py + test_ai_contract.py` = **50 passed** |
| 无真 astrbot 环境（仓库全部用例） | 仅"需要真框架组件"的用例允许失败 | ⚠️ 75 passed / 4 failed | 4 例全在 `tests/test_platform_compat.py`（合并转发解析依赖真框架 `Forward/Node/Nodes` 行为）；CI 里装了 astrbot，不受影响 |
| 屏蔽关键词 | AI 调用被拒且**不发图**；错误码按设计 §7.2 为 `disabled` | ✅ | `test_send_checks_blocked_keyword_first`（断言 `event.sent == []`） |
| 限流 | 超阈值被拒；**按调用者**而非整群 | ✅ | `test_send_throttled_by_caller` / `test_throttle_is_per_caller` |
| 发送幂等窗口 | 同会话同关键词窗口内不重复发图（含**并发**） | ✅ | `test_send_success_and_idempotent_window` / `test_concurrent_sends_do_not_double_send`（`asyncio.gather` 两路 → 只 1 次真发） |
| 只读工具不写盘 | 不清理索引（区别于人类指令的自修复） | ✅ | `test_read_tools_do_not_write_index`（索引 JSON 前后一致） |
| 索引被改坏 | `../` 越目录条目被丢弃 | ✅ | `test_index_path_traversal_is_rejected` |
| 页面配置真落盘 | 页面形状的 payload 必须写进配置文件 | ✅ | `test_page_payload_is_really_saved`（真写 `config.json` 后逐键校验） |
| 人类指令不受影响 | 三条指令输出与重构前逐字一致 | ✅ | `test_human_list_out_of_range_text_unchanged`（越界文案仍是「页码超出范围」）+ `test_sticker_refactor.py` 7 例 + 既有 29 例 |

| 索引被改坏 | `../` 越目录条目被丢弃（**目录外文件真实存在**才算数） | ✅ | `test_index_path_traversal_is_rejected`（变异掉守卫即转红） |
| 超长关键词 | >32 字**明确拒绝**（不是静默截断） | ✅ | `test_overlong_keyword_is_rejected` / `test_overlong_keyword_cannot_hit_shorter_keyword`（上限改 64 即转红） |
| 发送失败可重试 | 失败回滚占位，下次真重试 | ✅ | `test_send_failure_allows_retry`（删掉回滚即转红） |

> 说明：`tools/no_astrbot_hook` 只 patch 了 `importlib.util.find_spec`，属"伪装无框架"而非硬隔离（真包仍在磁盘、子进程里仍可 import）；它的用途是验证桩本身可用——本轮正是靠它发现桩缺 `MessageChain`。
