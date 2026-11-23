#!/usr/bin/env bash
# =============================================================================
#  Hermes obuna plugini — o'rnatish skripti (Hermes Agent v0.23.x, bitta Telegram bot)
#
#  Nima qiladi (har bir o'zgartiruvchi qadamdan oldin tasdiq so'raydi):
#    1. preflight  — faqat tekshiradi, hech narsani o'zgartirmaydi
#    2. backup     — config.yaml, .env, pairing, plugin va profil zaxirasi (~/hermes-subs-backups/<vaqt>)
#    3. install    — "subscribers" profilini yaratadi va sozlaydi, pluginni ikkala profilga o'rnatib yoqadi,
#                    route'larni yozadi (hammasi `hermes config set` orqali — izohlar saqlanadi)
#    4. activate   — `hermes gateway migrate --multiplex` (gateway BIR MARTA qayta ishga tushadi)
#    5. timers     — pairing tozalash (10 daqiqada) va kunlik zaxira uchun systemd user timer'lar
#
#  Tegmaydi: AI_SCALPING_PRO va ai-scalping-pro.service, mavjud SOUL.md, mavjud skill'lar va xotira.
#
#  Foydalanish (ubuntu foydalanuvchisi, sudo'siz):
#    bash install.sh --admin-id 123456789 --dry-run     # faqat nima qilinishini ko'rsatadi
#    bash install.sh --admin-id 123456789               # bosqichma-bosqich, har birida tasdiq
#    bash install.sh --admin-id 123456789 --step preflight
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_PLUGIN="$(cd "$SCRIPT_DIR/../subscriptions" && pwd)"
SRC_TESTS="$(cd "$SCRIPT_DIR/../tests" && pwd)"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PROFILE="subscribers"
SUB_HOME="$HERMES_HOME_DIR/profiles/$PROFILE"
DB_PATH="$HOME/.local/share/hermes-subscriptions/subs.db"
MODEL="gpt-5.4-mini"
PROVIDER="openai"
DAILY_LIMIT=50
ADMIN_ID=""
ASSUME_YES=0
DRY_RUN=0
STEP="all"
HERMES_BIN="$(command -v hermes 2>/dev/null || echo "$HOME/.local/bin/hermes")"
HPY="$HERMES_HOME_DIR/hermes-agent/venv/bin/python"
BACKUP_ROOT="$HOME/hermes-subs-backups"
BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
UNIT_DIR="$HOME/.config/systemd/user"
MARKER=".installed-by-hermes-subscriptions"
SUB_TOOLSETS='["web","vision","clarify","todo"]'
SUB_DISABLED='["terminal","file","code_execution","computer_use","skills","cronjob","memory","delegation","session_search","browser"]'

usage() {
  cat <<'EOF'
Foydalanish: bash install.sh --admin-id <TELEGRAM_ID> [--dry-run] [--yes] [--step NOM]
  --admin-id N   Administrator Telegram ID (majburiy)
  --dry-run      Hech narsa o'zgartirmaydi, faqat buyruqlarni ko'rsatadi
  --yes          Tasdiq so'ramaydi (tavsiya etilmaydi)
  --step NOM     preflight | backup | install | activate | timers | all (standart: all)
  --model M      Obunachilar modeli (standart: gpt-5.4-mini)
  --limit N      Kunlik xabar limiti (standart: 50)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --admin-id) ADMIN_ID="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --step) STEP="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --limit) DAILY_LIMIT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Noma'lum parametr: $1" >&2; usage; exit 2 ;;
  esac
done

log()  { printf '%s\n' "$*"; }
step() { printf '\n==================== %s ====================\n' "$*"; }
die()  { printf '\nXATO: %s\nHech qanday keyingi o'"'"'zgarish qilinmadi.\n' "$*" >&2; exit 1; }
trap 'die "kutilmagan xato (qator $LINENO). Kerak bo'"'"'lsa: bash rollback.sh"' ERR

run() {
  if (( DRY_RUN )); then printf '  [dry-run] %q' "$1"; shift; printf ' %q' "$@"; printf '\n'; return 0; fi
  printf '  + %s\n' "$*"
  "$@"
}

confirm() {
  (( DRY_RUN || ASSUME_YES )) && return 0
  local answer
  read -r -p "$1 Davom etamizmi? [ha/yo'q]: " answer
  [[ "$answer" == "ha" ]] || die "foydalanuvchi bekor qildi"
}

hermes()     { "$HERMES_BIN" "$@"; }
cfg_default() { run "$HERMES_BIN" config set "$1" "$2"; }
cfg_sub()     { run "$HERMES_BIN" -p "$PROFILE" config set "$1" "$2"; }

# `hermes config get KEY --json`: muvaffaqiyatli bo'lsa JSON, aks holda bo'sh satr (xato matni JSON deb olinmaydi).
cfg_get_json() {
  local out
  if out="$("$HERMES_BIN" config get "$1" --json 2>/dev/null)"; then printf '%s' "$out"; fi
  return 0
}

env_names() { grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$1" 2>/dev/null | tr -d '=' || true; }

# ----------------------------------------------------------------------------- 1. preflight
preflight() {
  step "1. TEKSHIRUV (hech narsa o'zgarmaydi)"
  [[ $EUID -ne 0 ]] || die "root sifatida ishga tushirmang — 'ubuntu' foydalanuvchisi bilan ishga tushiring"
  [[ "$ADMIN_ID" =~ ^[1-9][0-9]{4,14}$ ]] || die "--admin-id noto'g'ri (Telegram ID raqam bo'lishi kerak)"
  [[ "$DAILY_LIMIT" =~ ^[0-9]{1,6}$ ]] || die "--limit noto'g'ri"
  [[ "$MODEL" =~ ^[A-Za-z0-9._:/-]{1,64}$ ]] || die "--model noto'g'ri"
  [[ -x "$HERMES_BIN" ]] || die "hermes topilmadi ($HERMES_BIN)"
  [[ -f "$HERMES_HOME_DIR/config.yaml" && -f "$HERMES_HOME_DIR/.env" ]] || die "$HERMES_HOME_DIR/config.yaml yoki .env topilmadi"
  [[ -x "$HPY" ]] || die "Hermes Python topilmadi ($HPY)"
  for f in __init__.py plugin.yaml service.py gate.py admin_tools.py telegram_ui.py cli.py; do
    [[ -f "$SRC_PLUGIN/$f" ]] || die "plugin fayli yo'q: $SRC_PLUGIN/$f"
  done

  local version; version="$(hermes --version 2>&1 | head -n1 || true)"
  log "Hermes: $version"
  [[ "$version" == *"0.23."* ]] || die "Hermes v0.23.x kutilgan. Boshqa versiyada sinovdan o'tmagan."

  local pyinfo; pyinfo="$("$HPY" -c 'import sys, telegram; print(sys.version.split()[0], telegram.__version__)' 2>&1)" \
    || die "Hermes venv'da python-telegram-bot topilmadi: $pyinfo"
  log "Python / python-telegram-bot: $pyinfo"
  [[ "$pyinfo" == 3.1[1-4].*" 22."* ]] || die "Python 3.11–3.14 va python-telegram-bot 22.x kutilgan"

  # Sintaksis tekshiruvi — fayl yozmaydi (py_compile o'rniga ast.parse)
  "$HPY" -c 'import ast, sys
for f in sys.argv[1:]:
    ast.parse(open(f, encoding="utf-8").read(), f)
print("Sintaksis: OK,", len(sys.argv) - 1, "fayl")' "$SRC_PLUGIN"/*.py || die "plugin kodida sintaksis xatosi"

  if "$HPY" -c 'import pytest' 2>/dev/null; then
    log "Testlar (vaqtinchalik katalogda):"
    PYTHONDONTWRITEBYTECODE=1 "$HPY" -m pytest -q -p no:cacheprovider "$SRC_TESTS" 2>&1 | tail -n 3 \
      || die "testlar o'tmadi"
  else
    log "pytest yo'q — testlar o'tkazib yuborildi (sintaksis tekshirildi)."
  fi

  if grep -E '^TELEGRAM_ALLOWED_USERS=' "$HERMES_HOME_DIR/.env" | grep -qE "(^|[=,\"' ])${ADMIN_ID}([,\"' ]|$)"; then
    log "Admin ID TELEGRAM_ALLOWED_USERS ichida: ha"
  else
    die "Admin ID $ADMIN_ID ~/.hermes/.env dagi TELEGRAM_ALLOWED_USERS ichida topilmadi. ID to'g'riligini tekshiring."
  fi
  if env_names "$HERMES_HOME_DIR/.env" | grep -qxE 'TELEGRAM_ALLOW_ALL_USERS|GATEWAY_ALLOW_ALL_USERS'; then
    die ".env da *_ALLOW_ALL_USERS bor — bu hamma uchun ochiq kirish. Avval uni olib tashlang."
  fi

  local routes; routes="$(cfg_get_json gateway.profile_routes)"
  if [[ -n "$routes" && "$routes" != "[]" && "$routes" != "null" ]]; then
    "$HPY" -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if all(x.get("name") in ("admin","subscribers") for x in r) else 1)' "$routes" \
      || die "gateway.profile_routes da boshqa route'lar bor — qo'lda ko'rib chiqing: $routes"
  fi

  for dir in "$HERMES_HOME_DIR/plugins/subscriptions" "$SUB_HOME/plugins/subscriptions"; do
    if [[ -e "$dir" && ! -f "$dir/$MARKER" ]]; then
      die "$dir mavjud va bu skript tomonidan o'rnatilmagan — ustidan yozilmaydi"
    fi
  done

  log "Gateway: $(systemctl --user is-active hermes-gateway 2>/dev/null || echo noma'lum)"
  log "Linger: $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo noma'lum)"
  log "Profil '$PROFILE': $([[ -d "$SUB_HOME" ]] && echo 'mavjud (qayta sozlanadi)' || echo 'yaratiladi')"
  log "Baza: $DB_PATH"
  log "AI_SCALPING_PRO va ai-scalping-pro.service: tegilmaydi."
  log "Tekshiruv: OK"
}

# ----------------------------------------------------------------------------- 2. backup
backup() {
  step "2. ZAXIRA"
  confirm "Zaxira $BACKUP_DIR ga olinadi."
  run mkdir -p -m 700 "$BACKUP_DIR"
  run cp -a "$HERMES_HOME_DIR/config.yaml" "$BACKUP_DIR/config.yaml"
  run cp -a "$HERMES_HOME_DIR/.env" "$BACKUP_DIR/default.env"
  for rel in platforms/pairing pairing plugins/subscriptions; do
    if [[ -e "$HERMES_HOME_DIR/$rel" ]]; then
      run tar -C "$HERMES_HOME_DIR" -czf "$BACKUP_DIR/$(echo "$rel" | tr '/' '_').tgz" "$rel"
    fi
  done
  if [[ -d "$SUB_HOME" ]]; then
    run tar -C "$HERMES_HOME_DIR/profiles" -czf "$BACKUP_DIR/profile_$PROFILE.tgz" "$PROFILE"
    (( DRY_RUN )) || echo "yes" > "$BACKUP_DIR/profile_existed"
  else
    (( DRY_RUN )) || echo "no" > "$BACKUP_DIR/profile_existed"
  fi
  if [[ -f "$DB_PATH" ]]; then
    run "$HPY" -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d)' \
      "$DB_PATH" "$BACKUP_DIR/subs.db"
  fi
  if (( ! DRY_RUN )); then
    chmod -R go-rwx "$BACKUP_DIR"
    (cd "$BACKUP_DIR" && sha256sum -- * > SHA256SUMS)
    echo "$BACKUP_DIR" > "$BACKUP_ROOT/last_install"
    # Birinchi o'rnatishdan OLDINGI asl holat bir marta yoziladi — qayta o'rnatish uni almashtirmaydi.
    if [[ ! -f "$BACKUP_ROOT/original_install" ]]; then echo "$BACKUP_DIR" > "$BACKUP_ROOT/original_install"; fi
  fi
  log "Zaxira tayyor: $BACKUP_DIR"
}

# ----------------------------------------------------------------------------- 3. install
deploy_plugin() {  # $1 = maqsad plugins katalogi
  local dest="$1/subscriptions"
  run mkdir -p "$1"
  run rm -rf "$dest.new"
  run cp -a "$SRC_PLUGIN" "$dest.new"
  run find "$dest.new" -name __pycache__ -prune -exec rm -rf {} +
  if (( ! DRY_RUN )); then : > "$dest.new/$MARKER"; fi
  if [[ -e "$dest" ]]; then run rm -rf "$dest.old"; run mv "$dest" "$dest.old"; fi
  run mv "$dest.new" "$dest"
  run rm -rf "$dest.old"
}

plugin_settings() {  # $1 = cfg_default | cfg_sub ; $2 = rol
  "$1" plugins.entries.subscriptions.settings.role "$2"
  "$1" plugins.entries.subscriptions.settings.admin_telegram_id "$ADMIN_ID"
  "$1" plugins.entries.subscriptions.settings.db_path "$DB_PATH"
  "$1" plugins.entries.subscriptions.settings.daily_limit "$DAILY_LIMIT"
  "$1" plugins.entries.subscriptions.settings.subscriber_profile "$PROFILE"
}

telegram_section_key() {
  # Telegram sozlamalari config.yaml'da qayerda turganini aniqlaydi (gateway/config_loader.py:188 tartibi).
  "$HPY" -c 'import sys, yaml
c = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
g = c.get("gateway") or {}
if isinstance(c.get("telegram"), dict): print("telegram")
elif isinstance((g.get("platforms") or {}).get("telegram"), dict): print("gateway.platforms.telegram")
else: print("platforms.telegram")' "$HERMES_HOME_DIR/config.yaml"
}

install_step() {
  step "3. O'RNATISH"
  confirm "Profil '$PROFILE' yaratiladi/sozlanadi, plugin ikkala profilga o'rnatiladi, route'lar yoziladi. Gateway hali qayta ishga tushirilmaydi."

  # Profil yaratilganda gateway o'zi multiplex'ga o'tib ketmasligi uchun (gateway/run_profile_reconcile.py:355).
  cfg_default gateway.auto_multiplex_migration false

  if [[ ! -d "$SUB_HOME" ]]; then
    run "$HERMES_BIN" profile create "$PROFILE" --no-skills --description "Obunachilar uchun cheklangan profil"
  fi
  if (( ! DRY_RUN )); then
    [[ -d "$SUB_HOME" ]] || die "profil yaratilmadi: $SUB_HOME"
    local names; names="$(env_names "$SUB_HOME/.env")"
    if grep -qxE 'TELEGRAM_ALLOWED_USERS|GATEWAY_ALLOWED_USERS|TELEGRAM_ALLOW_ALL_USERS|GATEWAY_ALLOW_ALL_USERS|TELEGRAM_BOT_TOKEN' <<<"$names"; then
      die "$SUB_HOME/.env da Telegram allowlist/token bor — obunachilar profilida bo'lmasligi kerak"
    fi
    grep -qx 'OPENAI_API_KEY' <<<"$names" || die "$SUB_HOME/.env da OPENAI_API_KEY yo'q (profil kalitni ko'chirmadi)"
  fi

  # Obunachilar profili: arzon model, minimal tool'lar, xotira va avtomatik yozishlar o'chiq.
  cfg_sub model.provider "$PROVIDER"
  cfg_sub model.default "$MODEL"
  cfg_sub platform_toolsets.telegram "$SUB_TOOLSETS"
  cfg_sub agent.disabled_toolsets "$SUB_DISABLED"
  cfg_sub memory.memory_enabled false
  cfg_sub memory.user_profile_enabled false
  cfg_sub auxiliary.background_review.enabled false
  plugin_settings cfg_sub gate
  deploy_plugin "$SUB_HOME/plugins"
  run "$HERMES_BIN" -p "$PROFILE" plugins enable subscriptions

  # Default (administrator) profil.
  plugin_settings cfg_default admin
  deploy_plugin "$HERMES_HOME_DIR/plugins"
  run "$HERMES_BIN" plugins enable subscriptions

  local current; current="$(cfg_get_json platform_toolsets.telegram)"
  if [[ -n "$current" && "$current" != "null" ]]; then
    local updated
    updated="$("$HPY" -c 'import json,sys; v=json.loads(sys.argv[1]); v=v if isinstance(v,list) else [v]
print(json.dumps(v if "subscriptions" in v else v+["subscriptions"]))' "$current")"
    [[ "$updated" == "$current" ]] || cfg_default platform_toolsets.telegram "$updated"
  fi

  local section; section="$(telegram_section_key)"
  cfg_default "$section.unauthorized_dm_behavior" pair

  cfg_default gateway.profile_routes \
    "[{\"name\":\"admin\",\"platform\":\"telegram\",\"user_id\":\"$ADMIN_ID\",\"profile\":\"default\"},{\"name\":\"subscribers\",\"platform\":\"telegram\",\"profile\":\"$PROFILE\"}]"

  if (( ! DRY_RUN )); then
    log "Tekshiruv (o'qib qaytarish):"
    log "  route'lar: $(hermes config get gateway.profile_routes --json)"
    log "  $section.unauthorized_dm_behavior: $(hermes config get "$section.unauthorized_dm_behavior")"
    log "  obunachilar modeli: $("$HERMES_BIN" -p "$PROFILE" config get model.default)"
    log "  obunachilar toolset'lari: $("$HERMES_BIN" -p "$PROFILE" config get platform_toolsets.telegram --json)"
  fi
}

# ----------------------------------------------------------------------------- 4. activate
activate_step() {
  step "4. FAOLLASHTIRISH (gateway bir marta qayta ishga tushadi)"
  run "$HERMES_BIN" gateway migrate --multiplex --dry-run
  confirm "Yuqoridagi reja bajariladi va gateway qayta ishga tushadi (bir necha soniya uzilish)."
  run "$HERMES_BIN" gateway migrate --multiplex --yes
  if (( ! DRY_RUN )); then
    sleep 15
    local state; state="$(systemctl --user is-active hermes-gateway 2>/dev/null || true)"
    log "Gateway holati: $state"
    [[ "$state" == "active" ]] || die "gateway faol emas. Loglar: journalctl --user -u hermes-gateway -n 100 ; qaytarish: bash rollback.sh"
    hermes gateway status 2>&1 | head -n 30 || true
  fi
}

# ----------------------------------------------------------------------------- 5. timers
write_unit() {  # $1 = fayl nomi, stdin = mazmun
  if (( DRY_RUN )); then log "  [dry-run] $UNIT_DIR/$1 yoziladi:"; sed 's/^/      /'; return 0; fi
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$1"
  log "  + $UNIT_DIR/$1"
}

timers_step() {
  step "5. SYSTEMD TIMER'LAR (yangi unit'lar; mavjudlariga tegilmaydi)"
  confirm "2 ta yangi user timer qo'shiladi: pairing tozalash (10 daqiqa) va kunlik zaxira (03:30)."
  local cli="$HPY -m subscriptions.cli --db $DB_PATH --admin-id $ADMIN_ID"
  write_unit hermes-subscriptions-reconcile.service <<EOF
[Unit]
Description=Hermes subscriptions: muddati o'tganlarni pairing'dan tozalash

[Service]
Type=oneshot
WorkingDirectory=$HERMES_HOME_DIR/plugins
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$cli reconcile --apply --profile $PROFILE --hermes-bin $HERMES_BIN
EOF
  write_unit hermes-subscriptions-reconcile.timer <<'EOF'
[Unit]
Description=Hermes subscriptions: pairing tozalash (har 10 daqiqada)

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF
  write_unit hermes-subscriptions-backup.service <<EOF
[Unit]
Description=Hermes subscriptions: baza zaxirasi

[Service]
Type=oneshot
WorkingDirectory=$HERMES_HOME_DIR/plugins
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$cli backup --keep 14
EOF
  write_unit hermes-subscriptions-backup.timer <<'EOF'
[Unit]
Description=Hermes subscriptions: kunlik zaxira

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
  run systemctl --user daemon-reload
  run systemctl --user enable --now hermes-subscriptions-reconcile.timer hermes-subscriptions-backup.timer
  if [[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
    log "OGOHLANTIRISH: Linger yoqilmagan — SSH'dan chiqqanda timer'lar to'xtashi mumkin. Yoqish: sudo loginctl enable-linger $(id -un)"
  fi
}

# ----------------------------------------------------------------------------- asosiy
[[ -n "$ADMIN_ID" ]] || { usage; exit 2; }
if (( DRY_RUN )); then log "DRY-RUN: hech narsa o'zgartirilmaydi."; fi
case "$STEP" in
  preflight) preflight ;;
  backup) preflight; backup ;;
  install) preflight; backup; install_step ;;
  activate) preflight; activate_step ;;
  timers) preflight; timers_step ;;
  all) preflight; backup; install_step; activate_step; timers_step ;;
  *) die "noma'lum --step: $STEP" ;;
esac
step "TAYYOR"
log "Zaxira: $BACKUP_DIR (qaytarish: bash $SCRIPT_DIR/rollback.sh)"
log "Keyingi qadam: deploy/TEST_PLAN.md bo'yicha qabul testlari."
