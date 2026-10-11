# StoryLoop Harness

**中文** | [English](README.en.md)

用于互动叙事的 Python 运行时，维护持续变化的世界事实与角色各自的信息视角。开发者可以把玩家的自由输入转成场景叙述、经过校验的事件和新的状态快照。

例如，玩家来到港口，可以向码头工提问，也可以尝试打破店铺橱窗。运行时先组装相关上下文，让模型给出结构化提案，再按剧本规则检查并提交获准的状态变化。下一回合读取更新后的世界；模型写出一段叙述，并不意味着提议的变化已经成为事实。仓库包含这个[港口示例](examples/freeform)。

**状态：** 持续孵化中 · Python 3.12+ · MIT。API、配置和存档格式可能直接调整，不提供向后兼容或迁移保证。

## 它适合放在哪里

通过注入模型和存储，Harness 可以独立用于你自己的叙事应用、命令行工具或实验。[StoryLoop Platform](https://github.com/storyloop0/storyloop-platform) 是配套的玩家与创作者应用，负责前端、账号、模型路由、计费、SQL 存储和部署。

```mermaid
flowchart LR
    App[你的应用或 StoryLoop Platform] --> Engine[Harness TurnEngine]
    Package[ScenarioPackage：规则与故事素材] --> Engine
    Engine --> Model[ModelPort：生成适配器]
    Engine --> Store[GameStore：持久化实现]
    Engine --> Result[TurnOutcome：故事、快照、事件、用量]
    Result --> App
```

Harness 负责上下文投影、结构化生成、剧本校验和事件执行；调用方负责凭据、持久存储、请求归属与价格策略。内置的内存存储和确定性模型，让你无需模型密钥就能体验这些边界。

## 一个回合如何执行

```mermaid
flowchart TD
    Input[玩家输入与已有快照] --> Project[投影上下文并检查预算]
    Project --> Generate[生成结构化场景提案]
    Project -->|必要材料无法放入窗口| Fail[在模型调用和世界写入前失败]
    Generate --> Validate[校验并修正提案]
    Generate -->|生成异常| Stop[在提案提交前返回错误]
    Validate -->|提案被拒绝| Stop
    Validate --> Commit[按预期版本提交获准事件]
    Commit -->|存储冲突或异常| StoreError[向调用方传递存储错误]
    Commit --> Work[处理就绪工作：角色发言与场景提示]
    Work --> Output[可见故事与更新后的快照]
```

普通 `single_call` 回合采用一次结构化场景生成阶段，再执行确定性工作。传输重试和其他定时生成任务可能产生额外模型请求。角色回复一起生成，通过 `single_npc_reply` 工作交付；每个角色没有独立的 ReAct 循环。

校验会筛选目标、回复和里程碑，也会把无效的开放动作效果改成尚未确认结果的尝试。预设动作由剧本规则裁决效果。状态变化仍是提案，调用方可通过 `proposed_status` 读取并结算。存储异常或后续工作失败需要调用方处理；整个回合并不保证是一个原子事务。

## 可以检查的上下文

```mermaid
flowchart LR
    Sources[状态、可见世界书、来源事实与里程碑] --> Projector[上下文投影器]
    Views[玩家历史与选中角色的卡片和经历] --> Projector
    Budget[材料上限与完整请求窗口] --> Projector
    Projector --> Request[供生成使用的独立场景请求]
    Projector --> Report[开发者报告：来源、选择与估算]
```

`TurnEngine.project_context` 同步预览普通场景请求，不调用模型、不写入存储。角色视角有各自的历史与选择规则，但共享模型会看到多个角色的上下文，因此不能保证角色秘密的硬隔离。

独立请求包含故事素材；不可变报告包含来源 ID、归属、状态/检查点版本、字段、选择/裁剪原因与大小估算，不包含来源正文。两者都应留在开发者侧。报告不会进入玩家回合输出或故事事件；默认遥测只记录汇总数量与估算值。

## 快速开始：无需模型密钥

克隆仓库并安装到虚拟环境：

```sh
git clone https://github.com/storyloop0/storyloop-harness.git
cd storyloop-harness
python -m venv .venv
# macOS / Linux:
. .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install .
python examples/offline_turn.py
```

请使用 Python 3.12+。示例采用仓库内的合成剧本、内存存储和确定性模型，不请求模型服务，输出故事与最终快照版本。

### 使用回合 API

安装后在仓库根目录运行：

```python
import asyncio
import sys

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.testing import InMemoryGameStore, OfflineModel


async def main():
    package = ScenarioPackage.load("examples/freeform")
    store = InMemoryGameStore()
    package.seed_game(store, "demo")
    engine = TurnEngine(store, package, OfflineModel())
    outcome = await engine.run_turn(
        TurnInput("demo", "Hello", "turn-1", package.version)
    )
    print(outcome.narration)
    print(outcome.snapshot.version)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
```

连接真实模型时提供 `ModelPort` 实现；`generation.CompatibleOpenAIChatModel` 适配器使用 AgentScope 2.0.9，凭据与路由由调用方提供。持久化游戏时实现 `GameStore` 及其预期版本提交契约。

## 检查上下文与配置预算

在仓库根目录运行：

```python
from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded
from storyloop_harness.testing import InMemoryGameStore, OfflineModel

package = ScenarioPackage.load("examples/freeform")  # synthetic scenario
store = InMemoryGameStore()
package.seed_game(store, "inspection")
engine = TurnEngine(store, package, OfflineModel(), context_budget=ContextBudget(
    context_window_tokens=65536, output_tokens=4096,
    safety_margin_tokens=256, max_material_tokens=16000,
))
projection = engine.project_context(
    TurnInput("inspection", "Hello", "inspect-1", package.version)
)
print(projection.report.to_dict())
```

上例也是默认限制：完整请求窗口 65,536 Token，预留输出 4,096、安全余量 256，序列化材料另设 16,000 Token 上限。`utf8-conservative-v1` 使用字符/UTF-8 启发式估算，并非服务商的精确 Token 数。`scene-projection-v1` 计入系统提示词、消息封装与转义、展开后的输出 schema、传输余量、输出预留和安全余量。AgentScope 适配器在 HTTP 前检查 SDK 格式化后的请求，并发送一个有效的 `max_completion_tokens` 上限。自定义 `ModelPort` 必须遵守这个有界调用契约。

公开规则、来源解析、玩家输入、权威状态以及选中角色的非空身份信息属于必要内容。如果无法放入预算，`ContextBudgetExceeded` 携带不含来源正文的报告，在模型调用和世界写入前失败。修改后的检查请求会在生成时重新估算。该策略覆盖普通场景请求及每次传输尝试，不计量整局支出。

<details>
<summary>上下文选择规则参考</summary>

检查范围包括各接收者检查点之后的历史、最近四次玩家输入、最近六条玩家观察、可见世界书条目、公开/系统来源事实和里程碑候选，以及选中回复角色的私有卡片与历史。未选中角色的私有上下文和其他存档不在范围内。引擎使用自身的时钟、故事程序、回复角色上限和显式玩家偏好作用域。

`selected` 与 `truncated` 相互独立。`omission_reason` 记录排序/条数限制、字段裁剪（`field_limit`）或材料/窗口预算裁剪。来源事实与里程碑提示先于近期经历让出预算；从提示词中省略里程碑不会改变它的触发资格。

`testing.project_scene_request(store, package, game_id, player_text, context_budget=ContextBudget())` 使用同一投影器，仅返回独立请求；还接受 `clock`、`program` 和 `max_responders`。

</details>

## 集成接口参考

| 入口 | 用途 |
| --- | --- |
| 顶层包 | `ScenarioPackage`、`TurnEngine`、`TurnInput`、`TurnOutcome`、`GameStore`、`ModelPort`、`CampaignContext` |
| `contracts`、`ports` | 回合数据及模型/存储/故事程序协议 |
| `context` | `ContextBudget`、`ContextProjection`、`ProjectionReport`、`MaterialDecision`、`ContextBudgetExceeded` |
| `advanced` | 事件、工作与故事时钟契约 |
| `generation` | 结构化模型传输、格式化与动作建议 |
| `telemetry`、`usage` | 追踪与作用域内 Token 用量，不含价格策略 |
| `testing` | 内存存储、离线模型与独立请求检查 |

`run_turn` 接收 `TurnInput`，可选进度回调与故事时间上限；`run_ready_work` 处理支持的待办工作。`TurnOutcome` 包含可见输出、快照、本次调用提交的事件和模型用量。内部模块路径不属于扩展 API。

## 当前限制与开发

- 带有旧 `npc_reply` 待办的存档会在生成和世界写入前被拒绝，需要重建。
- 持久存储与跨进程协调需要调用方实现存储与应用层机制。
- 已存回合重放与预期版本提交，不代表端到端计费恢复或分布式执行保证。
- 长局记忆、可复现的质量/成本对照与更强的执行恢复仍是后续方向。

```sh
python -m pip install -e '.[test]' build
python -m pytest tests -q
python -m build --wheel
```

CI 还会在仓库外安装构建出的 wheel 并运行离线示例。确定性测试不能证明真实模型的叙事质量。

## 许可证

[MIT](LICENSE)。
