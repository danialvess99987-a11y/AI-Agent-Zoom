import json
import os
import stat
import subprocess

import pytest

from conftest import ADMIN, make_user
from subscriptions import cli
from subscriptions.models import Status
from subscriptions.timeutil import HOUR, Duration


def write_pairing(home, profile, ids):
    path = home / "profiles" / profile / "platforms" / "pairing"
    path.mkdir(parents=True)
    (path / "telegram-approved.json").write_text(json.dumps({str(i): {"user_name": "x"} for i in ids}))


class Runner:
    def __init__(self, fail_ids=()):
        self.calls = []
        self.fail_ids = {str(i) for i in fail_ids}

    def __call__(self, cmd, capture_output, text, timeout):
        self.calls.append(cmd)
        code = 1 if cmd[-1] in self.fail_ids else 0
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="err" if code else "")


def test_reconcile_dry_run_changes_nothing(svc, clock, tmp_path):
    for tid in (1, 2, 3):
        make_user(svc, tid)
    svc.add_time(1, Duration(1, "day"), ADMIN)
    svc.add_time(2, Duration(1, "hour"), ADMIN)
    write_pairing(tmp_path, "subscribers", [1, 2, 3, ADMIN, 999])
    clock.advance(2 * HOUR)
    runner = Runner()
    summary = cli.cmd_reconcile(svc, ADMIN, "subscribers", "hermes", apply=False, home=tmp_path, runner=runner)
    assert summary["stale_pairings"] == [2, 3, 999] and summary["revoked"] == [] and runner.calls == []
    assert svc.get_user(2).status is Status.ACTIVE  # dry-run bazaga tegmaydi
    assert svc.fetch_outbox() == []


def test_reconcile_apply_revokes_and_notifies_once(svc, clock, tmp_path):
    for tid in (1, 2):
        make_user(svc, tid)
    svc.add_time(1, Duration(1, "day"), ADMIN)
    svc.add_time(2, Duration(1, "hour"), ADMIN)
    write_pairing(tmp_path, "subscribers", [1, 2, 777])
    clock.advance(2 * HOUR)
    runner = Runner(fail_ids=[777])
    summary = cli.cmd_reconcile(svc, ADMIN, "subscribers", "/x/hermes", apply=True, home=tmp_path, runner=runner)
    assert summary["expired_now"] == [2] and summary["revoked"] == [2]
    assert runner.calls == [["/x/hermes", "-p", "subscribers", "pairing", "revoke", "telegram", "2"],
                            ["/x/hermes", "-p", "subscribers", "pairing", "revoke", "telegram", "777"]]
    assert svc.get_user(2).status is Status.EXPIRED
    kinds = sorted(m.kind for m in svc.fetch_outbox())
    assert kinds == ["expired", "expired_admin"]
    # Fon tsikli keyin ishlasa ham takroriy xabar yo'q (bir xil dedupe kalitlari)
    from subscriptions.jobs import expire_and_notify
    assert expire_and_notify(svc, ADMIN, lambda t: None) == []
    assert len(svc.fetch_outbox()) == 2


def test_approved_ids_reads_both_layouts_and_rejects_corrupt(tmp_path):
    write_pairing(tmp_path, "subscribers", [5, 6])
    legacy = tmp_path / "profiles" / "subscribers" / "pairing"
    legacy.mkdir()
    (legacy / "telegram-approved.json").write_text(json.dumps({"7": {}}))
    assert cli.approved_ids("subscribers", tmp_path) == {5, 6, 7}
    assert cli.approved_ids("nobody", tmp_path) == set()
    (legacy / "telegram-approved.json").write_text("{bad json")
    with pytest.raises(RuntimeError):
        cli.approved_ids("subscribers", tmp_path)


def test_backup_restore_roundtrip(svc, tmp_path):
    make_user(svc, 42, "Dilshod")
    svc.add_time(42, Duration(1, "day"), ADMIN)
    dest = tmp_path / "bk"
    first = cli.cmd_backup(svc, dest, keep=14)
    assert stat.S_IMODE(os.stat(first).st_mode) == 0o600
    svc.ban(42, ADMIN)
    cli.cmd_restore(svc, first, dest)
    assert svc.get_user(42).status is Status.ACTIVE  # zaxiradagi holat qaytdi
    assert len(list(dest.glob("subs-*.db"))) == 2  # tiklashdan oldingi xavfsizlik zaxirasi ham bor


def test_backup_retention(svc, tmp_path):
    dest = tmp_path / "bk"
    for _ in range(5):
        cli.cmd_backup(svc, dest, keep=3)
    assert len(list(dest.glob("subs-*.db"))) == 3


def test_restore_rejects_corrupt_file(svc, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(Exception):
        cli.cmd_restore(svc, bad, tmp_path / "bk")
    with pytest.raises(RuntimeError):
        cli.cmd_restore(svc, tmp_path / "missing.db", tmp_path / "bk")


def test_main_status_and_guards(db_path, svc, capsys):
    make_user(svc, 42)
    assert cli.main(["--db", str(db_path), "--admin-id", str(ADMIN), "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["integrity_ok"] is True and out["total_users"] == 1
    assert cli.main(["--db", str(db_path), "--admin-id", str(ADMIN), "restore", "x.db"]) == 2  # --yes yo'q
    assert cli.main(["--db", str(db_path.parent / "none.db"), "--admin-id", str(ADMIN), "status"]) == 2
    with pytest.raises(SystemExit):
        cli.main(["status"])  # --admin-id majburiy
