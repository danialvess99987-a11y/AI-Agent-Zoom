# Hermes obuna va foydalanuvchilarni boshqarish plugini

Hermes Agent v0.23.3 (Telegram, bitta bot) uchun foydalanuvchilar, ruxsat va obunalarni boshqarish.

## Holat

**O'rnatishga tayyor (serverdagi qabul testlari hali o'tkazilmagan).** O'rnatish: `deploy/README.md`,
qabul testlari: `deploy/TEST_PLAN.md`, qaytarish: `deploy/rollback.sh`. Qo'lda `hermes plugins enable` qilmang —
`deploy/install.sh` ishlating.

## Arxitektura

```
@bot (default profil egasi) ── gateway.profile_routes
   ├─ admin user_id ─────────► default profil  (plugin role: admin)
   └─ boshqa barcha telegram ─► subscribers     (plugin role: gate)
```

| Rol | Profil | Vazifasi |
|---|---|---|
| `admin` | default | LLM tool'lari (`subs_*`), tasdiqlash tugmalari, outbox xabarlari, muddat nazorati, default profilga faqat admin kirishi |
| `gate` | subscribers | Har bir xabarda obuna va kunlik limit nazorati (`pre_gateway_dispatch`, fail-closed), pairing sinxronizatsiyasi, `pre_tool_call` qo'riqchisi |

| Modul | Vazifa |
|---|---|
| `timeutil.py`, `models.py`, `db.py`, `service.py` | Yadro: muddatlar, holatlar, SQLite (WAL), biznes mantiq, limit, outbox, audit |
| `settings.py` | `plugins.entries.subscriptions.settings` ni tekshirib o'qish |
| `hermes_compat.py` | Hermes ichki API'lariga yagona kirish nuqtasi (pairing store, adapter, session env) |
| `gate.py` | Obunachilar profili nazorati va tool qo'riqchisi |
| `admin_tools.py` | Tabiiy tilda boshqarish uchun tool'lar; o'zgartirishlar faqat tasdiq so'rovi yaratadi |
| `telegram_ui.py` | Tugmalar (`subs:` prefiks, admin ID tekshiruvi), outbox yuborish, muddat tugashini qayta ishlash |
| `texts.py` | O'zbekcha matnlar |

## Sozlamalar (har bir profil config.yaml'ida)

```yaml
plugins:
  entries:
    subscriptions:
      settings:
        role: gate                 # default profilda: admin
        admin_telegram_id: 123456789
        db_path: /home/ubuntu/.local/share/hermes-subscriptions/subs.db
        daily_limit: 50
```

## Testlar

```bash
cd hermes-subscriptions
python3 -m pytest -q tests
```

130 ta Python testi (3.12, 3.13, 3.14). Tugmalar qismi haqiqiy python-telegram-bot 22.8 bilan (tarmoqsiz) tekshirilgan.
O'rnatish/qaytarish skriptlari uchun sinov stendi (soxta hermes/systemctl bilan, 43 tekshiruv):

```bash
VPY=/path/to/python-with-ptb-pyyaml-pytest bash tests/deploy/run.sh
```

Haqiqiy Hermes gateway bilan sinov — serverda `deploy/TEST_PLAN.md` bo'yicha.
