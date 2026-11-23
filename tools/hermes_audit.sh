#!/usr/bin/env bash
# =============================================================================
#  hermes_audit.sh  v2.0 — FAQAT O'QISH rejimidagi server va Hermes Agent auditi
#  Mo'ljal: Ubuntu 24.04, Hermes Agent v0.23.x, foydalanuvchi: ubuntu
# -----------------------------------------------------------------------------
#  KAFOLATLAR
#   * Hech qanday fayl yaratmaydi/o'zgartirmaydi/o'chirmaydi. Natija faqat stdout.
#     (heredoc ishlatilmaydi — bash vaqtinchalik fayl ham yaratmaydi)
#   * Paket o'rnatmaydi, sudo ishlatmaydi, xizmatlarni start/stop/restart qilmaydi.
#   * Tarmoqqa so'rov yubormaydi. `hermes` CLI ni ishga tushirmaydi.
#   * AI_SCALPING_PRO katalogiga kirmaydi; ai-scalping-pro.service dan faqat
#     holat (active/enabled/RAM) o'qiladi.
#   * Maxfiy ma'lumot UMUMAN chiqarilmaydi:
#       - .env / auth / token / credential fayllar mazmuni chiqarilmaydi
#         (.env dan faqat o'zgaruvchi NOMI va [set]/[EMPTY]);
#       - konfiguratsiyadan faqat xavfsiz ko'rinishdagi qiymatlar (bool, kichik son,
#         qisqa identifikator) chiqadi, qolgani "<matn, N belgi>";
#       - journal va log matni chiqarilmaydi — faqat kategoriyalar soni;
#       - systemd Environment= qiymatlari o'qilmaydi; git remote ko'rsatilmaydi;
#       - SOUL.md / xotira mazmuni chiqarilmaydi (faqat tuzilma va statistika);
#       - tashqi IP, hostname, e-mail, 7+ xonali raqamlar (Telegram ID) yashiriladi.
#   * Uch qatlamli himoya: (1) allowlist chiqish, (2) regex filtr,
#     (3) filtrdan keyingi "sizib chiqish detektori" — shubha bo'lsa bo'lim
#     butunlay chiqarilmaydi. Boshida filtr o'z-o'zini sinaydi; ishlamasa audit to'xtaydi.
#
#  ISHGA TUSHIRISH (ubuntu foydalanuvchisi nomidan, sudo'siz):
#     bash hermes_audit.sh
#  Hisobotni saqlash ixtiyoriy (bu sizning buyrug'ingiz, skript fayl yaratmaydi):
#     bash hermes_audit.sh > ~/hermes_audit_report.txt 2>&1
# =============================================================================

if [ -z "${BASH_VERSION:-}" ]; then echo "Bu skriptni bash bilan ishga tushiring: bash hermes_audit.sh"; exit 1; fi

export LC_ALL=C.UTF-8
export GIT_OPTIONAL_LOCKS=0 GIT_TERMINAL_PROMPT=0
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
export SYSTEMD_PAGER="" SYSTEMD_LESS="" SYSTEMD_COLORS=0 PAGER=cat
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export EXCL_NAME="AI_SCALPING_PRO"
export EXCL_UNIT="ai-scalping-pro.service"
export T="${AUDIT_CMD_TIMEOUT:-20}"
export SECTION_T="${AUDIT_SECTION_TIMEOUT:-180}"
HOST_RAW="$(cat /proc/sys/kernel/hostname 2>/dev/null)"; export HOST_RAW

# =============================================================================
#  Yordamchi funksiyalar
# =============================================================================
have() { command -v "$1" >/dev/null 2>&1; }
sum()  { printf '@@SUM@@%s\n' "$1"; }                       # yakuniy xulosaga qator
sub()  { printf '\n--- %s ---\n' "$1"; }
hp()   { local p="$1"; case "$p" in "$HOME"*) p="~${p#"$HOME"}";; esac; printf '%s' "$p"; }
hsize(){ awk -v b="${1:-0}" 'BEGIN{split("B KB MB GB TB",u);i=1;while(b>=1024&&i<5){b/=1024;i++}; if(i==1)printf "%dB",b; else printf "%.1f%s",b,u[i]}'; }
fhash(){ have sha256sum && sha256sum "$1" 2>/dev/null | cut -c1-16; }
meta() {
  [ -e "$1" ] || { echo "$(hp "$1")  [mavjud emas]"; return; }
  printf '%s  [perm=%s owner=%s size=%s mtime=%s]\n' "$(hp "$1")" "$(stat -c %a "$1" 2>/dev/null)" \
    "$(stat -c %U:%G "$1" 2>/dev/null)" "$(hsize "$(stat -c %s "$1" 2>/dev/null)")" "$(date -r "$1" '+%F %T' 2>/dev/null)"
}
is_excl() { case "$1" in *"/$EXCL_NAME"|*"/$EXCL_NAME/"*) return 0;; esac; return 1; }
is_sens() {
  local b; b="$(basename "$1" | tr '[:upper:]' '[:lower:]')"
  case "$b" in
    .env|.env.*|*.env|*secret*|*token*|*credential*|auth|auth.*|*auth*.json|*oauth*|*.pem|*.key|*.p12|*.pfx|\
    id_rsa*|id_ed25519*|id_ecdsa*|*.kdbx|*cookie*|*password*|*passwd*|*session*.json|*keyring*) return 0;;
  esac
  return 1
}
# find: og'ir va himoyalangan kataloglarga kirmaydi
ff() { local root="$1" depth="$2"; shift 2
  [ -d "$root" ] || return 0
  timeout "$T" find "$root" -maxdepth "$depth" \( -name node_modules -o -name .git -o -name venv -o -name .venv \
    -o -name __pycache__ -o -name site-packages -o -name .cache -o -name "$EXCL_NAME" \) -prune -o "$@" 2>/dev/null; }
# buyruqni timeout bilan ishga tushirish (mavjud bo'lmasa — xabar)
run_ro() { local t="$1"; shift; sub "$t"
  if ! have "$1"; then echo "  [mavjud emas: $1]"; return 0; fi
  timeout "$T" "$@" 2>&1; local rc=$?; [ "$rc" -eq 0 ] || echo "  [exit=$rc]"; return 0; }
# git: fsmonitor/untrackedCache o'chirilgan (begona repo konfiguratsiyasi buyruq bajara olmasin)
gitq() { local r="$1"; shift; timeout 10 git -c core.fsmonitor=false -c core.untrackedCache=false -C "$r" "$@" 2>/dev/null; }
# .env: faqat NOM va [set]/[EMPTY] — qiymat hech qachon chiqmaydi
env_names() {
  awk -v sq="'" '
    /^[[:space:]]*#/ || index($0,"=")==0 { next }
    { line=$0; sub(/^[[:space:]]*(export[[:space:]]+)?/, "", line)
      eq=index(line,"="); name=substr(line,1,eq-1); val=substr(line,eq+1)
      gsub(/[[:space:]]/, "", name); gsub(/[[:space:]"]/, "", val); gsub(sq, "", val)
      if (name ~ /^[A-Za-z_][A-Za-z0-9_]*$/) printf "  %-42s %s\n", name, (length(val) > 0 ? "[set]" : "[EMPTY]") }
  ' "$1" 2>/dev/null
}
# log/journal matnidan faqat kategoriyalar soni (matn chiqarilmaydi)
cat_counts() {
  perl -ne '
    $n++;
    $c{"error"}++              if /\berror\b/i;
    $c{"warning"}++            if /\bwarn(ing)?\b/i;
    $c{"critical/fatal"}++     if /\b(critical|fatal|panic)\b/i;
    $c{"traceback"}++          if /Traceback \(most recent call last\)/;
    $c{"timeout"}++            if /time(d)?[ _-]?out/i;
    $c{"auth 401/403"}++       if /\b(401|403)\b|unauthori[sz]ed|forbidden|invalid[ _]api[ _]key/i;
    $c{"rate-limit 429"}++     if /\b429\b|rate[ _-]?limit|quota/i;
    $c{"telegram 409"}++       if /\b409\b|terminated by other getupdates/i;
    $c{"network"}++            if /connection (refused|reset|error|aborted)|name resolution|network is unreachable/i;
    $c{"memory/oom"}++         if /out of memory|memoryerror|\boom\b|killed process/i;
    $c{"systemd start/stop"}++ if /^(Started|Stopped|Stopping|Starting|Failed) /;
    while (/\b([A-Z][A-Za-z0-9]{2,40}(?:Error|Exception))\b/g) { $e{$1}++ }
    END {
      printf "  jami qatorlar: %d\n", $n+0;
      for my $k (sort keys %c) { printf "  %-22s %d\n", $k, $c{$k} }
      my @t = grep { defined } (sort { $e{$b} <=> $e{$a} } keys %e)[0..9];
      if (@t) { print "  eng ko\x27p exception turlari:\n"; printf "    %-40s %d\n", $_, $e{$_} for @t }
    }'
}
# Ikkinchi qatlam: regex filtr
redact() {
  perl -ne '
    BEGIN { $pem = 0; $h = $ENV{HOST_RAW} // "" }
    if (/-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/) { $pem = 1; print "***PRIVATE_KEY_BLOCK***\n"; next }
    if ($pem) { $pem = 0 if /-----END [A-Z0-9 ]*PRIVATE KEY-----/; next }
    s/\Q$h\E/<host>/g if length($h) >= 3;
    s/\b(sk-(?:ant-|proj-|or-|svcacct-)?)[A-Za-z0-9_\-]{8,}/${1}***/g;
    s/\b\d{6,12}:[A-Za-z0-9_\-]{30,}/<telegram-token>/g;
    s/\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}/<jwt>/g;
    s/\b(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|xox[abposr]-|AIza|AKIA|ASIA|hf_|gsk_|r8_|pplx-|nvapi-|tvly-)[A-Za-z0-9_\-]{8,}/${1}***/g;
    s{([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@}{${1}***:***@}g;
    s/(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9._~+\/=\-]{8,}/${1}***/g;
    s/(?i)([A-Za-z0-9_.\-]*(?:api[_\-]?key|apikey|token|secret|passw|pwd|cookie|credential|private[_\-]?key|session[_\-]?id|signature|salt)[A-Za-z0-9_.\-]*["\x27]?\s*[:=]\s*["\x27]?)(?!(?:-?\d+(?:\.\d+)?|true|false|null|none|~)["\x27]?(?:[,}\s#]|$))(?!["\x27]?\s*$)(?!<|\*\*\*|\[)[^\s"\x27,}#]+/${1}***/g;
    s/\b[\w.+\-]+@(?![\w.\-]*\.(?:service|socket|timer|mount|target|slice|scope|path|device|swap)\b)[A-Za-z0-9][\w\-]*(?:\.[\w\-]+)*\.[A-Za-z]{2,24}\b/<email>/g;
    s/(?<![\d.])(?!127\.)(?!0\.0\.0\.0(?![\d.]))(?:\d{1,3}\.){3}\d{1,3}(?![\d.])/<IP>/g;
    s/(?<![\w:.])(?:(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}|(?:[0-9a-fA-F]{1,4}:){1,6}:(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4})(?![\w:])/<IPv6>/g;
    s/(?<![\w.:\/\-])\d{7,}(?![\w.])/<raqam>/g;
    s/\b[A-Za-z0-9+_\-]{40,}={0,2}/<uzun-maxfiy-satr>/g;
    print;'
}
# Uchinchi qatlam: filtrdan keyin ham xom sir shakli qolsa — 3 qaytaradi
leakcheck() {
  perl -ne '
    $f = 1 if /\bsk-[A-Za-z0-9_\-]{12,}/ || /\b\d{6,12}:[A-Za-z0-9_\-]{30,}/ || /\beyJ[A-Za-z0-9_\-]{10,}\./
           || /\b(?:ghp_|github_pat_|xox[abpors]-|AKIA)[A-Za-z0-9_\-]{10,}/ || /\bAIza[0-9A-Za-z_\-]{20,}/
           || /-----BEGIN [A-Z0-9 ]*PRIVATE KEY/ || m{://[^/\s:@*<]+:[^/\s@*<]+@} || /(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}/;
    END { exit($f ? 3 : 0) }'
}

# Python: konfiguratsiyani ALLOWLIST bo'yicha ko'rsatish (qiymat faqat xavfsiz shaklda bo'lsa)
PY_CFG='
import sys, re, json
p = sys.argv[1]; want = (sys.argv[2] if len(sys.argv) > 2 else "").lower()
SENS = re.compile(r"(key|token|secret|passw|pwd|cookie|credential|auth|private|session|signature|salt|cert|bearer|webhook|chat|user|phone|email|owner|admin|allow|whitelist|home_channel|_id$|^id$)", re.I)
URLK = re.compile(r"(url|endpoint|host|base)$", re.I)
SAFE = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,64}$")
lines = [0]
def out(s):
    lines[0] += 1
    if lines[0] <= 400: print(s)
def secretish(s):
    if re.search(r"(sk-|eyJ|ghp_|github_pat_|AIza|xox[abposr]-|-----BEGIN|://[^/]*@)", s): return True
    if re.fullmatch(r"[0-9a-fA-F]{24,}", s): return True
    if len(s) >= 20 and re.fullmatch(r"[A-Za-z0-9_\-+/=]+", s) and re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"[0-9]", s): return True
    return bool(re.search(r"\d{7,}", s))
def scalar(k, v):
    if isinstance(v, bool) or v is None: return str(v)
    if isinstance(v, (int, float)) and abs(v) < 1000000 and re.search(r"(^|_)(max|min)_|tokens$|_limit$|_budget$", k, re.I): return str(v)
    if SENS.search(k): return "<yashirilgan %s>" % type(v).__name__
    if isinstance(v, (int, float)):
        return ("<son, %d xonali>" % len(str(abs(int(v))))) if abs(v) >= 1000000 else str(v)
    if isinstance(v, str):
        if URLK.search(k):
            m = re.match(r"^([a-z][a-z0-9+.\-]*://)(?:[^/@]*@)?([^/:?#]+)", v, re.I)
            if m:
                host = m.group(2)
                if re.fullmatch(r"[\d.]+", host) and not host.startswith("127."): host = "<IP>"
                return m.group(1) + host + "/..."
        if v.startswith("/") and SAFE.match(v): return v
        if SAFE.match(v) and not secretish(v): return v
        return "<matn, %d belgi>" % len(v)
    return "<%s>" % type(v).__name__
def walk(k, v, d):
    ind = "  " * d
    if isinstance(v, dict):
        out("%s%s:" % (ind, k))
        for kk, vv in v.items(): walk(str(kk), vv, d + 1)
    elif isinstance(v, list):
        if SENS.search(k): out("%s%s: <roʻyxat, %d element — yashirilgan>" % (ind, k, len(v)))
        elif v and all(isinstance(x, (str, int, float, bool)) for x in v) and len(v) <= 15:
            out("%s%s: [%s]" % (ind, k, ", ".join(scalar(k, x) for x in v)))
        else:
            out("%s%s: <roʻyxat, %d element>" % (ind, k, len(v)))
            for i, x in enumerate(v[:10]):
                if isinstance(x, (dict, list)): walk("%s[%d]" % (k, i), x, d + 1)
    else:
        out("%s%s: %s" % (ind, k, scalar(k, v)))
def topkeys_only(txt):
    out("  (toʻliq tahlil imkoni yoʻq — faqat yuqori darajadagi kalit nomlari)")
    for ln in txt.splitlines():
        m = re.match(r"^([A-Za-z_][\w\-]*)\s*:", ln)
        if m: out("  " + m.group(1))
try:
    txt = open(p, encoding="utf-8", errors="replace").read()
except Exception as e:
    print("  oʻqib boʻlmadi:", type(e).__name__); sys.exit(0)
data = None
try:
    if p.endswith(".json"): data = json.loads(txt)
    else:
        import yaml
        data = yaml.safe_load(txt)
except ImportError:
    topkeys_only(txt); sys.exit(0)
except Exception as e:
    print("  parse xatosi:", type(e).__name__); topkeys_only(txt); sys.exit(0)
if not isinstance(data, dict):
    print("  (boʻsh yoki lugʻat emas)"); sys.exit(0)
if want:
    hit = False
    for k, v in data.items():
        if want in str(k).lower(): walk(str(k), v, 1); hit = True
        elif isinstance(v, dict):
            for kk, vv in v.items():
                if want in str(kk).lower(): walk(str(k) + "." + str(kk), vv, 1); hit = True
    if not hit: print("  (\"%s\" boʻyicha sozlama topilmadi)" % want)
else:
    for k, v in data.items(): walk(str(k), v, 1)
if lines[0] > 400: print("  ... (%d qatordan 400 tasi koʻrsatildi)" % lines[0])
'
PY_SQLITE='
import sqlite3, sys, urllib.parse
p = sys.argv[1]
try:
    con = sqlite3.connect("file:" + urllib.parse.quote(p) + "?mode=ro&immutable=1", uri=True)
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type in (?, ?) order by name", ("table", "view"))]
    print("  jadvallar soni:", len(tabs))
    for t in tabs[:40]:
        try: n = "{:_}".format(con.execute("select count(*) from [" + t.replace("]", "]]") + "]").fetchone()[0])
        except Exception as e: n = "? (" + type(e).__name__ + ")"
        print("  -", t + ":", n)
    if len(tabs) > 40: print("  ... yana", len(tabs) - 40, "ta")
except Exception as e:
    print("  oʻqib boʻlmadi:", type(e).__name__)
'
PY_PKG='
import importlib.metadata as m, sys
mode = sys.argv[1]
ds = {}
for d in m.distributions():
    n = d.metadata["Name"] or "?"
    ds[n] = d
if mode == "hermes":
    print("  python:", sys.version.split()[0], "| prefix:", sys.prefix)
    for n, d in ds.items():
        if "hermes" in n.lower():
            files = d.files or []
            print("  paket:", n + "==" + d.version, "| ichidagi SKILL.md:", sum(1 for f in files if str(f).endswith("SKILL.md")))
elif mode == "key":
    low = {k.lower(): v.version for k, v in ds.items()}
    print("  jami paketlar:", len(low))
    for k in ["openai","anthropic","google-genai","google-generativeai","litellm","httpx","pydantic","python-telegram-bot","aiogram","playwright","fastapi","uvicorn","pyyaml","tiktoken","mcp","numpy","pandas","honcho-ai"]:
        if k in low: print("   ", k + "==" + low[k])
else:
    for n in sorted(ds, key=str.lower): print("   ", n + "==" + ds[n].version)
'
export PY_CFG PY_SQLITE PY_PKG

# =============================================================================
#  BO'LIMLAR
# =============================================================================
sec1() {
  sub "Operatsion tizim"
  grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release 2>/dev/null
  echo "kernel: $(uname -r)   arch: $(uname -m)   hostname: <yashirilgan>"
  echo "uptime: $(uptime -p 2>/dev/null || echo '?')   loadavg: $(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
  sub "CPU"
  echo "vCPU: $(nproc 2>/dev/null)"
  have lscpu && lscpu 2>/dev/null | grep -E '^(Model name|Hypervisor vendor|Virtualization type)'
  sub "RAM va swap"
  if have free; then free -h; else grep -E 'MemTotal|MemAvailable|SwapTotal' /proc/meminfo; fi
  local swap avail tot
  swap=$(awk '/SwapTotal/{print $2}' /proc/meminfo); avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  tot=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
  [ "${swap:-0}" -eq 0 ] && sum "WARN  Swap yo'q — 8 GB RAM'da brauzer/Docker yuklamasida OOM xavfi" || sum "OK    Swap mavjud ($(hsize $((swap*1024))))"
  sum "INFO  RAM: jami ${tot} MB, hozir bo'sh ${avail} MB"
  sub "Disklar"
  if have df; then df -hT -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null
    local u; u=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
    if [ "${u:-0}" -ge 80 ]; then sum "WARN  / disk ${u}% band"; else sum "OK    / disk ${u}% band"; fi
  fi
  sub "RAM bo'yicha 15 ta eng katta jarayon (faqat nom, argumentlar chiqarilmaydi)"
  if have ps; then ps -eo user,rss,%mem,%cpu,etime,comm --sort=-rss 2>/dev/null | head -n 16 |
    awk 'NR==1{printf "  %-10s %9s %5s %5s %12s %s\n",$1,"RSS",$3,$4,$5,$6;next}{printf "  %-10s %7.0fMB %5s %5s %12s %s\n",$1,$2/1024,$3,$4,$5,$6}'; fi
  sub "Vaqt sinxronizatsiyasi"
  if have timedatectl; then
    local td; td="$(timeout "$T" timedatectl show -p Timezone -p NTPSynchronized 2>/dev/null)"
    echo "${td:-  (aniqlanmadi)}" | sed 's/^/  /'
    printf '%s' "$td" | grep -q 'NTPSynchronized=no' && sum "WARN  Vaqt NTP bilan sinxron emas"
  else echo "  [timedatectl mavjud emas]"; fi
  sub "Yangilanishlar"
  echo "  unattended-upgrades: $(systemctl is-enabled unattended-upgrades 2>/dev/null || echo 'aniqlanmadi')"
  if [ -f /var/run/reboot-required ]; then echo "  reboot talab qilinadi"; sum "INFO  Server qayta yuklashni kutmoqda (reboot-required)"; else echo "  reboot-required: yo'q"; fi
}

sec2() {
  echo "hermes buyrug'i : ${HERMES_BIN:-TOPILMADI}"
  [ -n "$HERMES_BIN" ] && echo "haqiqiy yo'l    : $(hp "$(readlink -f "$HERMES_BIN")")"
  echo "Hermes Python   : ${HERMES_PY:-aniqlanmadi}"
  echo "(hermes CLI ishga tushirilmaydi — versiya paket metama'lumotidan o'qiladi)"
  if [ -n "$HERMES_PY" ] && [ -x "$HERMES_PY" ]; then
    sub "Hermes paketi"; timeout "$T" "$HERMES_PY" -I -B -c "$PY_PKG" hermes 2>&1
    local v; v="$("$HERMES_PY" -I -B -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)"
    sum "INFO  Hermes Python: ${v:-?}"
  else
    sum "WARN  Hermes Python muhiti aniqlanmadi"
  fi
  local src
  for src in "$HERMES_HOME/hermes-agent" "$HOME/hermes-agent"; do
    [ -d "$src/.git" ] || continue
    sub "Hermes manba repozitoriysi: $(hp "$src") (remote ko'rsatilmaydi)"
    echo "  branch : $(gitq "$src" rev-parse --abbrev-ref HEAD)"
    echo "  commit : $(gitq "$src" log -1 --format='%h %cs')"
    echo "  tag    : $(gitq "$src" describe --tags --always)"
    echo "  o'zgargan tracked fayllar: $(gitq "$src" status --porcelain --untracked-files=no | wc -l)"
  done
  sub "HERMES_HOME yuqori darajasi (nom, ruxsat, hajm)"
  if [ ! -d "$HERMES_HOME" ]; then echo "  $(hp "$HERMES_HOME") mavjud emas"; sum "WARN  HERMES_HOME topilmadi"; return; fi
  local perm; perm=$(stat -c %a "$HERMES_HOME")
  case "$perm" in 700|750) sum "OK    ~/.hermes ruxsati $perm";; *) sum "WARN  ~/.hermes ruxsati $perm (700 tavsiya etiladi)";; esac
  local e n
  for e in "$HERMES_HOME"/* "$HERMES_HOME"/.[!.]*; do
    [ -e "$e" ] || continue
    if [ -d "$e" ]; then
      n=$(timeout "$T" find "$e" -type f 2>/dev/null | wc -l)
      printf '  %-4s %-30s %8s  (%s fayl)\n' "$(stat -c %a "$e")" "$(basename "$e")/" "$(timeout "$T" du -sh "$e" 2>/dev/null | cut -f1)" "$n"
    else
      printf '  %-4s %-30s %8s\n' "$(stat -c %a "$e")" "$(basename "$e")" "$(hsize "$(stat -c %s "$e")")"
    fi
  done
}

sec3() {
  local f py
  py="${HERMES_PY:-}"; [ -x "$py" ] || py="$(command -v python3 2>/dev/null)"
  while IFS= read -r f; do
    sub "Fayl: $(hp "$f")"; meta "$f"
    if is_sens "$f"; then echo "  [mazmun o'qilmaydi — maxfiy fayl nomi]"; continue; fi
    case "$f" in
      *.toml) echo "  (TOML: faqat bo'lim/kalit nomlari)"; grep -oE '^\s*(\[[^]]+\]|[A-Za-z_][A-Za-z0-9_-]*)\s*' "$f" | grep -v '^\s*$' | head -n 80 ;;
      *) if [ -n "$py" ]; then timeout "$T" "$py" -I -B -c "$PY_CFG" "$f" 2>&1
         else echo "  (python yo'q — faqat yuqori daraja kalit nomlari)"; grep -oE '^[A-Za-z_][A-Za-z0-9_-]*:' "$f" | head -n 80; fi ;;
    esac
  done < <(ff "$HERMES_HOME" 1 -type f \( -name '*.yaml' -o -name '*.yml' -o -name '*.json' -o -name '*.toml' \) -print | sort)
  sub ".env fayllari — FAQAT nom va [set]/[EMPTY] (qiymatlar chiqarilmaydi)"
  if [ -z "$ENV_FILES" ]; then echo "  (.env topilmadi)"; fi
  local p
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    meta "$f"; p=$(stat -c %a "$f")
    case "$p" in 600|400) sum "OK    $(hp "$f") ruxsati $p";; *) sum "WARN  $(hp "$f") ruxsati $p (600 bo'lishi kerak)";; esac
    env_names "$f"
  done < <(printf '%s\n' "$ENV_FILES")
  local provs="" k
  for k in OPENAI ANTHROPIC CLAUDE GEMINI GOOGLE DEEPSEEK OPENROUTER GROQ MISTRAL XAI NOUS TOGETHER FIREWORKS; do
    printf ' %s ' "$ENV_SET_NAMES" | grep -qE " ${k}[A-Z0-9_]*(KEY|TOKEN) " && provs+="$k "
  done
  echo; echo "Kaliti o'rnatilgan provayderlar (.env nomlaridan): ${provs:-topilmadi}"
  sum "INFO  Kaliti bor provayderlar: ${provs:-topilmadi}"
  sub "Maxfiy nomli fayllar (faqat nom va ruxsat)"
  ff "$HERMES_HOME" 3 -type f -print | while IFS= read -r f; do
    is_sens "$f" || continue
    case "$(basename "$f")" in .env|.env.*|*.env) continue;; esac   # .env yuqorida tekshirildi
    meta "$f"
    p=$(stat -c %a "$f"); case "$p" in 600|400) ;; *) echo "@@SUM@@WARN  $(hp "$f") ruxsati $p (600 tavsiya etiladi)";; esac
  done
}

sec4() {
  local f="$HERMES_HOME/SOUL.md"
  if [ ! -f "$f" ]; then echo "SOUL.md topilmadi"; sum "WARN  SOUL.md topilmadi"; return; fi
  meta "$f"
  echo "  sha256 (16): $(fhash "$f")   — keyingi o'zgarishlarni kuzatish uchun"
  echo "  qatorlar: $(wc -l < "$f")   so'zlar: $(wc -w < "$f")   belgilar: $(wc -m < "$f")"
  perl -CSD -ne '$l += () = /\p{Latin}/g; $c += () = /\p{Cyrillic}/g; END { printf "  harflar: lotin=%d kirill=%d\n", $l, $c }' "$f" 2>/dev/null
  echo "  sarlavhalar soni: $(grep -cE '^#{1,6} ' "$f")   ro'yxat bandlari: $(grep -cE '^[[:space:]]*([-*]|[0-9]+\.) ' "$f")"
  sub "Sarlavhalar tuzilmasi (60 belgigacha; matn chiqarilmaydi)"
  grep -E '^#{1,6} ' "$f" | head -n 40 | cut -c1-60 | sed 's/^/  /'
}

sec5() {
  sub "Xotira kataloglari"
  local d f n
  while IFS= read -r d; do
    n=$(timeout "$T" find "$d" -type f 2>/dev/null | wc -l)
    echo "  $(hp "$d")/  ($n fayl, $(timeout "$T" du -sh "$d" 2>/dev/null | cut -f1))"
  done < <(ff "$HERMES_HOME" 2 -type d \( -iname '*memor*' -o -iname 'sessions' -o -iname '*honcho*' \) -print)
  sub "Markdown xotira fayllari (mazmun chiqarilmaydi — faqat statistika)"
  while IFS= read -r f; do
    [ "$(basename "$f")" = SOUL.md ] && continue
    case "$f" in "$HERMES_HOME/skills/"*|"$HERMES_HOME/hermes-agent/"*) continue;; esac
    meta "$f"
    echo "    sha256(16)=$(fhash "$f")  qatorlar=$(wc -l < "$f")  belgilar=$(wc -m < "$f")  yozuvlar=$(grep -cE '^[[:space:]]*([-*]|§)' "$f")"
  done < <(ff "$HERMES_HOME" 3 -type f -iname '*.md' -print | sort)
  sub "SQLite bazalari (faqat o'qish, immutable rejim: jadval nomlari va qatorlar soni)"
  local py; py="${HERMES_PY:-}"; [ -x "$py" ] || py="$(command -v python3 2>/dev/null)"
  local found=0
  while IFS= read -r f; do
    found=1; meta "$f"
    if [ -n "$py" ]; then timeout "$T" "$py" -I -B -c "$PY_SQLITE" "$f" 2>&1; else echo "  [python mavjud emas]"; fi
  done < <(ff "$HERMES_HOME" 3 -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) -print)
  [ "$found" = 0 ] && echo "  (SQLite fayl topilmadi)"
}

sec6() {
  local SK="$HERMES_HOME/skills" f rel desc cnt
  if [ ! -d "$SK" ]; then echo "$(hp "$SK") topilmadi"; sum "WARN  skills katalogi topilmadi"; return; fi
  cnt=$(timeout "$T" find "$SK" -name SKILL.md 2>/dev/null | wc -l)
  echo "~/.hermes/skills ichidagi SKILL.md soni: $cnt"
  sum "INFO  Skills: $cnt ta (~/.hermes/skills)"
  sub "Kategoriyalar (skill soni)"
  for f in "$SK"/*/; do [ -d "$f" ] && printf '  %-30s %s\n' "$(basename "$f")" "$(find "$f" -name SKILL.md 2>/dev/null | wc -l)"; done
  sub "Skill ro'yxati (kategoriya/nom | SKILL.md hajmi | oxirgi o'zgarish) — tavsif matni chiqarilmaydi"
  timeout "$T" find "$SK" -name SKILL.md 2>/dev/null | sort | while IFS= read -r f; do
    rel="${f#"$SK"/}"; rel="${rel%/SKILL.md}"
    printf '  %-45s | %8s | %s\n' "$rel" "$(hsize "$(stat -c %s "$f")")" "$(date -r "$f" '+%F')"
  done
}

sec7() {
  local me L u st pids p cmd gw=0
  me="$(id -un)"
  have loginctl && L="$(timeout "$T" loginctl show-user "$me" -p Linger --value 2>/dev/null)"
  echo "Linger ($me): ${L:-aniqlanmadi}"
  if [ "$L" = yes ]; then sum "OK    Linger yoqilgan — user service logout/reboot'dan keyin ham ishlaydi"
  else sum "WARN  Linger: ${L:-aniqlanmadi} — SSH'dan chiqqanda yoki reboot'da gateway to'xtashi mumkin"; fi
  if ! have systemctl; then echo "[systemctl mavjud emas — systemd tekshiruvlari o'tkazib yuborildi]"; sum "WARN  systemctl mavjud emas"; return; fi
  sub "Yoqilgan user service'lar (nomlar)"
  timeout "$T" systemctl --user list-unit-files --type=service --state=enabled --no-legend --no-pager 2>&1 | awk '{print "  "$1}'
  sub "User timer'lar (nom, holat)"
  timeout "$T" systemctl --user list-units --type=timer --all --no-legend --no-pager --plain 2>&1 | awk '{print "  "$1, $3, $4}'
  local units
  units="$( { systemctl --user list-unit-files --type=service --no-legend --no-pager; systemctl --user list-units --type=service --all --no-legend --no-pager --plain; } 2>/dev/null \
      | awk '{print $1}' | grep -iE 'hermes|gateway' | grep -vF "$EXCL_UNIT" | grep -v '@\.service$' | sort -u)"
  [ -z "$units" ] && { echo; echo "Hermes bilan bog'liq user unit topilmadi"; sum "WARN  Hermes gateway user unit topilmadi"; }
  for u in $units; do
    sub "Unit: $u (allowlist xususiyatlar; Environment o'qilmaydi)"
    timeout "$T" systemctl --user show "$u" --no-pager -p LoadState -p ActiveState -p SubState -p UnitFileState -p NRestarts \
      -p Restart -p RestartUSec -p ActiveEnterTimestamp -p MemoryCurrent -p MemoryPeak -p MemoryMax -p TasksCurrent -p FragmentPath -p WorkingDirectory 2>&1 |
      awk -F= '/^Memory(Current|Peak|Max)=[0-9]+$/ {printf "  %s=%.1fMB\n",$1,$2/1048576; next} {print "  "$0}'
    echo "  ExecStart (faqat dastur yo'li): $(timeout "$T" systemctl --user show "$u" -p ExecStart --value 2>/dev/null | grep -oE 'path=[^ ;]+' | head -n1 | cut -d= -f2)"
    echo "  EnvironmentFiles (faqat yo'l): $(timeout "$T" systemctl --user show "$u" -p EnvironmentFiles --value 2>/dev/null | awk '$1 ~ /^\/[A-Za-z0-9._\/-]+$/ {print $1}' | tr '\n' ' ')"
    st="$(systemctl --user is-active "$u" 2>/dev/null)"
    if [ "$st" = active ]; then sum "OK    $u: active"; else sum "WARN  $u: ${st:-unknown}"; fi
    local nr; nr="$(systemctl --user show "$u" -p NRestarts --value 2>/dev/null)"
    [ "${nr:-0}" -gt 5 ] 2>/dev/null && sum "WARN  $u: $nr marta avtomatik qayta ishga tushgan"
    if have journalctl; then
      sub "Jurnal, 7 kun — faqat kategoriyalar soni (matn chiqarilmaydi)"
      timeout "$T" journalctl --user -u "$u" --since "-7d" --no-pager -o cat 2>/dev/null | cat_counts
    fi
  done
  sub "Tizim darajasidagi Hermes unitlari"
  timeout "$T" systemctl list-unit-files --type=service --no-legend --no-pager 2>/dev/null | awk '{print $1}' | grep -iE 'hermes' | sed 's/^/  /' || echo "  (yo'q)"
  sub "Himoyalangan xizmat: $EXCL_UNIT (faqat holat o'qiladi)"
  local scope any=0 ld en mem
  for scope in --user --system; do
    ld="$(timeout "$T" systemctl "$scope" show "$EXCL_UNIT" -p LoadState --value 2>/dev/null)"
    [ "$ld" = loaded ] || continue
    any=1
    st="$(systemctl "$scope" is-active "$EXCL_UNIT" 2>/dev/null)"; en="$(systemctl "$scope" is-enabled "$EXCL_UNIT" 2>/dev/null)"
    mem="$(systemctl "$scope" show "$EXCL_UNIT" -p MemoryCurrent --value 2>/dev/null)"
    [[ "$mem" =~ ^[0-9]+$ ]] && mem="$(hsize "$mem")"
    echo "  ${scope#--}: active=$st enabled=$en RAM=${mem:-?}"
    sum "INFO  $EXCL_UNIT (${scope#--}): $st, RAM ${mem:-?} — tegilmadi"
  done
  [ "$any" = 0 ] && echo "  (topilmadi)"
  sub "Hermes jarayonlari (argumentlarsiz)"
  pids=""
  for p in $(pgrep -f -i 'hermes' 2>/dev/null); do
    [ -r "/proc/$p/cmdline" ] || continue
    cmd="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
    case "$cmd" in *hermes_audit*|*pgrep*) continue;; esac
    case "$cmd" in *gateway*) gw=$((gw+1));; esac
    pids+="$p,"
  done
  if [ -n "$pids" ] && have ps; then
    ps -o user,rss,%mem,%cpu,etime,comm -p "${pids%,}" 2>/dev/null |
      awk 'NR==1{printf "  %-10s %9s %5s %5s %12s %s\n",$1,"RSS",$3,$4,$5,$6;next}{printf "  %-10s %7.0fMB %5s %5s %12s %s\n",$1,$2/1024,$3,$4,$5,$6}'
  else echo "  (hermes jarayoni topilmadi)"; fi
  echo "  'gateway' jarayonlari soni: $gw"
  [ "$gw" -gt 1 ] && sum "WARN  $gw ta gateway jarayoni — Telegram 409 Conflict xavfi"
}

sec8() {
  sub "Telegram bilan bog'liq .env o'zgaruvchilari (faqat nom)"
  printf '%s\n' $ENV_SET_NAMES | grep -iE 'telegram|^TG_' | sed 's/^/  /'
  local py; py="${HERMES_PY:-}"; [ -x "$py" ] || py="$(command -v python3 2>/dev/null)"
  if [ -f "$HERMES_HOME/config.yaml" ] && [ -n "$py" ]; then
    sub "config.yaml — telegram sozlamalari (ID va tokenlar yashirilgan)"
    timeout "$T" "$py" -I -B -c "$PY_CFG" "$HERMES_HOME/config.yaml" telegram 2>&1
  fi
  sub "409 Conflict / 401 belgilari (faqat son)"
  local u total=0 c units
  if have journalctl && have systemctl; then
    units="$(systemctl --user list-units --type=service --all --no-legend --no-pager --plain 2>/dev/null | awk '{print $1}' | grep -iE 'hermes|gateway' | grep -vF "$EXCL_UNIT")"
    for u in $units; do
      c=$(timeout "$T" journalctl --user -u "$u" --since "-7d" --no-pager -o cat 2>/dev/null | grep -ciE '\b409\b|terminated by other getUpdates')
      echo "  $u jurnalida 409 (7 kun): ${c:-0}"; total=$((total + ${c:-0}))
    done
  fi
  if [ -d "$HERMES_HOME/logs" ]; then
    c=$(timeout "$T" grep -rciE '\b409\b|terminated by other getUpdates' "$HERMES_HOME/logs" 2>/dev/null | awk -F: '{s+=$NF} END{print s+0}')
    echo "  ~/.hermes/logs ichida 409: $c"; total=$((total + c))
  fi
  if [ "$total" -gt 0 ]; then sum "WARN  Telegram 409 Conflict: $total marta — bitta token ikki joyda ishlatilayotgan bo'lishi mumkin"
  else sum "OK    Telegram 409 Conflict belgisi topilmadi"; fi
  echo "  Eslatma: $EXCL_NAME ham Telegram bot ishlatsa, tokenlar har xil ekanini o'zingiz tekshiring (audit uning fayllarini o'qimaydi)."
  echo "  Telegram API'ga so'rov yuborilmadi."
}

sec9() {
  local LOGD="$HERMES_HOME/logs" f
  if [ ! -d "$LOGD" ]; then echo "$(hp "$LOGD") topilmadi"; return; fi
  sub "Log fayllari"
  ls -1t "$LOGD" 2>/dev/null | head -n 20 | while IFS= read -r f; do [ -f "$LOGD/$f" ] && meta "$LOGD/$f"; done
  ls -1t "$LOGD" 2>/dev/null | head -n 6 | while IFS= read -r f; do
    [ -f "$LOGD/$f" ] || continue
    sub "$f — kategoriyalar soni (matn chiqarilmaydi)"
    timeout "$T" tail -n 50000 "$LOGD/$f" 2>/dev/null | cat_counts
  done
}

sec10() {
  sub "Runtime'lar"
  local b
  for b in python3 python3.12 python3.13 python3.14 pip3 uv pipx node npm bun git docker perl; do
    if have "$b"; then printf '  %-10s %-40s %s\n' "$b" "$("$b" --version 2>&1 | head -n1 | cut -c1-40)" "$(command -v "$b")"
    else printf '  %-10s [mavjud emas]\n' "$b"; fi
  done
  if [ -n "$HERMES_PY" ] && [ -x "$HERMES_PY" ]; then
    sub "Hermes venv — asosiy paketlar"; timeout "$T" "$HERMES_PY" -I -B -c "$PY_PKG" key 2>&1
    sub "Hermes venv — to'liq ro'yxat"; timeout "$T" "$HERMES_PY" -I -B -c "$PY_PKG" all 2>&1 | head -n 400
  fi
  sub "Playwright brauzerlari"
  if [ -d "$HOME/.cache/ms-playwright" ]; then echo "  $(timeout "$T" du -sh "$HOME/.cache/ms-playwright" 2>/dev/null | cut -f1)  $(ls -1 "$HOME/.cache/ms-playwright" 2>/dev/null | tr '\n' ' ')"
  else echo "  ~/.cache/ms-playwright yo'q"; fi
}

sec11() {
  if ! have docker; then echo "docker mavjud emas"; sum "INFO  Docker o'rnatilmagan"; return; fi
  if ! timeout 10 docker info >/dev/null 2>&1; then
    echo "Docker bor, lekin daemon'ga ulanib bo'lmadi (ruxsat yo'q yoki ishlamayapti)"
    sum "WARN  Docker daemon'ga bu foydalanuvchidan ulanib bo'lmadi"; return
  fi
  sum "OK    Docker ishlayapti va foydalanuvchiga ruxsat bor"
  run_ro "Docker" docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
  run_ro "Umumiy" docker info --format 'containers={{.Containers}} running={{.ContainersRunning}} images={{.Images}} storage={{.Driver}} cgroup={{.CgroupVersion}}'
  run_ro "Konteynerlar" docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
  run_ro "Compose loyihalari" docker compose ls -a
  run_ro "Tarmoqlar" docker network ls --format 'table {{.Name}}\t{{.Driver}}\t{{.Scope}}'
  run_ro "Volume'lar" docker volume ls --format 'table {{.Name}}\t{{.Driver}}'
  run_ro "Disk sarfi" docker system df
  run_ro "Resurs sarfi (bir martalik)" docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
  local pub; pub=$(timeout "$T" docker ps --format '{{.Ports}}' 2>/dev/null | grep -cE '0\.0\.0\.0:|\[::\]:|:::')
  [ "${pub:-0}" -gt 0 ] && sum "WARN  $pub ta konteyner portni barcha interfeyslarga ochgan — Docker UFW qoidalarini chetlab o'tadi"
}

sec12() {
  sub "Interfeyslar (manzillar ko'rsatilmaydi — faqat soni)"
  if have ip; then ip -br addr 2>/dev/null | awk '{printf "  %-16s %-8s manzillar: %d\n",$1,$2,NF-2}'; else echo "  [ip mavjud emas]"; fi
  sub "Tinglayotgan portlar"
  if ! have ss; then echo "  [ss mavjud emas]"; return; fi
  printf '  %-5s %-7s %-16s %s\n' PROTO PORT SCOPE PROCESS
  timeout "$T" ss -H -tulpn 2>/dev/null | awk '{
    a=""; ai=0; for(i=1;i<=NF;i++) if ($i ~ /:[0-9]+$/) { a=$i; ai=i; break }
    if (ai == 0) next
    port=a; sub(/.*:/,"",port); proc=""; for(i=ai+2;i<=NF;i++) proc=proc" "$i
    name="?"; if (match(proc, /\(\("[^"]+"/)) name=substr(proc, RSTART+3, RLENGTH-4)
    if (a ~ /^(127\.|\[::1\]|::1|\[::ffff:127\.)/) s="local-only"
    else if (a ~ /^(0\.0\.0\.0|\*|\[::\]|::):/) s="ALL-INTERFACES"
    else s="SPECIFIC-IP"
    printf "  %-5s %-7s %-16s %s\n", $1, port, s, name }' | sort -u -k3,3 -k2,2n
  local open; open=$(timeout "$T" ss -H -tln 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i ~ /:[0-9]+$/) {print $i; break}}' | grep -cvE '^(127\.|\[::1\]|::1|\[::ffff:127\.)')
  sum "INFO  Tashqi interfeysda tinglayotgan TCP portlar: $open ta"
  echo "  (Boshqa foydalanuvchi jarayonlari nomi sudo'siz ko'rinmaydi — '?' normal.)"
}

sec13() {
  sub "SSH server (faqat allowlist direktivalar)"
  local files; files="$(ls /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null)"
  if [ -n "$files" ]; then
    # shellcheck disable=SC2086
    grep -hEi '^\s*(Port|PermitRootLogin|PasswordAuthentication|PubkeyAuthentication|KbdInteractiveAuthentication|MaxAuthTries|X11Forwarding)\b' $files 2>/dev/null | sed 's/^/  /'
    # shellcheck disable=SC2086
    echo "  AllowUsers qatorlari: $(grep -hciE '^\s*AllowUsers\b' $files 2>/dev/null | awk '{s+=$1} END{print s+0}')"
    # shellcheck disable=SC2086
    grep -hEiq '^\s*PasswordAuthentication\s+yes' $files 2>/dev/null && sum "WARN  SSH parol bilan kirish yoqilgan"
    # shellcheck disable=SC2086
    grep -hEiq '^\s*PermitRootLogin\s+yes' $files 2>/dev/null && sum "WARN  SSH orqali root kirishi yoqilgan"
  else echo "  [sshd_config o'qib bo'lmadi]"; fi
  sub "Firewall"
  echo "  ufw.conf: $(grep -E '^ENABLED=' /etc/ufw/ufw.conf 2>/dev/null || echo 'topilmadi')"
  echo "  ufw service: $(systemctl is-active ufw 2>/dev/null || echo '?')   fail2ban: $(systemctl is-active fail2ban 2>/dev/null || echo '?')"
  echo "  (UFW qoidalari ro'yxati root talab qiladi; bu audit uni ko'rmaydi.)"
  if grep -q '^ENABLED=yes' /etc/ufw/ufw.conf 2>/dev/null; then sum "OK    UFW yoqilgan"; else sum "WARN  UFW yoqilmagan yoki aniqlanmadi"; fi
  [ "$(systemctl is-active fail2ban 2>/dev/null)" = active ] || sum "INFO  fail2ban faol emas"
  sub "~/.ssh (faqat nom va ruxsat)"
  if [ -d "$HOME/.ssh" ]; then
    local sp; sp="$(stat -c %a "$HOME/.ssh")"; echo "  ~/.ssh: $sp"
    [ "$sp" = 700 ] || sum "WARN  ~/.ssh ruxsati $sp (700 bo'lishi kerak)"
    for f in "$HOME"/.ssh/*; do
      [ -e "$f" ] || continue
      p="$(stat -c %a "$f")"; printf '  %-4s %s\n' "$p" "$(basename "$f")"
      case "$(basename "$f")" in
        id_*.pub|*.pub|known_hosts*|authorized_keys|config) ;;
        id_*|*.pem|*.key) case "$p" in 600|400) ;; *) sum "WARN  ~/.ssh/$(basename "$f") shaxsiy kalit ruxsati $p (600 bo'lishi kerak)";; esac ;;
      esac
    done
    [ -f "$HOME/.ssh/authorized_keys" ] && echo "  authorized_keys: $(grep -cvE '^\s*(#|$)' "$HOME/.ssh/authorized_keys") ta kalit"
  else echo "  ~/.ssh yo'q"; fi
  sub "~/.hermes ichida hamma yoza oladigan (world-writable) fayllar"
  local ww; ww="$(ff "$HERMES_HOME" 4 ! -type l -perm -o+w -print | head -n 20)"
  if [ -n "$ww" ]; then printf '%s\n' "$ww" | while IFS= read -r f; do echo "  $(hp "$f")"; done; sum "WARN  ~/.hermes ichida world-writable fayllar bor"
  else echo "  (yo'q — yaxshi)"; fi
}

sec14() {
  sub "Asosiy kataloglar (himoyalangan katalog ichiga kirilmaydi)"
  local d e
  local seen=" "
  for d in "$HOME" /home/* /opt /srv /var/www; do
    [ -d "$d" ] || continue
    case "$seen" in *" $d "*) continue;; esac; seen+="$d "
    echo "  $d/"
    for e in "$d"/*; do
      [ -e "$e" ] || continue
      if is_excl "$e"; then echo "    $(basename "$e")/  [HIMOYALANGAN — ichiga kirilmadi]"
      elif [ -d "$e" ]; then echo "    $(basename "$e")/"
      else echo "    $(basename "$e")"; fi
    done
  done
  sub "Git repozitoriylar (yo'l | branch | oxirgi commit sanasi | remote bormi | o'zgargan tracked fayllar)"
  local r g
  timeout 60 find "$HOME" /opt /srv /var/www -maxdepth 5 \( -name node_modules -o -name .cache -o -name venv -o -name .venv \
      -o -name site-packages -o -name __pycache__ -o -name "$EXCL_NAME" \) -prune -o -type d -name .git -print -prune 2>/dev/null |
    head -n 60 | while IFS= read -r g; do
      r="$(dirname "$g")"
      printf '  %s | %s | %s | remote=%s | %s\n' "$(hp "$r")" "$(gitq "$r" rev-parse --abbrev-ref HEAD)" \
        "$(gitq "$r" log -1 --format=%cs)" "$( [ -n "$(gitq "$r" remote)" ] && echo bor || echo yo\'q )" \
        "$(gitq "$r" status --porcelain --untracked-files=no | wc -l)"
    done
  sub "Docker Compose fayllari (faqat yo'l va host portlari)"
  timeout 60 find "$HOME" /opt /srv /var/www -maxdepth 5 \( -name node_modules -o -name .cache -o -name venv -o -name .venv \
      -o -name .git -o -name "$EXCL_NAME" \) -prune -o -type f \( -name 'docker-compose*.yml' -o -name 'docker-compose*.yaml' \
      -o -name 'compose.yml' -o -name 'compose.yaml' \) -print 2>/dev/null | while IFS= read -r c; do
      echo "  $(hp "$c")  host portlar: $(grep -oE '^\s*-\s*["'\'']?([0-9.]+:)?[0-9]{2,5}:[0-9]{2,5}' "$c" | grep -oE '[0-9]{2,5}:[0-9]{2,5}' | cut -d: -f1 | sort -un | tr '\n' ' ')"
    done
  sub "Veb-serverlar (faqat portlar va domenlar soni)"
  local f
  for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
    [ -r "$f" ] || continue
    echo "  nginx $(basename "$f"): listen=[$(grep -oE '^\s*listen\s+[^;]+' "$f" | grep -oE '[0-9]{2,5}' | sort -un | tr '\n' ' ')] server_name soni=$(grep -cE '^\s*server_name\b' "$f") upstream portlar=[$(grep -oE 'proxy_pass\s+https?://[^;]+' "$f" | grep -oE ':[0-9]{2,5}' | tr -d : | sort -un | tr '\n' ' ')]"
  done
  [ -r /etc/caddy/Caddyfile ] && echo "  caddy: sayt bloklari=$(grep -cE '^[^[:space:]#].*\{' /etc/caddy/Caddyfile) reverse_proxy=$(grep -c 'reverse_proxy' /etc/caddy/Caddyfile)"
  [ -d /etc/apache2/sites-enabled ] && echo "  apache saytlar: $(ls -1 /etc/apache2/sites-enabled | wc -l)"
  sub "Ishlayotgan tizim xizmatlari (nomlar)"
  if have systemctl; then timeout "$T" systemctl list-units --type=service --state=running --no-legend --no-pager --plain 2>/dev/null | awk '{print "  "$1}'
  else echo "  [systemctl mavjud emas]"; fi
  sub "Cron (faqat yozuvlar soni)"
  echo "  foydalanuvchi crontab: $(crontab -l 2>/dev/null | grep -cvE '^\s*(#|$)') ta yozuv"
  echo "  /etc/cron.d: $(ls -1 /etc/cron.d 2>/dev/null | tr '\n' ' ')"
  sub "tmux / screen / pm2"
  echo "  tmux sessiyalari: $(tmux ls 2>/dev/null | wc -l)   screen: $(screen -ls 2>/dev/null | grep -cE '^\s+[0-9]+\.')   pm2: $(pgrep -x PM2 >/dev/null 2>&1 && echo ishlayapti || echo yo\'q)"
}

sec15() {
  local found="" b
  for b in restic borg rclone duplicity rsnapshot timeshift; do have "$b" && found+="$b "; done
  echo "Backup vositalari: ${found:-topilmadi}"
  echo "Cron'da backup yozuvlari: $(crontab -l 2>/dev/null | grep -ciE 'backup|restic|borg|rclone|rsync')"
  echo "Backup timer'lari: $(systemctl list-units --type=timer --all --no-legend --plain 2>/dev/null | grep -ciE 'backup|restic|borg')"
  echo "~/.hermes ichidagi backup kataloglari: $(ff "$HERMES_HOME" 2 -iname '*backup*' -print | wc -l)"
  [ -z "$found" ] && sum "WARN  Backup vositasi topilmadi — ~/.hermes (xotira, skills, config) zaxiralanmayotgan bo'lishi mumkin"
}

# =============================================================================
#  Asosiy oqim
# =============================================================================
hdr() { printf '\n\n################################################################\n# %s\n################################################################\n' "$1"; }
SUMMARY=()
emit() {
  local red rc line
  red="$(printf '%s\n' "$1" | redact)"; rc=$?
  if [ "$rc" -ne 0 ]; then echo "  [BO'LIM CHIQARILMADI: maxfiylik filtri xatosi (rc=$rc)]"; SUMMARY+=("WARN  Bo'lim filtr xatosi sababli chiqarilmadi"); return; fi
  printf '%s\n' "$red" | leakcheck; rc=$?
  if [ "$rc" -ne 0 ]; then echo "  [BO'LIM CHIQARILMADI: filtrdan keyin ham maxfiy ko'rinishdagi qiymat aniqlandi]"; SUMMARY+=("WARN  Bo'lim maxfiylik detektori sababli chiqarilmadi"); return; fi
  while IFS= read -r line; do
    case "$line" in "@@SUM@@"*) SUMMARY+=("${line#@@SUM@@}");; *) printf '%s\n' "$line";; esac
  done < <(printf '%s\n' "$red")
}
run_section() {  # raqam, sarlavha, funksiya
  hdr "$1. $2"
  local out rc
  out="$(timeout "$SECTION_T" bash -c "$3" 2>&1)"; rc=$?
  [ "$rc" -eq 124 ] && out+=$'\n'"[bo'lim vaqt limiti (${SECTION_T}s) tugadi — natija qisman]"
  emit "$out"
}
selftest() {
  local sample out
  sample=$'api_key: sk-proj-SELFTESTabcdefghijkl1234\nTOKEN=123456789:AAselftestselftestselftestselftest12\nurl=https://u:selfpass@h.example/x\nAuthorization: Bearer SELFTESTbearerTOKEN123456'
  out="$(printf '%s\n' "$sample" | redact)" || return 1
  printf '%s' "$out" | grep -qE 'SELFTEST|AAselftest|selfpass' && return 1
  printf '%s\n' "$sample" | leakcheck && return 1     # detektor xom sirni USHLASHI shart
  printf '%s\n' "$out" | leakcheck || return 1        # filtrlangan matn toza bo'lishi shart
  return 0
}

for need in perl awk grep timeout stat find; do
  have "$need" || { echo "XATO: '$need' topilmadi — xavfsiz audit imkonsiz. To'xtatildi."; exit 1; }
done
if ! selftest; then echo "XATO: maxfiylik filtri o'z-o'zini sinovdan o'tmadi. Xavfsizlik uchun audit to'xtatildi."; exit 1; fi

# Hermes joylashuvi (CLI ishga tushirilmaydi)
HERMES_BIN="$(command -v hermes 2>/dev/null)"
if [ -z "$HERMES_BIN" ]; then
  for c in "$HOME/.local/bin/hermes" "$HERMES_HOME/hermes-agent/venv/bin/hermes" "$HERMES_HOME/venv/bin/hermes"; do [ -x "$c" ] && { HERMES_BIN="$c"; break; }; done
fi
HERMES_PY=""
if [ -n "$HERMES_BIN" ]; then
  _real="$(readlink -f "$HERMES_BIN")"
  _sb="$(head -n1 "$_real" 2>/dev/null | tr -d '\0' | cut -c1-200)"
  case "$_sb" in
    '#!'*python*) HERMES_PY="$(printf '%s' "${_sb#\#!}" | awk '{print $1}')"
                  [ "$(basename "$HERMES_PY")" = env ] && HERMES_PY="$(command -v "$(printf '%s' "${_sb#\#!}" | awk '{print $2}')")" ;;
    '#!'*) HERMES_PY="$(grep -aoE '/[^ "'\'']*/bin/python[0-9.]*' "$_real" 2>/dev/null | head -n1)" ;;
  esac
  [ -x "$HERMES_PY" ] || HERMES_PY=""
fi
for c in "$HERMES_HOME/hermes-agent/venv/bin/python" "$HERMES_HOME/venv/bin/python"; do [ -z "$HERMES_PY" ] && [ -x "$c" ] && HERMES_PY="$c"; done
ENV_FILES="$(ff "$HERMES_HOME" 3 -type f \( -name '.env' -o -name '.env.*' -o -name '*.env' \) -print)"
ENV_SET_NAMES=""
while IFS= read -r _f; do [ -n "$_f" ] && ENV_SET_NAMES+=" $(env_names "$_f" | awk '$2=="[set]"{print $1}' | tr '\n' ' ')"; done < <(printf '%s\n' "$ENV_FILES")
export HERMES_BIN HERMES_PY ENV_FILES ENV_SET_NAMES
export -f have sum sub hp hsize fhash meta is_excl is_sens ff run_ro gitq env_names cat_counts \
          sec1 sec2 sec3 sec4 sec5 sec6 sec7 sec8 sec9 sec10 sec11 sec12 sec13 sec14 sec15

hdr "0. AUDIT HAQIDA"
_h0="Sana          : $(date '+%F %T %Z')
Foydalanuvchi : $(id -un) (uid=$(id -u)), guruhlar: $(id -Gn)
HOME          : $HOME
HERMES_HOME   : $(hp "$HERMES_HOME")
Himoyalangan  : $EXCL_NAME (katalog), $EXCL_UNIT (xizmat) — tegilmaydi
Rejim         : FAQAT O'QISH. Filtr o'z-o'zini sinovi: O'TDI.
Buyruqlar     :"
for c in systemctl journalctl loginctl docker ss ip git python3 timedatectl lscpu free df ps sha256sum crontab; do
  if have "$c"; then _h0+=" $c=bor"; else _h0+=" $c=YO'Q"; fi
done
[ "$(id -u)" = 0 ] && _h0+=$'\n'"OGOHLANTIRISH: root sifatida ishga tushdi — 'ubuntu' nomidan ishga tushirish tavsiya etiladi."$'\n'"@@SUM@@WARN  Audit root sifatida bajarildi — user-service natijalari noto'g'ri bo'lishi mumkin"
emit "$_h0"

run_section 1  "OPERATSION TIZIM VA RESURSLAR"            sec1
run_section 2  "HERMES O'RNATILISHI VA VERSIYASI"         sec2
run_section 3  "KONFIGURATSIYA VA PROVAYDERLAR"           sec3
run_section 4  "SOUL.md TUZILISHI"                        sec4
run_section 5  "XOTIRA TIZIMI VA SQLITE"                  sec5
run_section 6  "SKILLS"                                   sec6
run_section 7  "GATEWAY VA SYSTEMD"                       sec7
run_section 8  "TELEGRAM VA 409 CONFLICT"                 sec8
run_section 9  "LOGLAR (FAQAT STATISTIKA)"                sec9
run_section 10 "PYTHON VA HERMES VENV"                    sec10
run_section 11 "DOCKER"                                   sec11
run_section 12 "PORTLAR VA INTERFEYSLAR"                  sec12
run_section 13 "SSH, UFW, FAIL2BAN, RUXSATLAR"            sec13
run_section 14 "BOSHQA LOYIHALAR VA XIZMATLAR"            sec14
run_section 15 "BACKUP"                                   sec15

hdr "16. YAKUNIY XULOSA (OK / WARN / INFO)"
_s=""
for _x in "${SUMMARY[@]}"; do case "$_x" in WARN*) _s+="  $_x"$'\n';; esac; done
for _x in "${SUMMARY[@]}"; do case "$_x" in OK*)   _s+="  $_x"$'\n';; esac; done
for _x in "${SUMMARY[@]}"; do case "$_x" in INFO*) _s+="  $_x"$'\n';; esac; done
emit "$_s"
echo
echo "Audit tugadi. Hech qanday fayl, sozlama yoki xizmat o'zgartirilmadi."
