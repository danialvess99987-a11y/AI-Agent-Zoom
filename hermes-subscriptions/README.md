# Hermes obuna va foydalanuvchilarni boshqarish tizimi

Hermes Agent v0.23.3 (Telegram) uchun foydalanuvchilar, ruxsat va obunalarni boshqarish plugini.

## Holat

**1-bosqich: yadro tayyor, Hermes integratsiyasi hali yo'q.** Bu paketda `register(ctx)` yo'q, shuning uchun
uni hozircha `~/.hermes/plugins/` ga o'rnatmang va `hermes plugins enable` qilmang.

| Modul | Vazifa |
|---|---|
| `subscriptions/timeutil.py` | Muddatlar (daqiqa/soat/kun/hafta/oy), UTC saqlash, Asia/Tashkent ko'rsatish |
| `subscriptions/models.py` | Holatlar, o'tish jadvali, xatolar, ism normallashtirish |
| `subscriptions/db.py` | SQLite (WAL, BEGIN IMMEDIATE), migratsiyalar |
| `subscriptions/service.py` | Biznes mantiq: obuna berish/uzaytirish/to'xtatish/bekor qilish, qidiruv, statistika, tasdiqlash, audit |

Faqat Python standart kutubxonasi ishlatiladi. Python 3.11–3.14 bilan mos.

## Testlar

```bash
cd hermes-subscriptions
python3 -m pytest -q tests
```

Tekshirilgan: Python 3.12.3, 3.13.18 va 3.14.6 — 70/70 test.

## Keyingi bosqichlar

2. Hermes integratsiyasi: `pre_gateway_dispatch` nazorati, pairing store sinxronizatsiyasi, admin tool'lari, tasdiqlash tugmalari.
3. Bildirishnomalar, muddat nazorati, backup, CLI.
4. Serverdagi test muhiti.
5. O'rnatish (zaxira va rollback bilan).
