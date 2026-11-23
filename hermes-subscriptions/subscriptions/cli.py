"""Mustaqil CLI (Hermes jarayonidan tashqarida): holat, zaxira, tiklash, pairing'ni tozalash.

Ishga tushirish (plugin katalogining ota katalogidan):
    cd ~/.hermes/plugins && python3 -m subscriptions.cli --admin-id 123 status
Buyruqlar:
    status                       — statistika va baza butunligi
    backup [--dest D] [--keep N] — SQLite backup API orqali izchil zaxira (0600)
    restore FILE --yes           — zaxiradan tiklash (avval joriy baza ham zaxiralanadi)
    reconcile [--apply]          — muddati o'tganlarni EXPIRED qilish va obunasi faol bo'lmaganlarni
                                   Hermes pairing'dan chiqarish (`hermes -p <profil> pairing revoke`). Standart: dry-run.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import db
from .jobs import expire_and_notify
from .models import Status
from .service import SubscriptionService
from .settings import DEFAULT_DB_PATH, DEFAULT_SUBSCRIBER_PROFILE

BACKUP_PREFIX = "subs-"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_DEFAULT_ROOT") or os.path.expanduser("~/.hermes"))


def approved_ids(profile: str, home: Optional[Path] = None) -> set[int]:
    """Profilning Telegram pairing ro'yxati (faqat o'qiladi). gateway/pairing.py:58,366 joylashuvlari."""
    root = home or _hermes_home()
    base = root / "profiles" / profile
    ids: set[int] = set()
    for path in (base / "platforms" / "pairing" / "telegram-approved.json", base / "pairing" / "telegram-approved.json"):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Pairing faylini o'qib bo'lmadi: {path}: {exc}") from exc
        if isinstance(data, dict):
            ids.update(int(k) for k in data if str(k).isdigit())
    return ids


def hermes_revoker(hermes_bin: str, profile: str, dry_run: bool,
                   runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Callable[[int], bool]:
    def revoke(telegram_id: int) -> bool:
        cmd = [hermes_bin, "-p", profile, "pairing", "revoke", "telegram", str(int(telegram_id))]
        if dry_run:
            print("  [dry-run] " + " ".join(cmd))
            return True
        result = runner(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"  ! revoke xato ({telegram_id}): {(result.stderr or result.stdout).strip()[:200]}")
            return False
        print(f"  pairing'dan chiqarildi: {telegram_id}")
        return True

    return revoke


def cmd_status(service: SubscriptionService) -> int:
    stats = service.stats()
    with closing(db.connect(service.db_path)) as con:
        ok = db.integrity_ok(con)
        version = db.schema_version(con)
    print(json.dumps({"db": str(service.db_path), "schema_version": version, "integrity_ok": ok, **stats},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 2


def cmd_backup(service: SubscriptionService, dest: Path, keep: int) -> Path:
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = dest / f"{BACKUP_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}.db"
    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(db.connect(service.db_path)) as src, closing(sqlite3.connect(str(target))) as out:
        src.backup(out)
    with closing(sqlite3.connect(str(target))) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"Zaxira butunlik tekshiruvidan o'tmadi: {target}")
    backups = sorted(p for p in dest.glob(f"{BACKUP_PREFIX}*.db") if p.is_file())
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
    print(f"Zaxira: {target}")
    return target


def cmd_restore(service: SubscriptionService, source: Path, dest: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"Zaxira fayli topilmadi: {source}")
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Zaxira fayli buzilgan — tiklanmadi")
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if version > db.SCHEMA_VERSION:
        raise RuntimeError("Zaxira sxemasi koddan yangiroq — tiklanmadi")
    safety = cmd_backup(service, dest, keep=0)
    print(f"Joriy baza tiklashdan oldin saqlandi: {safety}")
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src, \
            closing(db.connect(service.db_path)) as target:
        src.backup(target)
    with closing(db.connect(service.db_path)) as con:
        db.migrate(con)
        if not db.integrity_ok(con):
            raise RuntimeError("Tiklangan baza butunlik tekshiruvidan o'tmadi")
    print(f"Tiklandi: {source} -> {service.db_path}")


def cmd_reconcile(service: SubscriptionService, admin_id: int, profile: str, hermes_bin: str, apply: bool,
                  home: Optional[Path] = None,
                  runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    revoke = hermes_revoker(hermes_bin, profile, dry_run=not apply, runner=runner)
    expired: list[int] = expire_and_notify(service, admin_id, lambda tid: None) if apply else []
    now = service.now()
    stale = sorted(tid for tid in approved_ids(profile, home)
                   if tid != admin_id and not ((u := service.get_user(tid)) and u.effective_status(now) is Status.ACTIVE))
    revoked = [tid for tid in stale if revoke(tid)]
    summary = {"apply": apply, "expired_now": expired, "stale_pairings": stale, "revoked": revoked if apply else []}
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m subscriptions.cli", description="Obuna tizimi CLI")
    parser.add_argument("--db", default=os.path.expanduser(DEFAULT_DB_PATH), help="Baza yo'li")
    parser.add_argument("--admin-id", type=int, required=True, help="Administrator Telegram ID")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p_backup = sub.add_parser("backup")
    p_backup.add_argument("--dest", default=None, help="Zaxira katalogi (standart: <db katalogi>/backups)")
    p_backup.add_argument("--keep", type=int, default=14, help="Saqlanadigan zaxiralar soni")
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("file")
    p_restore.add_argument("--yes", action="store_true", help="Tasdiqlash (gateway to'xtatilgan bo'lishi tavsiya etiladi)")
    p_rec = sub.add_parser("reconcile")
    p_rec.add_argument("--profile", default=DEFAULT_SUBSCRIBER_PROFILE)
    p_rec.add_argument("--hermes-bin", default=os.path.expanduser("~/.local/bin/hermes"))
    p_rec.add_argument("--apply", action="store_true", help="O'zgarishlarni bajarish (aks holda faqat ko'rsatadi)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db).expanduser()
    if args.command != "restore" and not db_path.exists():
        print(f"Baza topilmadi: {db_path}", file=sys.stderr)
        return 2
    service = SubscriptionService(db_path, [args.admin_id])
    backups = Path(getattr(args, "dest", None) or db_path.parent / "backups")
    try:
        if args.command == "status":
            return cmd_status(service)
        if args.command == "backup":
            cmd_backup(service, backups, max(0, args.keep))
            return 0
        if args.command == "restore":
            if not args.yes:
                print("Tiklash uchun --yes qo'shing (avval gateway'ni to'xtating).", file=sys.stderr)
                return 2
            cmd_restore(service, Path(args.file).expanduser(), db_path.parent / "backups")
            return 0
        if args.command == "reconcile":
            cmd_reconcile(service, args.admin_id, args.profile, args.hermes_bin, args.apply)
            return 0
    except Exception as exc:
        print(f"XATO: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
