#!/usr/bin/env bash
#
# dev.sh — 本机开发模式。
#
# 设计目标：**开发时永远不碰云端**。这个脚本只监听 127.0.0.1，数据写进 backend/data/，
# token 存在 backend/data/dev-token（不进 git）。云端那套 systemd / env 文件 / Tailscale
# 一概不参与。
#
# 用法（在仓库根目录）：
#   ./dev.sh start     后台起后端 + 前端，打印访问地址
#   ./dev.sh stop      停掉
#   ./dev.sh restart   重启（数据保留）
#   ./dev.sh logs      跟后端日志
#   ./dev.sh status    看状态
#   ./dev.sh token     打印 dev token（给 cpctl 用）
#   ./dev.sh reset     删掉本机数据，从零开始（会先确认）
#
# 为什么后端在 8780、前端在 5173：
#   Vite dev server 有热更新，改前端立刻能看到；它把 /api 代理到 8780，所以浏览器只
#   需要开 5173，不用管 CORS，也不用在代码里写死 API 地址。

set -uo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
DATA="$BACKEND/data"
RUN="$DATA/run"
PY="$BACKEND/.venv/Scripts/python.exe"
API_PORT="${CSD_DEV_API_PORT:-8780}"
UI_PORT="${CSD_DEV_UI_PORT:-5173}"

blue()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
die()   { red "$*"; exit 1; }

# Windows 布局是 Scripts/，Linux 是 bin/。两条都认，这样脚本在 WSL 里也能用。
if [ ! -f "$PY" ]; then
  [ -f "$BACKEND/.venv/bin/python" ] && PY="$BACKEND/.venv/bin/python"
fi

port_pid() {
  # Vite 默认绑 `[::1]`（IPv6），uvicorn 绑 `127.0.0.1`（IPv4）。只查其中一边会漏掉
  # 另一种占用，然后 `--strictPort` 以一条看不懂的 "port already in use" 失败 ——
  # 这个坑真踩过：5173 被占，但脚本查不出是谁。
  #
  # 匹配 `:<port>` 后面必须紧跟空白（netstat 的列分隔），否则 8780 会误匹配 18780。
  netstat -ano 2>/dev/null \
    | awk '$4 ~ /LISTENING/ { n=split($2, a, ":"); if (a[n] == "'"$1"'") { print $5; exit } }'
}

kill_port() {
  # 按端口杀，而不是只信 pid 文件：pid 文件可能因为各种原因对不上，
  # 而端口不会骗人。`/T` 是必须的 —— Windows 上 npm 会派生 node 子进程，
  # 只杀父进程会留下一个还占着端口的孤儿。
  local port="$1" pid
  pid="$(port_pid "$port")"
  if [ -n "$pid" ]; then
    taskkill //PID "$pid" //T //F >/dev/null 2>&1 || kill -9 "$pid" 2>/dev/null
    return 0
  fi
  return 1
}

ensure_venv() {
  if [ ! -f "$PY" ]; then
    blue "==> 第一次运行，创建 venv"
    ( cd "$BACKEND" && python -m venv .venv )
    "$PY" -m pip install -q --upgrade pip
    "$PY" -m pip install -q fastapi "uvicorn[standard]" "websockets>=12" pytest httpx psutil
    green "    venv 就绪"
  fi
  "$PY" -c 'import fastapi, uvicorn' 2>/dev/null \
    || die "venv 里缺依赖，跑一次：cd backend && .venv/Scripts/pip install -e '.[dev]'"
}

ensure_token() {
  # 开发环境的 token 不值得当秘密对待，但仍然要存在且稳定：否则每重启一次，
  # 复制粘贴到 cpctl 的那串就得换，等于自己给自己制造摩擦。
  if [ ! -f "$DATA/dev-token" ]; then
    "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$DATA/dev-token"
    green "    生成了新的 dev token（存在 backend/data/dev-token）"
  fi
}

ensure_access_token() {
  # 一个 token 管整页：读、关会话、驱动 agent（docs/design.md §22）。本地必须有它，
  # 否则 agent 那半边是 403 关着的 —— “没设 token = 读开放、能动手的关闭”是那条默认值。
  # 值随机、只在本机，永不发到网上。
  if [ ! -f "$DATA/dev-access-token" ]; then
    "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$DATA/dev-access-token"
    green "    生成了新的 access token（存在 backend/data/dev-access-token）"
  fi
}

ensure_node_deps() {
  if [ ! -d "$FRONTEND/node_modules" ]; then
    blue "==> 第一次运行，安装前端依赖"
    ( cd "$FRONTEND" && npm install --no-audit --no-fund )
    green "    node_modules 就绪"
  fi
}

do_start() {
  ensure_venv
  mkdir -p "$DATA" "$RUN"

  for p in "$API_PORT" "$UI_PORT"; do
    local pid; pid="$(port_pid "$p")"
    [ -n "$pid" ] && die "端口 $p 已被占用（PID $pid）。先跑 ./dev.sh stop，或设 CSD_DEV_API_PORT / CSD_DEV_UI_PORT 换一个。"
  done

  ensure_token
  ensure_access_token
  ensure_node_deps

  local token; token="$(tr -d '[:space:]' < "$DATA/dev-token")"
  local access; access="$(tr -d '[:space:]' < "$DATA/dev-access-token")"
  local api="http://127.0.0.1:$API_PORT"

  # agenest 默认是 native（本机 pi 进程），所以只需要一个 provider key。
  # 想接已有的 agent（nanobot 等）：在设置面板里切，或 CSD_AGENT_BACKEND=nanobot。
  local backend_kind="${CSD_AGENT_BACKEND:-native}"
  local workspace="${CSD_AGENT_WORKSPACE:-$DATA/agent-workspace}"
  mkdir -p "$workspace"

  # 授权给 native agent 的 ssh 私钥放哪（design.md §24）。默认是 $HOME/.ssh —— 部署时那是
  # agent 自己的 home，正合 ssh 的查找路径；但开发机上那是**你本人的** ~/.ssh，让开发服务器
  # 往里写 csd_* 私钥不合适，所以 dev 默认挪到 data/ 下，和 workspace 同样的理由。
  local key_dir="${CSD_AGENT_KEY_DIR:-$DATA/agent-keys}"
  mkdir -p "$key_dir"

  # provider key：优先用 .env，其次环境变量。native 后端没有 key 就是一个哑的 agent，
  # 而"为什么它不说话"是最难自己诊断的一类问题，所以这里明确检查并提示。
  local env_file="$ROOT/.env"
  if [ -f "$env_file" ]; then
    set -a; . "$env_file"; set +a
  fi
  if [ "$backend_kind" = "native" ] && [ -z "${DEEPSEEK_API_KEY:-}${ANTHROPIC_API_KEY:-}${OPENAI_API_KEY:-}${GEMINI_API_KEY:-}" ]; then
    yellow "    提示：没有找到任何 provider key（DEEPSEEK_API_KEY 等）。"
    yellow "    native agent 需要它才能说话 —— 写进 .env 或 export 后再 start。"
  fi

  blue "==> 启动后端 (127.0.0.1:$API_PORT)"

  # 环境变量用一个数组传，而不是写成 `VAR=value cmd` 的长链条。
  #
  # 原因：`${VAR:+VAR="$VAR"}` 在 `VAR=value cmd` 链条里会展开成一个**裸赋值**，
  # 而裸赋值在那个位置会被 bash 当成命令名 —— 报错是
  # `CSD_PI_WORKSPACE=...: No such file or directory`，看起来像路径不存在，
  # 实际是解析问题。数组形式没有这个歧义，加一个变量也不会牵动别的行。
  local -a envs=(
    "CSD_INGEST_TOKEN=$token"
    "CSD_DASHBOARD_TOKEN=$access"
    "CSD_AGENT_BACKEND=$backend_kind"
    "CSD_DB_PATH=$DATA/csd.sqlite3"
    "CSD_STATIC_DIR=$FRONTEND/dist"
    "CSD_HOST=127.0.0.1"
    "CSD_PORT=$API_PORT"
    "CSD_NANOBOT_WS_URL=${CSD_NANOBOT_WS_URL:-ws://127.0.0.1:8765/}"
    "CSD_NANOBOT_CLIENT_ID=${CSD_NANOBOT_CLIENT_ID:-csd-dashboard-dev}"
    "CSD_PI_WORKSPACE=$workspace"
    "CSD_PI_SESSION_DIR=$DATA/pi-sessions"
    "CSD_AGENT_KEY_DIR=$key_dir"
  )
  # 只在有值时传，避免把空串塞进去覆盖掉后端自己的默认值。
  local -a optional=(
    CSD_NANOBOT_TOKEN CSD_PI_PROVIDER CSD_PI_MODEL CSD_PI_BIN CSD_PI_TOOLS
    DEEPSEEK_API_KEY ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN OPENAI_API_KEY
    GEMINI_API_KEY GOOGLE_API_KEY OPENROUTER_API_KEY XAI_API_KEY GROQ_API_KEY
  )
  local name
  for name in "${optional[@]}"; do
    [ -n "${!name:-}" ] && envs+=("$name=${!name}")
  done

  # 注意 WorkingDirectory 必须是 backend —— `python -m app.main` 按 cwd 解析 app 包。
  # 这和 systemd 单元里的 WorkingDirectory 是同一个约束。
  #
  # 为什么要重定向全部三个 fd：被后台起的进程如果还持有调用方的 stdout，管道就一直不关闭，
  # 调用 `./dev.sh start` 的那个 shell 会被挂住（看起来像脚本卡死）。把 stdin/stdout/stderr
  # 全部指向日志，这个脚本才能正常返回。
  # 不要用 setsid —— Git Bash 里没有这个命令（Linux 有，Windows 没有）。
  ( cd "$BACKEND" && env "${envs[@]}" "$PY" -m app.main \
      > "$RUN/backend.log" 2>&1 < /dev/null & echo $! > "$RUN/backend.pid" )

  # `/api/sessions` 要带 CSD_DASHBOARD_TOKEN（就是上面 envs 里那个），所以 `curl -f` 会把
  # 401 当成"没起来"—— 健康的进程也过不了这一关。任何 HTTP 应答都说明端口在服务，401/403
  # 和 200 一样是好心跳；`000` 才是连不上。
  backend_code() { curl -s -o /dev/null -w '%{http_code}' "$api/api/sessions" 2>/dev/null; }

  for _ in $(seq 1 40); do
    [ "$(backend_code)" != "000" ] && break
    sleep 0.25
  done
  [ "$(backend_code)" != "000" ] \
    || { red "后端没起来，日志："; tail -20 "$RUN/backend.log"; die "启动失败"; }
  green "    后端就绪"

  # Vite dev server：热更新 + /api 代理。同样的 fd 处理，否则脚本挂住。
  blue "==> 启动前端 (127.0.0.1:$UI_PORT)"
  # `--host 127.0.0.1` 是必须的：Vite 默认绑 `[::1]`，而我们的健康检查和
  # `curl http://127.0.0.1:$UI_PORT` 走 IPv4。绑 `[::1]` 时检查会误判为"没起来"，
  # 端口检测也会漏掉它。
  ( cd "$FRONTEND" \
    && VITE_API_ORIGIN="$api" \
       npm run dev -- --host 127.0.0.1 --port "$UI_PORT" --strictPort \
         > "$RUN/frontend.log" 2>&1 < /dev/null & \
    echo $! > "$RUN/frontend.pid" )

  for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "http://127.0.0.1:$UI_PORT/" 2>/dev/null; then break; fi
    sleep 0.25
  done
  curl -sf -o /dev/null "http://127.0.0.1:$UI_PORT/" 2>/dev/null \
    && green "    前端就绪（热更新已开）" \
    || { red "前端没起来，日志："; tail -20 "$RUN/frontend.log"; die "启动失败"; }

  echo
  green "=============================================="
  green " 本机开发环境已启动"
  green "=============================================="
  echo
  echo "  界面（有热更新）   http://127.0.0.1:$UI_PORT"
  echo "  API（直接打）      $api"
  echo "  API 文档           $api/api/docs"
  echo "  数据库             $DATA/csd.sqlite3"
  echo
  echo "  agenest（第二个 tab）"
  echo "      后端           $backend_kind"
  if [ "$backend_kind" = "native" ]; then
    echo "      工作目录       $workspace"
    echo "      换到真实代码： CSD_AGENT_WORKSPACE=/your/code ./dev.sh restart"
  else
    echo "      gateway        ${CSD_NANOBOT_WS_URL:-ws://127.0.0.1:8765/}"
    echo "      没起 gateway 的话，Agent 页会直接说连不上，不影响 dashboard"
  fi
  echo
  echo "  打开（token 已填在 fragment 里，点开即用）："
  echo "        http://127.0.0.1:$UI_PORT/#token=$access"
  echo
  echo "  拿 token 给 cpctl 用："
  echo "        export CSD_BASE_URL=$api"
  echo "        export CSD_INGEST_TOKEN=\$(cat '$DATA/dev-token')"
  echo "        cd sdk/cpctl && PYTHONPATH=. ../backend/.venv/Scripts/python -m cpctl status"
  echo
  echo "  这些都只在本机，云端那台不受影响。"
  echo "  停：./dev.sh stop     日志：./dev.sh logs"
  echo
}

do_stop() {
  blue "==> 停止"
  local any=0
  # 按端口杀，pid 文件只用来兜底显示。
  for p in "$UI_PORT" "$API_PORT"; do
    if kill_port "$p"; then
      green "    端口 $p 上的进程已停"
      any=1
    fi
  done
  for name in backend frontend; do
    local pidfile="$RUN/$name.pid"
    if [ -f "$pidfile" ]; then
      local pid; pid="$(cat "$pidfile" 2>/dev/null)"
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
        green "    $name (pid $pid) 已停"
        any=1
      fi
      rm -f "$pidfile"
    fi
  done
  [ "$any" = "0" ] && echo "    本来就没在跑"
  return 0
}

do_status() {
  local api="http://127.0.0.1:$API_PORT"
  printf '  后端 %-5s ' "$(curl -sf -o /dev/null "$api/api/sessions" 2>/dev/null && echo 绿 || echo 停)"
  printf '前端 %-5s ' "$(curl -sf -o /dev/null "http://127.0.0.1:$UI_PORT/" 2>/dev/null && echo 绿 || echo 停)"
  if [ -f "$DATA/csd.sqlite3" ]; then
    printf '数据 %s' "$(du -h "$DATA/csd.sqlite3" 2>/dev/null | cut -f1)"
  else
    printf '数据 无'
  fi
  echo
}

do_reset() {
  red "这会删掉本机的 backend/data/（包括所有测试 session）。云端不受影响。"
  printf '  确认删除？输入 yes 继续: '
  read -r answer
  [ "$answer" = "yes" ] || { echo "已取消"; exit 0; }
  do_stop
  rm -rf "$DATA"
  green "已清空。下次 ./dev.sh start 会生成新 token。"
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  logs)    [ -f "$RUN/backend.log" ] && tail -f "$RUN/backend.log" || echo "还没有日志" ;;
  status)  do_status ;;
  token)   [ -f "$DATA/dev-token" ] && cat "$DATA/dev-token" || echo "还没有 token，先跑 ./dev.sh start" ;;
  access-token|agent-token) [ -f "$DATA/dev-access-token" ] && cat "$DATA/dev-access-token" || echo "还没有 access token，先跑 ./dev.sh start" ;;
  reset)   do_reset ;;
  *) die "用法: $0 {start|stop|restart|logs|status|token|access-token|reset}" ;;
esac