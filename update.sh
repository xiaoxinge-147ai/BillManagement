#!/usr/bin/env bash
#
# 拉取最新代码并重建 Docker 容器。
#
# 用法：
#   ./update.sh                 拉取当前分支最新代码，有变化才重建（跑完即退出）
#   ./update.sh -f              强制重建，不管有没有新提交
#   ./update.sh -b dev          拉取指定分支
#   ./update.sh --follow        重建完跟日志（Ctrl+C 只退出日志，不影响容器）
#   ./update.sh -h              看用法
#
# 设计要点：
#   * 容器用 docker compose up -d 后台常驻，restart: always 保证开机/崩溃自动拉起。
#   * 默认不在结尾跟日志，否则会占住终端，不适合定时任务/无人值守。
#   * .env 绝不覆盖 —— 它是服务器本地配置，不进 git。重建前后校验指纹。
#   * 没有新提交时直接退出，不做无谓的镜像重建。
#   * 用 git pull --ff-only，宁可失败也不产生意外的合并提交。
#   * 工作区有改动时：能自动 stash 的就 stash（下次跑自动恢复），
#     自动放不下的（如 stash 冲突）才报错停下。见下方 AUTO_STASH 说明。
#   * 数据库在 pgdata 命名卷里，compose 已写死项目名 name: billmanagement，
#     重建容器不会丢数据，也不会因为目录改名而挂到空卷上。
#
set -euo pipefail
cd "$(dirname "$0")"

FORCE=0
FOLLOW=0
BRANCH=""
NO_STASH=0

while [ $# -gt 0 ]; do
  case "$1" in
    -f|--force)     FORCE=1; shift;;
    -b|--branch)    BRANCH="$2"; shift 2;;
    --follow|-l)    FOLLOW=1; shift;;
    --no-stash)     NO_STASH=1; shift;;   # 不自动 stash，有改动直接报错
    -h|--help)      sed -n '3,22p' "$0" | sed 's/^# \?//'; exit 0;;
    *) echo "未知参数: $1（-h 看用法）" >&2; exit 2;;
  esac
done

step() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 前置检查 ----------
step "检查运行环境"

command -v git >/dev/null 2>&1 || die "没装 git"
git rev-parse --git-dir >/dev/null 2>&1 || \
  die "当前目录不是 git 仓库。请按部署说明把服务器目录转成 git 仓库（见 README「服务器部署」）"

DOCKER=""
if docker compose version >/dev/null 2>&1; then
  DOCKER="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DOCKER="docker-compose"
else
  die "找不到 docker compose，请先安装 Docker"
fi
echo "  git      : $(git --version)"
echo "  compose  : $DOCKER"

# .env 是本地配置，不该被 git 跟踪
if [ -f .env ]; then
  if git check-ignore -q .env; then
    echo "  .env     : 已存在（被 .gitignore 忽略，不会被覆盖）"
  else
    warn ".env 没有被 .gitignore 忽略！请确认它没被提交过，否则重建后配置可能被冲掉"
  fi
else
  warn ".env 不存在 —— 将使用默认配置（SQLite 落在 ./data/bill.db、端口 8000）"
  warn "生产环境请 cp .env.example .env 并填好 POSTGRES_PASSWORD / APP_PASSWORD / CRYPTO_SECRET"
fi

ENV_BEFORE=""
command -v shasum >/dev/null 2>&1 && [ -f .env ] && ENV_BEFORE=$(shasum .env | awk '{print $1}')

# ---------- 拉取 ----------
step "拉取最新代码"

if [ -n "$BRANCH" ]; then
  git checkout "$BRANCH" 2>/dev/null || die "切换分支 $BRANCH 失败"
fi
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "  当前分支 : $CURRENT_BRANCH"

# 手动拷代码过来的服务器，工作区常常是「脏」的（文件与仓库版本有差异）。
# 默认自动 stash 掉，拉完再恢复，避免每次都卡在「请先处理这些改动」。
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
  if [ "$NO_STASH" -eq 1 ]; then
    warn "工作区有未提交的本地改动（--no-stash 指定不自动处理）："
    git status --short | sed 's/^/    /'
    die "请先处理这些改动（git stash / git checkout -- .）再更新"
  fi
  warn "工作区有未提交的本地改动，先自动暂存（git stash）："
  git status --short | sed 's/^/    /'
  if git stash push -u -m "update.sh 自动暂存 $(date +%F\ %T)" >/dev/null 2>&1; then
    STASHED=1
    echo "  ✓ 已暂存，拉取完成后会自动恢复"
  else
    die "自动暂存失败，请手动处理上面的改动后重试"
  fi
fi

BEFORE=$(git rev-parse HEAD)
echo "  更新前   : $(git log -1 --format='%h %s' HEAD)"

git fetch --prune origin

if ! git rev-parse --verify -q "origin/$CURRENT_BRANCH" >/dev/null; then
  die "远程没有 origin/$CURRENT_BRANCH 分支"
fi

CHANGED=1
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$CURRENT_BRANCH")" ]; then
  CHANGED=0
  if [ "$FORCE" -eq 0 ]; then
    step "已经是最新版本，无需更新"
    echo "  当前 : $(git log -1 --format='%h %s')"
    [ "$STASHED" -eq 1 ] && { git stash pop >/dev/null 2>&1 && echo "  本地改动已恢复 ✓"; }
    echo
    echo "  如需强制重建镜像，执行：  ./update.sh -f"
    exit 0
  fi
  echo "  已是最新，但 -f 指定强制重建"
fi

# 未跟踪文件挡住 pull 时的兜底：备份后移开，拉取完再放回。
CLASH=$(comm -12 \
  <(git ls-files --others --exclude-standard | sort) \
  <(git diff --name-only --diff-filter=A "HEAD..origin/$CURRENT_BRANCH" | sort) || true)

BACKUP_DIR=""
if [ -n "$CLASH" ]; then
  BACKUP_DIR=".update-backup-$(date +%Y%m%d%H%M%S)"
  mkdir -p "$BACKUP_DIR"
  warn "以下未跟踪文件会被新版本覆盖，已备份并暂时移开："
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    echo "    $f"
    mkdir -p "$BACKUP_DIR/$(dirname "$f")"
    mv "$f" "$BACKUP_DIR/$f"
  done <<< "$CLASH"
fi

if [ "$CHANGED" -eq 1 ]; then
  if ! git pull --ff-only origin "$CURRENT_BRANCH"; then
    if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
      warn "拉取失败，正在还原被移开的文件…"
      (cd "$BACKUP_DIR" && find . -type f | while IFS= read -r f; do
        mkdir -p "../$(dirname "$f")" 2>/dev/null || true
        mv "$f" "../$f" 2>/dev/null || true
      done)
      rm -rf "$BACKUP_DIR"
    fi
    [ "$STASHED" -eq 1 ] && git stash pop >/dev/null 2>&1 || true
    die "git pull 失败，请查看上面的错误信息"
  fi

  if [ -n "$BACKUP_DIR" ]; then
    warn "这些文件的新版本已由 git 检出；你原来的本地版本留在 $BACKUP_DIR/ 里"
    echo "      确认没问题后可删： rm -rf $BACKUP_DIR"
  fi

  AFTER=$(git rev-parse HEAD)
  echo "  更新后   : $(git log -1 --format='%h %s' HEAD)"
  echo
  echo "  本次新增提交："
  git log --oneline --no-decorate "$BEFORE..$AFTER" | sed 's/^/    /'
fi

# ---------- 恢复本地改动 ----------
if [ "$STASHED" -eq 1 ]; then
  step "恢复本地改动"
  if git stash pop >/dev/null 2>&1; then
    echo "  ✓ 已恢复（这些改动是服务器上的本地状态，未提交）"
  else
    warn "本地改动自动恢复失败（可能与新代码冲突）。它仍在 stash 里："
    echo "      git stash list      # 查看"
    echo "      git stash pop       # 手动恢复"
    echo "  本次不会重建容器，请先处理冲突。"
    exit 1
  fi
fi

# ---------- 重建 ----------
step "重建并重启容器"
$DOCKER up -d --build

# ---------- 校验 ----------
step "校验"

if [ -n "$ENV_BEFORE" ]; then
  ENV_AFTER=$(shasum .env | awk '{print $1}')
  if [ "$ENV_BEFORE" = "$ENV_AFTER" ]; then
    echo "  .env 未被改动 ✓"
  else
    warn ".env 内容发生了变化，请检查是否被意外覆盖"
  fi
fi

$DOCKER ps

# 健康检查：/health 在 app.auth.PUBLIC_PATHS 里，免登录
PORT=$(grep -E '^APP_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' "'"'"'')
PORT="${PORT:-8000}"
step "等待服务就绪（端口 $PORT）"
READY=0
for i in $(seq 1 30); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)
  if [ "$CODE" = "200" ]; then
    echo "  服务已就绪： http://127.0.0.1:${PORT}/health ✓"
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" -eq 0 ]; then
  warn "等待超时（30 秒）。可能是端口不是 $PORT，或服务启动失败。"
  echo "  看日志排查： $DOCKER logs --tail=80 app"
else
  echo
  gmip() { ipconfig getifaddr "$1" 2>/dev/null || true; }
  IP=$(gmip en0 || gmip en1 || ipconfig getifaddr en0 2>/dev/null || echo "")
  [ -n "$IP" ] && echo "  局域网访问： http://$IP:${PORT}"
fi

if [ "$FOLLOW" -eq 1 ]; then
  step "跟日志（Ctrl+C 只退出日志，容器继续在后台运行）"
  $DOCKER logs --tail=50 -f app
else
  step "完成"
  echo "  服务已在后台常驻运行。"
  echo
  echo "  查看日志： $DOCKER logs -f --tail=100 app"
  echo "  查看状态： $DOCKER ps"
  echo "  重启服务： $DOCKER restart"
  echo "  停止服务： $DOCKER down"
  echo
  echo "  想重建完直接跟日志，下次加 --follow。"
fi
