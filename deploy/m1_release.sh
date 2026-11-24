#!/usr/bin/env bash
# ============================================================
# m1 服务器发布脚本(工单 FZ-1 末尾 F 裁决,2026-11-19)
#
# 授权范围(改这个脚本 = 改授权范围,先把 diff 给 Silvia 看,她点头再跑):
#   只做 打包上传 → build 镜像 → 重建指定容器 → 核对版本和 health → 回滚。
#   不碰 env 文件;不读也不改 /srv/m1/data;不 push。
#   E-10(2026-11-24)起多一件:铺代码镜像 /srv/m1/code/<仓库名>(Ombre-Brain / chatnest-home),
#     chatnest 容器只读挂 /code 给 Flint 房间读代码。镜像 = 这次发布的同一个 git archive 包,
#     删掉 handoff/、full-stack/personas/、名叫 .env / *.env 的文件;有符号链接就停下不换;
#     写 MIRROR_VERSION(仓库、commit、发布时间 LA;不碰仓库自己的 VERSION);整目录替换(旧版删掉的文件镜像里也没了)。
#     run 核对通过后铺;rollback 后按退回那版的包重铺;mirror 模式只铺镜像。
#   两个项目(2026-11-19 E-6 起加 chatnest-home,Silvia 看过 diff 点头):
#     ombre    = 本仓库(Ombre-Brain),容器名只允许 ombre-*
#     chatnest = chatnest-home 仓库(默认 ~/Documents/chatnest-home,可用 CHATNEST_REPO 改),
#                容器只允许 chatnest-home 这一个
#
# 连接信息不进 git(仓库是公开的):放在本机 ~/.ombre-m1-deploy,两行:
#   DEPLOY_HOST=<用户>@<服务器地址>
#   DEPLOY_KEY=<私钥路径>
#
# 用法(在哪个目录跑都行;项目不写 = ombre):
#   bash deploy/m1_release.sh [chatnest] check <commit>           只检查:打包自检 + 服务器现状,不改任何东西
#   bash deploy/m1_release.sh [chatnest] run <commit> [容器...]   发布;ombre 默认 ombre-flint ombre-kieran ombre-rio,
#                                                                chatnest 只有 chatnest-home
#   bash deploy/m1_release.sh [chatnest] rollback [容器...]       退回上一次发布前的镜像
#   bash deploy/m1_release.sh [chatnest] build <commit>           只打包上传 + build 镜像(打 :<commit> 和 :m1 标签),
#                                                                不重建容器、不核对(M-2 搬家:新机器上先备好镜像)
#   bash deploy/m1_release.sh [chatnest] mirror <commit>          只打包上传 + 铺代码镜像 /srv/m1/code/<仓库名>,
#                                                                不 build、不重建(E-10:首次铺 / 补铺)
#
# 发往哪台机器:默认读 ~/.ombre-m1-deploy;设 OMBRE_M1_DEPLOY_CONF=<另一份配置> 就发往别的机器
# (M-2:~/.ombre-m2-deploy 指向 m2)。脚本开头会打印这次的目标机器。
# ============================================================
set -euo pipefail

CONF="${OMBRE_M1_DEPLOY_CONF:-$HOME/.ombre-m1-deploy}"

# ------------------------------------------------------------ 项目
PROJECT=ombre
if [ "${1:-}" = chatnest ] || [ "${1:-}" = ombre ]; then
  PROJECT="$1"
  shift
fi
case "$PROJECT" in
  ombre)
    REPO="$(cd "$(dirname "$0")" && git rev-parse --show-toplevel)"
    IMAGE=ombre-brain
    DEFAULT_CONTAINERS=(ombre-flint ombre-kieran ombre-rio)
    ;;
  chatnest)
    REPO="${CHATNEST_REPO:-$HOME/Documents/chatnest-home}"
    IMAGE=chatnest-home
    DEFAULT_CONTAINERS=(chatnest-home)
    ;;
esac

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
echo "目标机器: $DEPLOY_HOST(配置 $CONF)"
SCP=(scp -q -i "$DEPLOY_KEY" -o BatchMode=yes -o ConnectTimeout=15)

[ -d "$REPO" ] || die "找不到仓库: $REPO"
cd "$REPO"
cd "$(git rev-parse --show-toplevel)"
# 认仓库:防止在错的仓库里打包发到错的容器
case "$PROJECT" in
  ombre)    [ -f src/VERSION ] && [ -f entrypoint.sh ] || die "$PWD 不是 Ombre-Brain 仓库" ;;
  chatnest) [ -f full-stack/run.sh ] && [ -f Dockerfile ] || die "$PWD 不是 chatnest-home 仓库" ;;
esac

check_containers() {
  local c
  for c in "$@"; do
    case "$PROJECT" in
      ombre)    [[ "$c" =~ ^ombre-[a-z0-9]+$ ]] || die "容器名不在授权范围内: $c(ombre 只允许 ombre-*)" ;;
      chatnest) [ "$c" = chatnest-home ] || die "容器名不在授权范围内: $c(chatnest 只允许 chatnest-home)" ;;
    esac
  done
}

# 包里的这个文件有 CRLF 就停(本机 autocrlf=true 踩过:chatnest run.sh 带 CRLF 容器崩溃循环)
no_crlf() {
  if tar -xzOf "$WORK/$PKG" "$1" | grep -q $'\r'; then
    die "包里的 $1 有 CRLF,容器会起不来"
  fi
}

# ------------------------------------------------------------ 本地打包 + 自检
package() {
  SHA="$(git rev-parse --verify --short=7 "$1^{commit}" 2>/dev/null)" || die "找不到 commit: $1"
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  PKG="${IMAGE}_${SHA}.tar.gz"
  # 必须关 autocrlf:本机 autocrlf=true 时 git archive 会把 LF 转成 CRLF
  git -c core.autocrlf=false archive --format=tar.gz --prefix="$IMAGE/" -o "$WORK/$PKG" "$SHA"
  case "$PROJECT" in
    ombre)
      VER="$(git show "$SHA:VERSION" | tr -d '\r\n ')"
      [[ "$VER" =~ ^[0-9]+(\.[0-9]+)+$ ]] || die "VERSION 格式不对: $VER"
      [ "$(git show "$SHA:src/VERSION" | tr -d '\r\n ')" = "$VER" ] || die "VERSION 和 src/VERSION 不一致"
      [ "$(tar -xzOf "$WORK/$PKG" ombre-brain/VERSION | tr -d '\r\n ')" = "$VER" ] || die "包里的 VERSION 不是 $VER"
      no_crlf ombre-brain/entrypoint.sh
      ;;
    chatnest)
      # chatnest 没有 VERSION 文件,版本就是 commit;镜像打 chatnest-home:<commit> + :m1(和以前手动发布一样)
      VER="$SHA"
      tar -tzf "$WORK/$PKG" chatnest-home/Dockerfile >/dev/null 2>&1 || die "包里没有 Dockerfile"
      no_crlf chatnest-home/full-stack/run.sh
      no_crlf chatnest-home/Dockerfile
      ;;
  esac
  SUM="$(sha256sum "$WORK/$PKG" | cut -d' ' -f1)"
  echo "包: $PKG  版本 $VER  sha256 ${SUM:0:12}…  ✅ 自检通过"
  if ! git branch -r --contains "$SHA" 2>/dev/null | grep -q .; then
    echo "ℹ️  $SHA 还没 push 到远端(只是提示,不影响发布)"
  fi
}

# ------------------------------------------------------------ 服务器端(经 ssh 执行)
remote() {
  "${SSH[@]}" bash -s -- "$PROJECT" "$@" <<'REMOTE'
set -euo pipefail
KIND="$1"; MODE="$2"; shift 2
case "$KIND" in
  ombre)    IMAGE=ombre-brain;   MIRROR=Ombre-Brain ;;
  chatnest) IMAGE=chatnest-home; MIRROR=chatnest-home ;;
  *) echo "❌ 未知项目: $KIND"; exit 1 ;;
esac
D() { sudo -n docker "$@"; }
fail() { echo "❌ $*"; exit 1; }
img_short() { local id; if id="$(D image inspect -f '{{.Id}}' "$1" 2>/dev/null)"; then echo "${id:7:12}"; else echo 无; fi; }

# ---- E-10 代码镜像:/srv/m1/code/<仓库名>,chatnest 容器只读挂 /code
# (E10_MIRROR_ROOT 只给本机测试用;经 ssh 跑时没有这个变量,永远是 /srv/m1/code)
MIRROR_ROOT="${E10_MIRROR_ROOT:-/srv/m1/code}"
# 镜像里不放的:handoff/、chatnest 的 personas/(别人的人设)、名叫 .env / *.env 的文件
mirror_excludes() {
  echo handoff
  [ "$KIND" = chatnest ] && echo full-stack/personas
  return 0
}

# mirror_from_pkg <包路径> <commit> <版本>:整目录替换。失败只影响镜像,不动容器。
mirror_from_pkg() {
  local pkg="$1" sha="$2" ver="$3" new old ex links left
  # 注意:这个函数在 `… || fail` 里调用,bash 会关掉函数里的 set -e,所以每一步自己判失败
  [ -f "$pkg" ] || { echo "❌ 找不到包 $pkg,镜像没换"; return 1; }
  new="$MIRROR_ROOT/.new-$MIRROR"
  old="$MIRROR_ROOT/.prev-$MIRROR"
  sudo -n mkdir -p "$MIRROR_ROOT" || { echo "❌ 建不了 $MIRROR_ROOT,镜像没换"; return 1; }
  sudo -n rm -rf -- "$new" "$old" && sudo -n mkdir "$new" || { echo "❌ 建不了临时目录,镜像没换"; return 1; }
  if ! sudo -n tar -xzf "$pkg" -C "$new" --strip-components=1 --no-same-owner; then
    sudo -n rm -rf -- "$new"; echo "❌ 解压失败,镜像没换"; return 1
  fi
  for ex in $(mirror_excludes); do sudo -n rm -rf -- "$new/$ex"; done
  sudo -n find "$new" \( -name .env -o -name '*.env' \) -type f -delete
  links="$(sudo -n find "$new" -type l | sed "s#^$new/##" | head -20)"
  if [ -n "$links" ]; then
    sudo -n rm -rf -- "$new"
    echo "❌ 包里有符号链接,镜像没换(线上镜像还是旧的):"; echo "$links" | sed 's/^/   /'
    return 1
  fi
  for ex in $(mirror_excludes); do
    if sudo -n test -e "$new/$ex"; then sudo -n rm -rf -- "$new"; echo "❌ $ex 没删掉,镜像没换"; return 1; fi
  done
  left="$(sudo -n find "$new" \( -name .env -o -name '*.env' \) | head -3)"
  if [ -n "$left" ]; then sudo -n rm -rf -- "$new"; echo "❌ 还有 env 文件,镜像没换"; return 1; fi
  # 标记文件叫 MIRROR_VERSION,不碰仓库自己的 VERSION(F 2026-11-24:镜像里的代码和 commit 一字不差)
  if sudo -n test -e "$new/MIRROR_VERSION"; then
    sudo -n rm -rf -- "$new"; echo "❌ 仓库里自己就有 MIRROR_VERSION,不覆盖,镜像没换"; return 1
  fi
  if [ -z "$ver" ]; then
    if [ "$KIND" = ombre ]; then ver="$(sudo -n cat "$new/VERSION" 2>/dev/null | head -1 | tr -d '\r\n ')"; else ver="$sha"; fi
  fi
  if ! printf 'repo=%s\ncommit=%s\nversion=%s\npublished_at=%s\n' \
    "$MIRROR" "$sha" "$ver" "$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M:%S %Z')" \
    | sudo -n tee "$new/MIRROR_VERSION" >/dev/null; then
    sudo -n rm -rf -- "$new"; echo "❌ 写不了 MIRROR_VERSION,镜像没换"; return 1
  fi
  sudo -n chmod -R u+rwX,go+rX,go-w "$new" || { sudo -n rm -rf -- "$new"; echo "❌ 改权限失败,镜像没换"; return 1; }
  if sudo -n test -e "$MIRROR_ROOT/$MIRROR"; then
    sudo -n mv -- "$MIRROR_ROOT/$MIRROR" "$old" || { sudo -n rm -rf -- "$new"; echo "❌ 挪不开旧镜像,镜像没换"; return 1; }
  fi
  if ! sudo -n mv -- "$new" "$MIRROR_ROOT/$MIRROR"; then
    sudo -n test -e "$old" && sudo -n mv -- "$old" "$MIRROR_ROOT/$MIRROR"
    echo "❌ 新镜像换不上,已放回旧的"; return 1
  fi
  sudo -n rm -rf -- "$old"
  echo "✅ 代码镜像 $MIRROR_ROOT/$MIRROR → $sha($(sudo -n find "$MIRROR_ROOT/$MIRROR" -type f | wc -l) 个文件)"
}

mirror_state() {
  if sudo -n test -f "$MIRROR_ROOT/$MIRROR/MIRROR_VERSION"; then
    echo "代码镜像 $MIRROR_ROOT/$MIRROR: $(sudo -n sed -n 's/^commit=//p; s/^published_at=/发布于 /p' "$MIRROR_ROOT/$MIRROR/MIRROR_VERSION" | paste -sd' ')"
  else
    echo "代码镜像 $MIRROR_ROOT/$MIRROR: 无"
  fi
}

# 退回那版镜像对应的 commit(从它的标签里认:ombre 是 <版本>-<commit>,chatnest 是 <commit>)
image_commit() {
  D image inspect -f '{{join .RepoTags "\n"}}' "$1" 2>/dev/null \
    | sed -n "s#^$IMAGE:\([0-9.]*-\)\{0,1\}\([0-9a-f]\{7\}\)\$#\2#p" | head -1
}

# chatnest 核对:容器在跑、跑的是期望的镜像、docker 自己的健康检查 healthy、首页 200。
# 只看 docker 元数据和首页,不读 /srv/m1/data。健康检查 30 秒一次,每台最多等 150 秒。
verify_chatnest() {
  local want_id="$1"; shift
  local c bad=0 st hs addr code img i
  for c in "$@"; do
    for i in $(seq 1 75); do
      st="$(D inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
      hs="$(D inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null || echo missing)"
      [ "$st" = running ] && [ "$hs" = healthy ] && break
      [ "$hs" = unhealthy ] && break
      sleep 2
    done
    addr="$(D port "$c" 2>/dev/null | head -1 | sed 's/.* -> //')"
    code="$( [ -n "$addr" ] && curl -s -o /dev/null -m 5 -w '%{http_code}' "http://$addr/" || echo 000)"
    img="$(D inspect -f '{{.Image}}' "$c" 2>/dev/null || echo none)"
    local ok=1 why=""
    [ "$st" = running ] || { ok=0; why="$why 状态 $st;"; }
    [ "$hs" = healthy ] || { ok=0; why="$why 健康检查 $hs;"; }
    [ "$code" = 200 ] || { ok=0; why="$why 首页 $code;"; }
    [ -z "$want_id" ] || [ "$img" = "$want_id" ] || { ok=0; why="$why 跑的不是新镜像;"; }
    if [ "$ok" = 1 ]; then
      echo "✅ $c  镜像 ${img:7:12}  $hs  首页 $code"
    else
      echo "❌ $c  镜像 ${img:7:12} —$why"
      bad=1
    fi
  done
  return $bad
}

# ombre 核对:容器在跑、跑的是期望的镜像、镜像里 VERSION 对、health ok、代码不是卷内覆盖版
# 只看 docker 元数据、容器日志里的 code-state 行和 /health,不读 /srv/m1/data
verify() {
  if [ "$KIND" = chatnest ]; then
    local want="$1"; shift 2
    verify_chatnest "$want" "$@"
    return
  fi
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
    echo "$IMAGE:m1       = $(img_short $IMAGE:m1)  commit $(image_commit $IMAGE:m1 || true)"
    echo "$IMAGE:rollback = $(img_short $IMAGE:rollback)  commit $(image_commit $IMAGE:rollback || true)"
    echo "磁盘: $(df -h /srv/m1 | tail -1 | awk '{print $4" 可用 / 已用 "$5}')"
    mirror_state
    verify "$(D image inspect -f '{{.Id}}' $IMAGE:m1 2>/dev/null || true)" "" "$@" || true
    ;;

  run|build)
    SHA="$1"; VER="$2"; SUM="$3"; shift 3
    PKG="${IMAGE}_${SHA}.tar.gz"
    if [ "$KIND" = chatnest ]; then TAG="$SHA"; LOG="chatnest-home.build.log.$SHA"; else TAG="$VER-$SHA"; LOG="build-$SHA.log"; fi
    echo "$SUM  $HOME/$PKG" | sha256sum -c - >/dev/null || fail "服务器上的包 sha256 对不上"
    cd /srv/m1/build
    sudo -n install -m 644 "$HOME/$PKG" ./
    STAGE="$(sudo -n mktemp -d ./.stage-XXXXXX)"
    sudo -n tar -xzf "$PKG" -C "$STAGE"
    if [ "$KIND" = ombre ]; then
      [ "$(sudo -n cat "$STAGE/ombre-brain/VERSION" | tr -d '\r\n ')" = "$VER" ] || fail "解压出来的 VERSION 不是 $VER"
    else
      sudo -n test -f "$STAGE/chatnest-home/Dockerfile" || fail "解压出来没有 Dockerfile"
    fi
    if [ -d "$IMAGE" ]; then
      sudo -n mv "$IMAGE" "$IMAGE.prev-$(date +%Y%m%d%H%M%S)"
    fi
    sudo -n mv "$STAGE/$IMAGE" "$IMAGE"
    sudo -n rmdir "$STAGE"
    echo "源码已换到 /srv/m1/build/$IMAGE(旧目录改名留着)"

    PREV_ID="$(D image inspect -f '{{.Id}}' $IMAGE:m1 2>/dev/null || true)"
    # 同一个 commit 再 build 一次(比如只改了 env、要重建生效)::<commit> 和 :m1 两个标签都会挪到
    # 新镜像上,旧镜像一个标签都不剩,按 ID 也找不到了,后面给它打 rollback 就失败、整个脚本在重建前
    # 停下(2026-11-20 chatnest 5c82f97 踩到)。所以 build 前先给旧镜像挂一个临时标签保住它;
    # 同一个 commit 的话 rollback 标签不动(还指着上一个 commit 那版,退回才有意义)。
    SAME_COMMIT=0
    if [ -n "$PREV_ID" ] && [ "$(D image inspect -f '{{.Id}}' "$IMAGE:$TAG" 2>/dev/null || true)" = "$PREV_ID" ]; then
      SAME_COMMIT=1
    fi
    [ -z "$PREV_ID" ] || D tag "$PREV_ID" "$IMAGE:prev-build"
    echo "开始 build(几分钟)…"
    if ! (cd "$IMAGE" && D build -t "$IMAGE:$TAG" -t $IMAGE:m1 . 2>&1) | sudo -n tee "$LOG" >/dev/null; then
      tail -20 "$LOG"
      fail "build 失败,没有重建任何容器。完整日志: /srv/m1/build/$LOG"
    fi
    NEW_ID="$(D image inspect -f '{{.Id}}' $IMAGE:m1)"
    echo "✅ build 完成: $IMAGE:$TAG"
    if [ -n "$PREV_ID" ] && [ "$PREV_ID" != "$NEW_ID" ]; then
      if [ "$SAME_COMMIT" = 1 ]; then
        echo "同一个 commit 重新 build:rollback 标签不动(还是上一个 commit 那版:$(img_short $IMAGE:rollback))"
      else
        D tag "$IMAGE:prev-build" $IMAGE:rollback
        echo "上一版镜像已标为 $IMAGE:rollback(出问题用 rollback 退回)"
      fi
    fi

    if [ "$MODE" = build ]; then
      [ -z "$PREV_ID" ] || D rmi "$IMAGE:prev-build" >/dev/null 2>&1 || true
      echo "✅ 只 build:$IMAGE:$TAG(= $IMAGE:m1,$(img_short $IMAGE:m1))。没有重建任何容器。"
      exit 0
    fi
    cd /srv/m1
    sudo -n docker compose up -d --force-recreate "$@"
    # 临时标签用完摘掉(只摘标签;rollback 指着的镜像不受影响)
    [ -z "$PREV_ID" ] || D rmi "$IMAGE:prev-build" >/dev/null 2>&1 || true
    echo "核对中(ombre 每台最多等 90 秒,chatnest 最多 150 秒)…"
    verify "$NEW_ID" "$VER" "$@" || fail "有容器没通过核对。要退回就跑 rollback。(代码镜像没动)"
    echo "✅ 发布完成: v$VER ($SHA) → $*"
    mirror_from_pkg "/srv/m1/build/$PKG" "$SHA" "$([ "$KIND" = ombre ] && echo "$VER" || echo "$SHA")" \
      || fail "发布本身已完成,但代码镜像没更新(线上容器不受影响)。修好后用 mirror $SHA 补铺。"
    ;;

  mirror)
    SHA="$1"; VER="$2"; SUM="$3"
    PKG="${IMAGE}_${SHA}.tar.gz"
    echo "$SUM  $HOME/$PKG" | sha256sum -c - >/dev/null || fail "服务器上的包 sha256 对不上"
    sudo -n install -m 644 "$HOME/$PKG" /srv/m1/build/
    mirror_from_pkg "/srv/m1/build/$PKG" "$SHA" "$([ "$KIND" = ombre ] && echo "$VER" || echo "$SHA")" \
      || fail "代码镜像没换"
    echo "没有 build、没有重建任何容器。"
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
    echo "核对中(ombre 每台最多等 90 秒,chatnest 最多 150 秒)…"
    verify "$RB_ID" "" "$@" || fail "回滚后有容器没通过核对,需要人看(代码镜像没动)"
    echo "✅ 回滚完成 → $*"
    # 代码镜像跟着退:按退回那版镜像标签里的 commit 找当时发布用的包重铺
    RB_SHA="$(image_commit "$RB_ID")"
    if [ -z "$RB_SHA" ]; then
      fail "回滚已完成,但认不出退回那版的 commit,代码镜像没跟着退。用 mirror <旧 commit> 补铺。"
    fi
    mirror_from_pkg "/srv/m1/build/${IMAGE}_${RB_SHA}.tar.gz" "$RB_SHA" "" \
      || fail "回滚已完成,但代码镜像没跟着退到 $RB_SHA。用 mirror $RB_SHA 补铺。"
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
  build)
    [ $# -eq 1 ] || usage
    step "本地打包自检"
    package "$1"
    echo "目标: v$VER ($SHA) → 只 build 镜像,不重建容器"
    step "上传"
    "${SCP[@]}" "$WORK/$PKG" "$DEPLOY_HOST:"
    echo "✅ 已上传"
    step "服务器:只 build"
    remote build "$SHA" "$VER" "$SUM"
    ;;
  mirror)
    [ $# -eq 1 ] || usage
    step "本地打包自检"
    package "$1"
    echo "目标: $SHA → 只铺代码镜像,不 build、不重建容器"
    step "上传"
    "${SCP[@]}" "$WORK/$PKG" "$DEPLOY_HOST:"
    echo "✅ 已上传"
    step "服务器:铺代码镜像"
    remote mirror "$SHA" "$VER" "$SUM"
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
