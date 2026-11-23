#!/usr/bin/env bash
# =============================================================================
#  Hermes obuna plugini — qaytarish (rollback) skripti
#
#  1. Obuna timer'larini to'xtatadi va o'chiradi (faqat shu skript qo'shgan unit'lar)
#  2. Pluginni ikkala profilda o'chiradi va plugin kataloglarini zaxira katalogiga ko'chiradi
#  3. ~/.hermes/config.yaml va .env ni o'rnatishdan oldingi zaxiradan tiklaydi
#     (route'lar, multiplex bayrog'i, plugin sozlamalari shu bilan qaytadi)
#  4. "subscribers" profili o'rnatishda yaratilgan bo'lsa — arxivlab, o'chiradi (--keep-profile bilan qoladi)
#  5. Gateway'ni qayta ishga tushiradi
#  Obuna bazasi (~/.local/share/hermes-subscriptions/subs.db) O'CHIRILMAYDI.
#
#  Foydalanish:  bash rollback.sh [--backup DIR] [--keep-profile] [--dry-run] [--yes]
# =============================================================================
set -Eeuo pipefail

HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PROFILE="subscribers"
SUB_HOME="$HERMES_HOME_DIR/profiles/$PROFILE"
HERMES_BIN="$(command -v hermes 2>/dev/null || echo "$HOME/.local/bin/hermes")"
BACKUP_ROOT="$HOME/hermes-subs-backups"
UNIT_DIR="$HOME/.config/systemd/user"
BACKUP_DIR=""
KEEP_PROFILE=0
DRY_RUN=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) BACKUP_DIR="${2:-}"; shift 2 ;;
    --keep-profile) KEEP_PROFILE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Noma'lum parametr: $1" >&2; exit 2 ;;
  esac
done

log()  { printf '%s\n' "$*"; }
die()  { printf '\nXATO: %s\n' "$*" >&2; exit 1; }
run() {
  if (( DRY_RUN )); then printf '  [dry-run] %q' "$1"; shift; printf ' %q' "$@"; printf '\n'; return 0; fi
  printf '  + %s\n' "$*"
  "$@"
}
try() { run "$@" || log "  (davom etiladi: buyruq xato qaytardi)"; }

[[ $EUID -ne 0 ]] || die "root sifatida ishga tushirmang"
if [[ -z "$BACKUP_DIR" ]]; then
  # Standart: birinchi o'rnatishdan oldingi asl holat.
  [[ -f "$BACKUP_ROOT/original_install" ]] || die "Zaxira topilmadi. --backup DIR bilan ko'rsating."
  BACKUP_DIR="$(cat "$BACKUP_ROOT/original_install")"
fi
[[ -f "$BACKUP_DIR/config.yaml" && -f "$BACKUP_DIR/default.env" ]] || die "Zaxira to'liq emas: $BACKUP_DIR"
if [[ -f "$BACKUP_DIR/SHA256SUMS" ]]; then
  (cd "$BACKUP_DIR" && sha256sum --quiet -c SHA256SUMS) || die "Zaxira fayllari o'zgargan (SHA256 mos emas)"
fi

log "Zaxira: $BACKUP_DIR"
log "Profil: $SUB_HOME ($([[ -d "$SUB_HOME" ]] && echo mavjud || echo yo\'q)), o'rnatishdan oldin: $(cat "$BACKUP_DIR/profile_existed" 2>/dev/null || echo noma\'lum)"
if (( ! DRY_RUN && ! ASSUME_YES )); then
  read -r -p "Qaytarish bajarilsinmi? Gateway qayta ishga tushadi. [ha/yo'q]: " answer
  [[ "$answer" == "ha" ]] || die "bekor qilindi"
fi

STASH="$BACKUP_DIR/rollback-$(date +%Y%m%d-%H%M%S)"
run mkdir -p -m 700 "$STASH"

log "1. Timer'lar"
for unit in hermes-subscriptions-reconcile.timer hermes-subscriptions-backup.timer; do
  try systemctl --user disable --now "$unit"
done
for f in hermes-subscriptions-reconcile.service hermes-subscriptions-reconcile.timer \
         hermes-subscriptions-backup.service hermes-subscriptions-backup.timer; do
  if [[ -f "$UNIT_DIR/$f" ]]; then run mv "$UNIT_DIR/$f" "$STASH/$f"; fi
done
try systemctl --user daemon-reload

log "2. Plugin"
try "$HERMES_BIN" plugins disable subscriptions
if [[ -d "$SUB_HOME" ]]; then try "$HERMES_BIN" -p "$PROFILE" plugins disable subscriptions; fi
for dir in "$HERMES_HOME_DIR/plugins/subscriptions" "$SUB_HOME/plugins/subscriptions"; do
  if [[ -d "$dir" ]]; then
    [[ -f "$dir/.installed-by-hermes-subscriptions" ]] || { log "  $dir bu skriptniki emas — tegilmadi"; continue; }
    run mv "$dir" "$STASH/plugin-$(echo "$dir" | md5sum | cut -c1-8)"
  fi
done

log "3. config.yaml va .env tiklanmoqda"
run cp -a "$HERMES_HOME_DIR/config.yaml" "$STASH/config.yaml.before-rollback"
run cp -a "$HERMES_HOME_DIR/.env" "$STASH/default.env.before-rollback"
run cp -a "$BACKUP_DIR/config.yaml" "$HERMES_HOME_DIR/config.yaml"
run cp -a "$BACKUP_DIR/default.env" "$HERMES_HOME_DIR/.env"

log "4. Profil"
if [[ -d "$SUB_HOME" && "$(cat "$BACKUP_DIR/profile_existed" 2>/dev/null || echo yes)" == "no" && $KEEP_PROFILE -eq 0 ]]; then
  run tar -C "$HERMES_HOME_DIR/profiles" -czf "$STASH/profile_$PROFILE.tgz" "$PROFILE"
  run "$HERMES_BIN" profile delete "$PROFILE" --yes
else
  log "  profil saqlab qolindi"
fi

log "5. Gateway"
run "$HERMES_BIN" gateway restart --yes
if (( ! DRY_RUN )); then
  sleep 10
  log "Gateway holati: $(systemctl --user is-active hermes-gateway 2>/dev/null || echo noma\'lum)"
  run chmod -R go-rwx "$STASH"
fi
log "Qaytarish tugadi. Olib tashlangan fayllar: $STASH"
if (( ! DRY_RUN )) && [[ -f "$BACKUP_ROOT/original_install" ]]; then
  run mv "$BACKUP_ROOT/original_install" "$STASH/original_install"
fi
log "Obuna bazasi saqlandi: ~/.local/share/hermes-subscriptions/subs.db"
