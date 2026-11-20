#!/usr/bin/env bash
# ============================================================
# m1 服务器发布脚本(工单 FZ-1 末尾 F 裁决,2026-11-19)
#
# 授权范围(改这个脚本 = 改授权范围,先把 diff 给 Silvia 看,她点头再跑):
#   只做 打包上传 → build 镜像 → 重建指定容器 → 核对版本和 health → 回滚。
#   不碰 env 文件;不读也不改 /srv/m1/data;不 push;容器名只允许 ombre-*。
#
# 连接信息不进 git(仓库是公开的):放在本机 ~/.ombre-m1-deploy,两行:
#   DEPLOY_HOST=<用户>@<服务器地址>
#   DEPLOY_KEY=<私钥路径>
#
# 用法(在 Ombre-Brain 仓库里):
#   bash deploy/m1_release.sh check <commit>           只检查:打包自检 + 服务器现状,不改任何东西
#   bash deploy/m1_release.sh run <commit> [容器...]   发布,默认 ombre-flint ombre-kieran ombre-rio
#   bash deploy/m1_release.sh rollback [容器...]       退回上一次发布前的镜像
# ============================================================
set -euo pipefail

CONF="${OMBRE_M1_DEPLOY_CONF:-$HOME/.ombre-m1-deploy}"
DEFAULT_CONTAINERS=(ombre-flint ombre-kieran ombre-rio)

die() { echo "❌ $*" >&2; exit 1; }
step() { echo; echo "== $* =="; }

usage() {
  sed -n '/^# 用法/,/^# =====/p' "$0" | sed 's/^# \{0,1\}//' | grep -v '^====='
  exit 2
}

# ------------------------------------------------------------ 连接配置
[ -f "$CONF" ] || die "缺连接配置 $CONF(两行 DEPLOY_HOST=... 和 DEPLOY_KEY=...)"
# 只取这两个键,不 source 整个文件
DEPLOY_HOST="$(sed -n 's/^DEPLOY_HOST=//p' "$CONF" | tr -d '\r' | head -1)"
DEPLOY_KEY="$(sed -n 's/^DEPLOY_KEY=//p' "$CONF" | tr -d '\r' | head -1)"
DEPLOY_KEY="${DEPLOY_KEY/#\~/$HOME}"
[ -n "$DEPLOY_HOST" ] || die "$CONF 里没有 DEPLOY_HOST"
[ -f "$DEPLOY_KEY" ] || die "私钥不存在: $DEPLOY_KEY"
SSH=(ssh -i "$DEPLOY_KEY" -o BatchMode=yes -o ConnectTimeout=15 "$DEPLOY_HOST")
SCP=(scp -q -i "$DEPLOY_KEY" -o BatchMode=yes -o ConnectTimeout=15)

cd "$(git rev-parse --show-toplevel)"

check_containers() {
  local c
  for c in "$@"; do
    [[ "$c" =~ ^ombre-[a-z0-9]+$ ]] || die "容器名不在授权范围内: $c(只允许 ombre-*)"
  done
}

# ------------------------------------------------------------ 本地打包 + 自检
package() {
  SHA="$(git rev-parse --verify --short=7 "$1^{commit}" 2>/dev/null)" || die "找不到 commit: $1"
  VER="$(git show "$SHA:VERSION" | tr -d '\r\n ')"
  [[ "$VER" =~ ^[0-9]+(\.[0-9]+)+$ ]] || die "VERSION 格式不对: $VER"
  [ "$(git show "$SHA:src/VERSION" | tr -d '\r\n ')" = "$VER" ] || die "VERSION 和 src/VERSION 不一致"
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  PKG="ombre-brain_${SHA}.tar.gz"
  # 必须关 autocrlf:本机 autocrlf=true 时 git archive 会把 LF 转成 CRLF
  git -c core.autocrlf=false archive --format=tar.gz --prefix=ombre-brain/ -o "$WORK/$PKG" "$SHA"
  [ "$(tar -xzOf "$WORK/$PKG" ombre-brain/VERSION | tr -d '\r\n ')" = "$VER" ] || die "包里的 VERSION 不是 $VER"
  if tar -xzOf "$WORK/$PKG" ombre-brain/entrypoint.sh | grep -q $'\r'; then
    die "包里的 entrypoint.sh 有 CRLF,容器会起不来"
  fi
  SUM="$(sha256sum "$WORK/$PKG" | cut -d' ' -f1)"
  echo "包: $PKG  版本 $VER  sha256 ${SUM:0:12}…  ✅ 自检通过"
}

# ------------------------------------------------------------ 服务器端(经 ssh 执行)
remote() {
  "${SSH[@]}" bash -s -- "$@" <<'REMOTE'
set -euo pipefail
MODE="$1"; shift
IMAGE=ombre-brain
D() { sudo -n docker "$@"; }
fail() { echo "❌ $*"; exit 1; }

# 核对:容器在跑、跑的是期望的镜像、镜像里 VERSION 对、health ok、代码不是卷内覆盖版
# 只看 docker 元数据、容器日志里的 code-state 行和 /health,不读 /srv/m1/data
verify() {
  local want_id="$1" want_ver="$2"; shift 2
  local c bad=0 st addr h img ver cs i
  for c in "$@"; do
    h=""
    for i in $(seq 1 45); do
      st="$(D inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
      addr="$(D port "$c" 2>/dev/null | head -1 | sed 's/.* -> //')"
      if [ "$st" = running ] && [ -n "$addr" ]; then
        h="$(curl -s -m 5 "http://$addr/health" || true)"
        case "$h" in *'"status":"ok"'*) break ;; esac
      fi
      sleep 2
    done
    img="$(D inspect -f '{{.Image}}' "$c" 2>/dev/null || echo none)"
    ver="$(D exec "$c" cat /app/VERSION 2>/dev/null | tr -d '\r\n ' || echo '?')"
    cs="$(D logs "$c" 2>&1 | grep -o 'code-state=[a-z-]*' | tail -1 || true)"
    local ok=1 why=""
    case "$h" in *'"status":"ok"'*) ;; *) ok=0; why="$why health 不 ok;" ;; esac
    [ -z "$want_id" ] || [ "$img" = "$want_id" ] || { ok=0; why="$why 跑的不是新镜像;"; }
    [ -z "$want_ver" ] || [ "$ver" = "$want_ver" ] || { ok=0; why="$why 版本是 $ver;"; }
    [ "$cs" != "code-state=runtime-override" ] || { ok=0; why="$why 卷内代码覆盖了镜像(可能自动回滚过);"; }
    if [ "$ok" = 1 ]; then
      echo "✅ $c  v$ver  ${cs:-code-state=?}  $h"
    else
      echo "❌ $c  v$ver  ${cs:-code-state=?} —$why"
      bad=1
    fi
  done
  return $bad
}

case "$MODE" in
  check)
    echo "ombre-brain:m1       = $(D image inspect -f '{{.Id}}' $IMAGE:m1 2>/dev/null | cut -c8-19 || echo 无)"
    echo "ombre-brain:rollback = $(D image inspect -f '{{.Id}}' $IMAGE:rollback 2>/dev/null | cut -c8-19 || echo 无)"
    echo "磁盘: $(df -h /srv/m1 | tail -1 | awk '{print $4" 可用 / 已用 "$5}')"
    verify "$(D image inspect -f '{{.Id}}' $IMAGE:m1 2>/dev/null || true)" "" "$@" || true
    ;;

  run)
    SHA="$1"; VER="$2"; SUM="$3"; shift 3
    PKG="ombre-brain_${SHA}.tar.gz"
    echo "$SUM  $HOME/$PKG" | sha256sum -c - >/dev/null || fail "服务器上的包 sha256 对不上"
    cd /srv/m1/build
    sudo -n install -m 644 "$HOME/$PKG" ./
    STAGE="$(sudo -n mktemp -d ./.stage-XXXXXX)"
    sudo -n tar -xzf "$PKG" -C "$STAGE"
    [ "$(sudo -n cat "$STAGE/ombre-brain/VERSION" | tr -d '\r\n ')" = "$VER" ] || fail "解压出来的 VERSION 不是 $VER"
    if [ -d ombre-brain ]; then
      sudo -n mv ombre-brain "ombre-brain.prev-$(date +%Y%m%d%H%M%S)"
    fi
    sudo -n mv "$STAGE/ombre-brain" ombre-brain
    sudo -n rmdir "$STAGE"
    echo "源码已换到 /srv/m1/build/ombre-brain(旧目录改名留着)"

    PREV_ID="$(D image inspect -f '{{.Id}}' $IMAGE:m1 2>/dev/null || true)"
    echo "开始 build(几分钟)…"
    if ! (cd ombre-brain && D build -t "$IMAGE:$VER-$SHA" -t $IMAGE:m1 . 2>&1) | sudo -n tee "build-$SHA.log" >/dev/null; then
      tail -20 "build-$SHA.log"
      fail "build 失败,没有重建任何容器。完整日志: /srv/m1/build/build-$SHA.log"
    fi
    NEW_ID="$(D image inspect -f '{{.Id}}' $IMAGE:m1)"
    echo "✅ build 完成: $IMAGE:$VER-$SHA"
    if [ -n "$PREV_ID" ] && [ "$PREV_ID" != "$NEW_ID" ]; then
      D tag "$PREV_ID" $IMAGE:rollback
      echo "上一版镜像已标为 $IMAGE:rollback(出问题用 rollback 退回)"
    fi

    cd /srv/m1
    sudo -n docker compose up -d --force-recreate "$@"
    echo "核对中(每台最多等 90 秒)…"
    verify "$NEW_ID" "$VER" "$@" || fail "有容器没通过核对。要退回就跑 rollback。"
    echo "✅ 发布完成: v$VER ($SHA) → $*"
    ;;

  rollback)
    RB_ID="$(D image inspect -f '{{.Id}}' $IMAGE:rollback 2>/dev/null)" || fail "没有 $IMAGE:rollback 镜像,退不了"
    CUR_ID="$(D image inspect -f '{{.Id}}' $IMAGE:m1)"
    [ "$RB_ID" != "$CUR_ID" ] || fail "rollback 和当前 m1 是同一个镜像,已经是退回状态"
    D tag "$CUR_ID" $IMAGE:rolled-back-from
    D tag "$RB_ID" $IMAGE:m1
    echo "m1 已指回上一版镜像(刚才那版标为 $IMAGE:rolled-back-from)"
    cd /srv/m1
    sudo -n docker compose up -d --force-recreate "$@"
    echo "核对中(每台最多等 90 秒)…"
    verify "$RB_ID" "" "$@" || fail "回滚后有容器没通过核对,需要人看"
    echo "✅ 回滚完成 → $*"
    ;;

  *) fail "未知模式: $MODE" ;;
esac
REMOTE
}

# ------------------------------------------------------------ 入口
MODE="${1:-}"
[ -n "$MODE" ] || usage
shift

case "$MODE" in
  check)
    [ $# -ge 1 ] || usage
    step "本地打包自检"
    package "$1"
    shift
    CONTAINERS=("${DEFAULT_CONTAINERS[@]}")
    [ $# -lt 1 ] || CONTAINERS=("$@")
    check_containers "${CONTAINERS[@]}"
    step "服务器现状(只读)"
    remote check "${CONTAINERS[@]}"
    ;;
  run)
    [ $# -ge 1 ] || usage
    step "本地打包自检"
    package "$1"
    shift
    CONTAINERS=("${DEFAULT_CONTAINERS[@]}")
    [ $# -lt 1 ] || CONTAINERS=("$@")
    check_containers "${CONTAINERS[@]}"
    echo "目标: v$VER ($SHA) → ${CONTAINERS[*]}"
    step "上传"
    "${SCP[@]}" "$WORK/$PKG" "$DEPLOY_HOST:"
    echo "✅ 已上传"
    step "服务器:build → 重建 → 核对"
    remote run "$SHA" "$VER" "$SUM" "${CONTAINERS[@]}"
    ;;
  rollback)
    CONTAINERS=("${DEFAULT_CONTAINERS[@]}")
    [ $# -lt 1 ] || CONTAINERS=("$@")
    check_containers "${CONTAINERS[@]}"
    step "服务器:回滚 → 核对"
    remote rollback "${CONTAINERS[@]}"
    ;;
  *) usage ;;
esac
