#!/usr/bin/env bash
# install.sh / rollback.sh uchun sinov stendi (soxta HOME, soxta hermes/systemctl/loginctl)
set -uo pipefail
T="$(cd "$(dirname "$0")" && pwd)"
REPO="${REPO:-$(cd "$T/../.." && pwd)}"
VPY="${VPY:?VPY=<python-telegram-bot 22.8, pyyaml va pytest bor python> kerak}"
PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

setup() {
  export HOME="$T/home"; rm -rf "$HOME" "$T/bin"; mkdir -p "$HOME/.hermes/hermes-agent/venv/bin" "$HOME/.hermes/platforms/pairing" \
    "$HOME/AI_SCALPING_PRO" "$T/bin" "$HOME/.local/bin"
  cat > "$HOME/.hermes/config.yaml" <<'EOF'
# foydalanuvchi izohi
model:
  default: gpt-5.4-mini
  provider: openai
telegram:
  home_channel: '111111111'
EOF
  printf 'OPENAI_API_KEY=sk-real\nTELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_ALLOWED_USERS=111111111,222\n' > "$HOME/.hermes/.env"
  chmod 600 "$HOME/.hermes/.env"
  echo '{}' > "$HOME/.hermes/platforms/pairing/telegram-approved.json"
  echo "secret" > "$HOME/AI_SCALPING_PRO/.env"
  printf '#!/bin/sh\nexec %s "$@"\n' "$VPY" > "$HOME/.hermes/hermes-agent/venv/bin/python"
  printf '#!/bin/sh\nexec %s %s "$@"\n' "$VPY" "$T/fake_hermes.py" > "$HOME/.local/bin/hermes"
  printf '#!/bin/sh\necho "systemctl $*" >> "$FAKE_LOG"\ncase "$*" in *is-active*) echo active;; esac\n' > "$T/bin/systemctl"
  printf '#!/bin/sh\necho yes\n' > "$T/bin/loginctl"
  chmod +x "$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.local/bin/hermes" "$T/bin/"*
  export PATH="$T/bin:$HOME/.local/bin:/usr/bin:/bin" FAKE_LOG="$T/calls.log"; : > "$FAKE_LOG"
}
snap() { (cd "$HOME" && find . -type f -not -path './.hermes/hermes-agent/*' -printf '%p %s %T@\n' | sort; cat .hermes/config.yaml .hermes/.env) | sha256sum; }
inst() { bash "$REPO/deploy/install.sh" --admin-id 111111111 "$@" > "$T/out.txt" 2>&1; }
cfg()  { "$HOME/.local/bin/hermes" "$@" 2>/dev/null; }

echo "== A. preflight: hech narsa o'zgarmaydi"
setup; before="$(snap)"; inst --step preflight; rc=$?
check "preflight exit=0" "[[ $rc -eq 0 ]]"
check "testlar serverdagi Python bilan o'tdi" "grep -q 'passed' $T/out.txt"
check "fayllar o'zgarmadi" "[[ \"$before\" == \"$(snap)\" ]]"

echo "== B. dry-run all: hech narsa o'zgarmaydi"
before="$(snap)"; inst --dry-run; rc=$?
check "dry-run exit=0" "[[ $rc -eq 0 ]]"
check "fayllar o'zgarmadi" "[[ \"$before\" == \"$(snap)\" ]]"
check "faqat o'qish chaqiruvlari" "! grep -qE '\"(set|create|enable|migrate|delete)\"' $FAKE_LOG && ! grep -q 'systemctl .*enable' $FAKE_LOG"
check "dry-run rejani ko'rsatdi" "grep -q 'dry-run.*migrate' $T/out.txt && grep -q 'hermes-subscriptions-reconcile.timer' $T/out.txt"

echo "== C. to'liq o'rnatish (--yes)"
cp "$HOME/.hermes/config.yaml" "$T/orig_config.yaml"; cp "$HOME/.hermes/.env" "$T/orig.env"
scalp_before="$(sha256sum "$HOME/AI_SCALPING_PRO/.env")"
: > "$FAKE_LOG"; inst --yes; rc=$?
check "install exit=0" "[[ $rc -eq 0 ]]" || tail -20 "$T/out.txt"
B="$(cat "$HOME/hermes-subs-backups/original_install" 2>/dev/null)"
check "asl zaxira: config, .env, SHA256SUMS" "[[ -f $B/config.yaml && -f $B/default.env && -f $B/SHA256SUMS ]] && (cd $B && sha256sum --quiet -c SHA256SUMS)"
check "zaxira ruxsatlari 700/600" "[[ \$(stat -c %a $B) == 700 && \$(stat -c %a $B/default.env) == 600 ]]"
check "profile_existed=no" "[[ \$(cat $B/profile_existed) == no ]]"
for d in "$HOME/.hermes/plugins/subscriptions" "$HOME/.hermes/profiles/subscribers/plugins/subscriptions"; do
  check "plugin o'rnatildi: ${d#$HOME/}" "[[ -f $d/__init__.py && -f $d/.installed-by-hermes-subscriptions && -z \$(find $d -name __pycache__) ]]"
done
check "route'lar to'g'ri" "[[ \$(cfg config get gateway.profile_routes --json) == '[{\"name\": \"admin\", \"platform\": \"telegram\", \"user_id\": \"111111111\", \"profile\": \"default\"}, {\"name\": \"subscribers\", \"platform\": \"telegram\", \"profile\": \"subscribers\"}]' ]]"
check "unauthorized_dm_behavior mavjud telegram bo'limiga yozildi" "[[ \$(cfg config get telegram.unauthorized_dm_behavior) == pair ]] && ! cfg config get platforms.telegram >/dev/null"
check "auto_multiplex_migration=false" "[[ \$(cfg config get gateway.auto_multiplex_migration) == false ]]"
check "admin plugin sozlamalari" "[[ \$(cfg config get plugins.entries.subscriptions.settings.role) == admin && \$(cfg config get plugins.entries.subscriptions.settings.admin_telegram_id) == 111111111 ]]"
check "obunachi: model, toolset, xotira o'chiq, rol gate" "[[ \$(cfg -p subscribers config get model.default) == gpt-5.4-mini && \$(cfg -p subscribers config get platform_toolsets.telegram --json) == '[\"web\", \"vision\", \"clarify\", \"todo\"]' && \$(cfg -p subscribers config get memory.memory_enabled) == false && \$(cfg -p subscribers config get plugins.entries.subscriptions.settings.role) == gate && \$(cfg -p subscribers config get plugins.entries.subscriptions.settings.daily_limit) == 50 ]]"
check "migrate: avval dry-run, keyin --yes" "grep -n 'migrate' $FAKE_LOG | head -2 | tr '\n' ' ' | grep -q 'dry-run.*--yes'"
check "plugin ikki profilda yoqildi" "[[ \$(grep -c '\"enable\", \"subscriptions\"' $FAKE_LOG) == 2 ]] && grep -q '\"-p\", \"subscribers\", \"plugins\", \"enable\"' $FAKE_LOG"
check "timer unit'lari yozildi va yoqildi" "[[ -f $HOME/.config/systemd/user/hermes-subscriptions-reconcile.timer ]] && grep -q 'enable --now hermes-subscriptions-reconcile.timer hermes-subscriptions-backup.timer' $FAKE_LOG"
check "AI_SCALPING_PRO tegilmadi" "[[ \"$scalp_before\" == \"\$(sha256sum $HOME/AI_SCALPING_PRO/.env)\" ]] && ! grep -q AI_SCALPING $FAKE_LOG"
check "asosiy .env o'zgarmadi" "cmp -s $T/orig.env $HOME/.hermes/.env"

echo "== C2. timer buyruqlari haqiqatan ishlaydi"
"$VPY" -c "import sys; sys.path.insert(0,'$REPO'); from subscriptions.service import SubscriptionService; SubscriptionService('$HOME/.local/share/hermes-subscriptions/subs.db',[111111111])"
for unit in reconcile backup; do
  cmdline="$(grep '^ExecStart=' "$HOME/.config/systemd/user/hermes-subscriptions-$unit.service" | cut -d= -f2-)"
  (cd "$HOME/.hermes/plugins" && PYTHONDONTWRITEBYTECODE=1 eval "$cmdline") > "$T/unit_$unit.txt" 2>&1; rc=$?
  check "ExecStart ($unit) exit=0" "[[ $rc -eq 0 ]]" || cat "$T/unit_$unit.txt"
done
check "backup fayli yaratildi" "ls $HOME/.local/share/hermes-subscriptions/backups/subs-*.db >/dev/null 2>&1"

echo "== D. qayta o'rnatish (idempotent)"
: > "$FAKE_LOG"; inst --yes; rc=$?
check "ikkinchi install exit=0" "[[ $rc -eq 0 ]]" || tail -20 "$T/out.txt"
check "profil qayta yaratilmadi" "! grep -q '\"profile\", \"create\"' $FAKE_LOG"
check "asl zaxira o'zgarmadi" "[[ \$(cat $HOME/hermes-subs-backups/original_install) == $B ]]"

echo "== E. rollback"
: > "$FAKE_LOG"; bash "$REPO/deploy/rollback.sh" --yes > "$T/rb.txt" 2>&1; rc=$?
check "rollback exit=0" "[[ $rc -eq 0 ]]" || tail -20 "$T/rb.txt"
check "config.yaml asl holatiga qaytdi (bayt-ma-bayt)" "cmp -s $T/orig_config.yaml $HOME/.hermes/config.yaml"
check ".env asl holatida" "cmp -s $T/orig.env $HOME/.hermes/.env"
check "plugin kataloglari olib tashlandi" "[[ ! -e $HOME/.hermes/plugins/subscriptions ]]"
check "profil o'chirildi (oldin yo'q edi)" "[[ ! -e $HOME/.hermes/profiles/subscribers ]] && grep -q '\"profile\", \"delete\", \"subscribers\", \"--yes\"' $FAKE_LOG"
check "timer'lar o'chirildi" "[[ ! -e $HOME/.config/systemd/user/hermes-subscriptions-reconcile.timer ]] && grep -q 'disable --now hermes-subscriptions-reconcile.timer' $FAKE_LOG"
check "gateway restart" "grep -q '\"gateway\", \"restart\", \"--yes\"' $FAKE_LOG"
check "baza saqlandi" "[[ -f $HOME/.local/share/hermes-subscriptions/subs.db ]]"

echo "== F. xavfli holatlarda hech narsa o'zgarmasdan to'xtaydi"
setup; before="$(snap)"
bash "$REPO/deploy/install.sh" --admin-id 999999999 --yes > "$T/out.txt" 2>&1; rc=$?
check "admin ID allowlist'da yo'q -> to'xtadi" "[[ $rc -ne 0 ]] && grep -q 'TELEGRAM_ALLOWED_USERS ichida topilmadi' $T/out.txt && [[ \"$before\" == \"$(snap)\" ]]"
mkdir -p "$HOME/.hermes/plugins/subscriptions"; before="$(snap)"
inst --yes; rc=$?
check "begona plugin katalogi -> to'xtadi" "[[ $rc -ne 0 ]] && grep -q 'ustidan yozilmaydi' $T/out.txt && [[ \"$before\" == \"$(snap)\" ]]"
rmdir "$HOME/.hermes/plugins/subscriptions" "$HOME/.hermes/plugins"
echo "GATEWAY_ALLOW_ALL_USERS=true" >> "$HOME/.hermes/.env"; before="$(snap)"
inst --yes; rc=$?
check "ALLOW_ALL_USERS -> to'xtadi" "[[ $rc -ne 0 ]] && grep -q 'ALLOW_ALL' $T/out.txt && [[ \"$before\" == \"$(snap)\" ]]"
setup; before="$(snap)"
FAKE_VERSION="Hermes Agent v0.24.0" inst --yes; rc=$?
check "boshqa Hermes versiyasi -> to'xtadi" "[[ $rc -ne 0 ]] && [[ \"$before\" == \"$(snap)\" ]]"
setup
FAKE_PROFILE_ENV=$'OPENAI_API_KEY=x\nTELEGRAM_ALLOWED_USERS=5\n' inst --yes; rc=$?
check "obunachi .env da allowlist bo'lsa -> to'xtadi" "[[ $rc -ne 0 ]] && grep -q 'bo.lmasligi kerak' $T/out.txt && ! grep -q 'plugins.*enable' $FAKE_LOG"
bash "$REPO/deploy/install.sh" --admin-id abc --step preflight > "$T/out.txt" 2>&1; rc=$?
check "noto'g'ri admin ID -> to'xtadi" "[[ $rc -ne 0 ]]"

echo; echo "NATIJA: PASS=$PASS FAIL=$FAIL"
