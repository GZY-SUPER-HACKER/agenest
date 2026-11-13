# agenest / csd：会话状态语义改造方案

**日期**：2026-11-13
**范围**：`sessions` 状态模型（去掉 `interrupted`、新增派生 `disconnected`、加 `stop_reason`、兼容旧 agent）
**线上版本**：v0.6.0（`caa9a668`）
**结论**：可行，而且比预想的轻 —— 项目里已经有两次同类型的成功先例，本次主要是「把一个已存在的显示投影改名 + 增加一条心跳专用判据 + 给终态加一个原因字段」。

---

## 0. 先说我核到的现状（这决定改法，不是背景铺垫）

这些是我逐文件读过的，不是推测。

- 状态是**闭集五个**：`running / completed / failed / interrupted / unknown`（`backend/app/schemas.py:36-49`）。**没有 `finished`**，你说的 finished 在库里叫 `completed`。这点在下面的「命名」一节会展开。
- **`possibly_stale` 已经存在**，而且就是你第二点描述的「派生状态」：`SessionOut.possibly_stale` 是 display-only 布尔，注释明写「hint; the stored status is never rewritten to reflect it」（`schemas.py:305-307`），由 `session_service.py:239-266` 在读取时按 `now - last_activity_at >= stale_after_seconds` 算出。阈值环境变量 `CSD_STALE_AFTER_SECONDS`，`deploy/deploy.sh:166` 设为 **900 秒**。
- **心跳已经在刷 `last_activity_at`**：`event_service.py:146-152`，heartbeat 分支只增 `heartbeat_count` 并推进 `last_activity_at`，不动 `updated_at`。也就是说 —— **你第一点要求的「收到心跳就退回 running」，现有机制已经是这个行为**（心跳到达 → `last_activity_at` 前进 → `possibly_stale` 自动变回 false）。不需要新增「回翻」逻辑，只要 disconnected 建在同一个时钟上。
- **host 侧已经把「沉默」和「离开」分开了**，这是最直接的样板：`agent_hosts.disconnected_at` 只在 connector **明确说走**时才写；单纯不说话只变 `possibly_stale`，注释原话是「the honest rendering — there is nobody left to ask」（`database.py:94-97`、`host_service.py` 模块 docstring、`schemas.py:428-430`）。
- **迁移是幂等加列**：`Database._migrate()` 的 `_add_missing()` 用 `PRAGMA table_info` 判断列是否存在再 `ALTER TABLE ... ADD COLUMN`，无 down-migration，列存在即标记（`database.py:310-397`）。所以加两列不需要写迁移脚本、不需要版本号、二次启动是 no-op。
- **`/close` 端点今天写的就是 `interrupted`**，`api/sessions.py:176-215`，docstring 里明确规定「`interrupted`, never `completed`：honest claim is 'this stopped and no outcome was reported'」。
- **心跳周期 5 分钟**（`sdk/csd-agent/csd_agent/install.py:228-232`，systemd timer `OnUnitActiveSec` / Windows schtasks `/MO 5`）。所以 900 秒阈值 ≈ 3 次心跳落空才判静默，比例是健康的。
- **会发 `interrupted` 的上报端不止 csd-agent，有四个**：csd-agent（`heartbeat.py` pid 判死 + `hooks.py:226` SessionStart 关死会话）、`sdk/pi-csd-extension/csd-reporter.js`（`aborted`→interrupted，185/723/772/793）、`sdk/dsh-csd-plugin/lib/reporter.js`（`aborted`/`blocked`/crash-orphaned→interrupted，90/93/98）、`sdk/cpctl/cli.py`（`--status` choices 三处，534/555/678）。兼容层必须覆盖这四个 + 已在跑的老二进制。
- `docs/design.md` 里有若干处把 `interrupted` 写成了设计理由（§2 第 37 行、§28 第 310-311 行、§42 第 2562-2585 行），改动要同步改文档，否则下一个读代码的人会认为是实现 bug。

一句话：**你的四点主张里有两点已经实现了（派生、可逆），一点是新增字段（`stop_reason`），一点是需要小心的一件事（去掉 `interrupted` 会牵动四个上报端 + 一批文档）。**

---

## 1. 目标状态模型

### 存储状态（只有三个）

- `running` —— 被上报为在跑，且没有终局报告。
- `completed` —— 有一方报告了结局。**不带褒贬**，成功与否由 `stop_reason` + 已有的事件（`task_completed` / `task_failed` / `exit_code`）表达。
- `failed` —— 进程/运行时明确失败（保留，不动）。

`interrupted` 从存储状态里移除。`unknown` 是否保留见 §7 待定项。

### 派生显示状态（不入库，读取时算）

- `disconnected` —— `status == running` 且 心跳静默超阈值。**这不是一个新状态，是 `possibly_stale` 的语义收窄 + 改名**。
- 一旦有心跳到达，条件自然不成立，渲染自动回到 `running`。**无需任何后台任务、无需回翻写入。**

### 新字段

- `stop_reason`：`user` / `crash` / `unknown`（终态时才有值；`running` 时为 NULL）。
  - `user` —— 人在 dashboard 上点了关闭（`/close`）。
  - `crash` —— 上报端发现进程没了（pid 判死、SessionStart 发现上个进程没发 SessionEnd、DSH crash-orphaned）。
  - `unknown` —— 旧 agent / 旧数据 / 无法判断来源。

这直接照 deer-flow 的 status contract 的做法，你的建议是对的。

### 命名提醒（必须你拍一下）

你写的是 `finished`，库里是 `completed`。三个选择：

1. **沿用 `completed`**，只是把「手动关闭」也写进去，靠 `stop_reason=user` 区分。改动最小，但前端那个绿色 ✓ 会出现在「我自己中止的」行上 —— 必须同步改渲染（见 §5），否则就是新的说谎。
2. 新增 `cancelled`/`stopped` 终态。语义最干净，但立刻撞上 `TERMINAL_STATUSES`、`TERMINAL_EVENTS`、前端 5 处映射、cpctl choices、四个 reporter 的映射表，改动面翻倍。
3. 沿用 `completed` 但给 `stop_reason=user` 单独的视觉（灰，非绿），文案写 `closed by you` 而不是 `completed`。

**我推荐 3**：不扩状态集，靠 `stop_reason` 分层，前端按 `stop_reason` 决定颜色与文案。它满足你「别丢掉这个区别」的要求，又不把 5 个状态扩成 6 个。

---

## 2. SQL / 迁移

### 2.1 schema 定义（`backend/app/database.py:30-55` 的 `CREATE TABLE sessions`）

```sql
    -- 最近一次心跳（received_at）。与 last_activity_at 分开：后者是「有任何动静」，
    -- 前者是「agent 亲口说我还活着」。disconnected 只用后者判定，见 §3 的误判分析。
    last_heartbeat_at TEXT,
    -- 终态的原因。'user' | 'crash' | 'unknown'；running 时为 NULL。
    -- 不加 CHECK：旧 agent 的枚举值未来可能扩，服务器要能安静地接收而不是 500。
    stop_reason       TEXT,
```

### 2.2 迁移（`_migrate()` 里加一行 `_add_missing` 调用）

```python
        # 「没消息」与「已结束」是两件事（§43）。last_heartbeat_at 只在心跳时前进，
        # stop_reason 只在终态时有值；两者都可空，旧行读出来就是「不知道」，这是诚实的。
        self._add_missing(
            "sessions", {"last_heartbeat_at": "TEXT", "stop_reason": "TEXT"}
        )
        self._backfill_last_heartbeat()
```

### 2.3 回填（可选但推荐，一次性、幂等）

没有回填的话，所有历史 `running` 行第一天全变 `disconnected`（因为 `last_heartbeat_at` 为 NULL）。有两条路：

```sql
-- 路 A（推荐）：从事件表回填，条件天然幂等（只填 NULL）
UPDATE sessions
   SET last_heartbeat_at = (
       SELECT MAX(received_at) FROM session_events e
        WHERE e.session_id = sessions.id AND e.event_type = 'heartbeat'
   )
 WHERE last_heartbeat_at IS NULL;
```

路 A 也有代价：回填完，一个**确实早就死了**的历史 running 行会显示 `disconnected`（而不是 `running`）。这其实是对的 —— 它确实失联了，只是不妨碍它是僵尸。

```sql
-- 路 B：把存量 interrupted 归一到新模型（可选，别默认做）
UPDATE sessions SET status = 'completed', stop_reason = COALESCE(stop_reason, 'unknown')
 WHERE status = 'interrupted';
```

**对路 B 我的建议是：先不做。** 理由：`interrupted` 是历史事实，改写它等于篡改归档，而且 `stop_reason=unknown` 并不能告诉你当时是谁关的。更省的做法是「读时归一」—— 序列化层把 `interrupted` 映射成 `completed` + `stop_reason='unknown'`，历史行保持原样。要不要落到库里，等第一版跑顺了再说。这条和 deploy 里已有的 data 备份习惯（`/srv/csd/data-backups/`）配合就行。

---

## 3. 后端改动清单

按文件给，逐条可勾。

### 3.1 `backend/app/schemas.py`

- `SessionStatus`：删 `interrupted`。**`SessionOut` 增加**：
  - `stop_reason: Literal["user","crash","unknown"] | None = None`
  - `disconnected: bool = False`（与 `possibly_stale` 并存还是替换，见 §3.6）
- `TERMINAL_STATUSES`：`{completed, failed}`。
- `TERMINAL_EVENTS`：`session_interrupted` **保留在枚举里**（历史时间线要能渲染），但从 `TERMINAL_EVENTS` 里摘掉；新增映射时把它当 `session_completed` 的别名处理。
- **新增兼容归一函数**（关键，见 §4）：

```python
def normalise_status(value: str) -> tuple[SessionStatus, str | None]:
    """旧 agent 的措辞收进新模型，服务器不因此 500（§43）。"""
    if value == "interrupted":          # 旧 csd-agent / pi / dsh / cpctl
        return SessionStatus.completed, "unknown"
    return SessionStatus(value), None
```

### 3.2 `backend/app/services/event_service.py`

- heartbeat 分支（`146-152`）增加一行：`sets.append("last_heartbeat_at = ?")`，值 `max(row["last_heartbeat_at"] or "", received_at)`（单调，与 `last_activity_at` 同一刻意写法）。
- 终局事件 `session_interrupted` 到达时：`status = completed`、`stop_reason = 'unknown'`（不知道是谁关的），而不是整请求拒绝。
- 若事件 payload 带 `stop_reason`（新 agent 会带），以 payload 为准。

### 3.3 `backend/app/services/session_service.py`

- `_row_to_session` / `_row_to_detail`：新增 `stop_reason`、`disconnected` 的投影。
- `_with_staleness`（`255-266`）改造成两条判据（这是全套改动的技术核心）：

```python
def _quiet_seconds(session, now) -> float:
    """'多久没消息' 用哪个时钟。

    有心跳记录的会话用 last_heartbeat_at —— 它才是「agent 还活着」的正面证据。
    没有心跳记录的会话（手工 cpctl、pi/dsh 的部分上报形态、API 直连）退回
    last_activity_at，否则这类行一上线就全部变灰。fallback 不是宽容，是避免
    把「没有心跳机制」误读成「心跳断了」。
    """
    clock = session.last_heartbeat_at or session.last_activity_at
    return (now - _to_dt(clock)).total_seconds()
```

- `patch_session`（`481-...`）：
  - 收到 `status=interrupted` 的 PATCH 时归一为 `completed` + `stop_reason=unknown`（兼容旧 cpctl `session update --status interrupted`）。
  - 新增 `stop_reason` 写入路径；终态首次写入时落值，粘性（first-write-wins，与现有终态规则一致）。
  - 拒绝把 `stop_reason` 写到 `running` 行上。
- `dismiss_session` 不动（它已经是「不碰状态、不碰时间」的正确形状）。

### 3.4 `backend/app/api/sessions.py`

- `/close`（`176-215`）：`SessionPatch(status=completed, stop_reason="user", summary=...)`。**docstring 必须改** —— 现在整段在论证「interrupted, never completed」，改完不重写会立刻自相矛盾。
- 其它路由基本不动；`SessionPatch` 的 `extra="forbid"` 意味着旧客户端发 `status: "interrupted"` 不会 422（值层面），但要在 `schemas.py` 的校验器里接住（见 §4）。

### 3.5 `backend/app/agent/backends/*.py`、`main.py`

- `native.py` 等处若有 `interrupted` 的状态映射，跟着归一。
- `main.py:92` 附近对 stale 的注入点不变，只是判据换成 §3.3 的函数。

### 3.6 `possibly_stale` 和 `disconnected` 是否并存

两个选择：

- **并存**（`possibly_stale` 表示「没有活动」，`disconnected` 表示「没有心跳」）。语义更精确，但前端要处理两个布尔四种组合，容易写成四不像。
- **替换**（`possibly_stale` 改名为 `disconnected`，判据换成心跳时钟）。前端一处改，API 少一个字段。

**推荐替换，但保留 `possibly_stale` 一个版本作为 deprecated 别名**（值恒等于 `disconnected`，响应里继续给），这样手机上没更新的前端、以及可能存在的第三方读端不会当场炸。下一版删掉。

---

## 4. 旧 agent 兼容（这条最容易出事，单独成节）

四个上报端 + 两台机器上已在跑的老二进制，全都在发 `interrupted`。服务器一旦从闭集里删掉它，`SessionStatus(value)` 会抛 `ValueError`，而入口在 FastAPI 里就是 **500**。所以：

1. **入口一律归一，不校验拒收**：`SessionCreate.status` / `SessionPatch.status` / `EventCreate` 的对应字段，都过 `normalise_status()`。宁可服务器自己翻译，也不要让旧机器以为「上报失败」而无限重试。
2. **`/close` 之外的 `session update --status interrupted` 也要接住**：cpctl 的 choices 里有它（`cli.py:534/555/678`），老版本 CLI 会照发。
3. **`session_interrupted` 事件保留可接收**，只做状态映射，不报错。
4. **四份 SDK 源码同步改**（发版一起走，否则新 SDK 又往回写老语义）：
   - `sdk/csd-agent/csd_agent/heartbeat.py`：pid 判死 → `--status completed --stop-reason crash`（新增 CLI 参数）。
   - `sdk/csd-agent/csd_agent/hooks.py:226`：SessionStart 关死会话 → 同上；`hooks.py:463` 的 `SessionEnd → completed` 保持（这是 clean exit，`stop_reason=unknown` 或省略）。
   - `sdk/pi-csd-extension/csd-reporter.js`：`aborted` → `completed` + `stop_reason=user`（用户主动 abort 的语义）；`session_shutdown` 关未结算 run → `crash`。
   - `sdk/dsh-csd-plugin/lib/reporter.js`：`aborted`/`blocked`/crash-orphaned → `crash`。
   - `sdk/cpctl/cpctl/cli.py`：`--status` choices 去掉 `interrupted`，新增 `--stop-reason {user,crash,unknown}`（**但服务器仍收 `interrupted` 一个过渡期**）。
5. **过渡期定多长要写进发版说明**。建议「服务器永久接受 `interrupted` 输入并归一」而不是「过渡三个月后删」—— 归一成本是一行代码，而删掉的收益是零，还换来一次线上 500 的风险。这条和项目既有的「NON_TERMINAL_EVENTS 必须能安静接收」风格一致。

**必须写进发版说明的原文**（草）：

> 本版起，`interrupted` 不再是存储状态，手动关闭/崩溃关闭都记 `completed`，并用新字段 `stop_reason`（`user`/`crash`/`unknown`）区分原因。**旧版 csd-agent / pi 扩展 / DSH 插件 / cpctl 无需升级**：服务器会把它们上报的 `interrupted` 静默归一为 `completed` + `stop_reason=unknown`。dashboard 上「running 但很久没有心跳」的会话改为显示 `disconnected`，一旦恢复心跳自动变回 `running`。

---

## 5. 前端改动清单

- `frontend/src/types/api.ts`：`SessionStatus` 删 `interrupted`；`Session` 加 `stop_reason`、`disconnected`；`possibly_stale` 标 deprecated。
- `frontend/src/lib/format.ts`：
  - `STATUS_LABEL`（53-58）删 `interrupted`；`completed` 的显示名改为按 `stop_reason` 二选一：`user` → **`closed by you`**，其余 → `completed`。
  - `eventKind`（31-32）保留 `session_interrupted → "interrupted"`（历史事件要渲染），新事件不会再产生。
  - `isBusy`（80）不变（`running` 才算活）。**新增** `isDisconnected(s)`。
- `OverviewPage.tsx:211-236` / `Rail.tsx:485-500` / `DetailPage.tsx:88-105,165`：
  - 现有 `possibly_stale` 的渲染是「No activity reported」/「As last reported; no activity since」。改名后文案建议：
    - 卡片行：**`disconnected · no heartbeat`**
    - 详情 title：**`Last heartbeat <相对时间> ago; the agent may be asleep or offline`**
    - 详情通知条：`This session has not reported a heartbeat since <时间>. It may be a sleeping laptop, a dead network, or a process that is gone. Nothing here can tell which.`
  - **颜色**：现在是 hollow dot / `status--stale`（安静样式）。保持安静，**不要用红色** —— 你这条的核心主张就是「失联 ≠ 失败」，红色会把它变回失败。红色只留给 `failed` 与 `task_failed`。
  - `stop_reason === "user"` 的行：中性灰 pill，**不要绿色 ✓**。这是 §1 选择 3 的必要配套，否则「我自己关的」会被渲染成「恭喜完成」。
- `Rail.tsx:165` 的 `connectedHosts` 统计口径跟着改（host 侧不动，但 session 侧若参与了某个计数，要核对）。

---

## 6. 验收（能证明改动成立的测试）

现有 `backend/tests/test_contract.py` 里有 `interrupted` 用例，按下面改/加：

1. **可逆**：running + 超阈值 → `disconnected=true`；POST 一条 heartbeat → 同一 session 再 GET，`disconnected=false`、`status` 仍是 `running`。**这条是整套改动的验收核心**，它同时证明了「派生」和「可逆」。
2. **不误判**：无 `last_heartbeat_at` 但有 `progress` 事件的 running 会话，不应因「从没心跳」被判 disconnected（走 fallback）。
3. **旧 agent**：POST `status=interrupted` → 200，库里 `completed` + `stop_reason=unknown`；POST `event_type=session_interrupted` → 200 且同样归一。
4. **`stop_reason` 粘性**：已是 `completed/user` 的行，再来一条 crash 上报不得改写（first-write-wins）。
5. **`/close`**：调 `/close` → `completed` + `stop_reason=user`，且 `finished_at` 有值、`last_heartbeat_at` 不变（关闭不是心跳）。
6. **迁移幂等**：老库启动两次，列只加一次，回填不重复。
7. **前端契约**：`SessionOut` 里 `possibly_stale` 仍存在且等于 `disconnected`（deprecated 别名生效）。

测试跑法（本机无 root 也行，pytest 装在 backend 的 venv 里）：`cd /opt/csd/backend && python -m pytest tests -q`。

---

## 7. 还没定、需要你拍的四件事

1. **`completed` vs 新增 `cancelled`**（§1 命名）—— 我推荐 `completed` + `stop_reason`，你如果坚持「中止就不该叫完成」，那就得接受改动面翻倍。
2. **存量 `interrupted` 行**：读时归一（推荐，不动历史）还是落库 UPDATE。
3. **`unknown` 状态怎么办**。它今天存在（闭集里）但没有谁主动写它，语义是「创建时就不知道结局」。加了 `stop_reason=unknown` 之后两者重叠了 —— 建议把 `SessionStatus.unknown` 也一并废弃，统一用 `completed + stop_reason=unknown`；但这会再次牵动闭集与前端映射，所以要你点头。
4. **阈值**。900 秒 / 心跳 5 分钟 = 3 次落空。够不够？笔记本合盖再打开常常超过 15 分钟，会经历一段 `disconnected` 再自己变绿 —— 这符合你的要求（暂时失联），但如果你觉得屏幕上一闪一闪很吵，可以放宽到 1800 秒。

---

## 8. 僵尸会话的两处补丁（顺带，独立于上面）

改造状态语义**不解决**僵尸本身。`lab/csd-zombie-sessions/NOTE-20261112.md` 记的两个成因仍在：

- `heartbeat.py` 在判死上报**失败的重试期**不刷 `last_seen_at` → 3 小时后被 `tracked.prune()` 删掉，从此没人再查。**改法**：重试分支也 `entry.last_seen_at = time.time(); tracked.put(entry)`。
- `entry.pid <= 1` 走 skip 分支、永不判定。**改法**：pid 未知时不要当成「不知道就不管」，而是在 `prune()` **之前**先查云端状态（你 11-13 提的方向）：如果服务端该会话仍是 `running` 且本地已无对应进程，就置终态（`crash`）。

这两条与状态改造可以同版发，也可以后置；但**只改状态语义、不改这两处，僵尸还会继续产生**，只是它们现在会显示成 `disconnected` 而不是 `running` —— 诚实了，但没消失。

---

## 9. 粗估

- 后端（schemas / event_service / session_service / api / 迁移 / 测试）：**改动集中在 3 个文件 + 7 个测试用例**，量不大，但 `_apply_session_effects` 与 `patch_session` 都有「先读事务外会写坏」的历史包袱（design.md §35），动它们必须跑全量 pytest 而不是挑几个。
- SDK 四份：各自几十行映射。
- 前端：三个组件 + 两个 helper，文案比逻辑多。
- 发版：`release.sh cut <tag>` → `deploy`，线上 v0.6.0 → v0.7.0，走既有备份（`/srv/csd/data-backups/`）。

**总判断**：一天到两天的活，风险点不在写码，在 §3.3 的时钟 fallback（判据一旦选错，所有非 csd-agent 上报的行会集体变灰）和 §4 的归一（漏一处就是线上 500）。
