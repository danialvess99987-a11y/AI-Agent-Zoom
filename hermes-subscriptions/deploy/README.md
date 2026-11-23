# Serverga o'rnatish (Termux / SSH orqali)

Barcha buyruqlar serverda **`ubuntu` foydalanuvchisi** bilan, `sudo`'siz bajariladi.

## 0. Oldindan bilish kerak

- Administrator Telegram ID'ingiz `~/.hermes/.env` dagi `TELEGRAM_ALLOWED_USERS` ichida bo'lishi shart (skript tekshiradi).
- O'rnatish gateway'ni **bir marta** qayta ishga tushiradi (bir necha soniya uzilish).
- `AI_SCALPING_PRO` va `ai-scalping-pro.service` ga tegilmaydi.
- Har bir o'zgartiruvchi qadamdan oldin skript `ha` deb yozishingizni so'raydi.

## 1. Kodni serverga olish

```bash
cd ~ && git clone -b claude/kind-fermat-jy8ra2 https://github.com/danialvess99987-a11y/AI-Agent-Zoom.git hermes-subs-src
```

Keyinchalik yangilash:

```bash
cd ~/hermes-subs-src && git pull
```

## 2. Faqat tekshiruv (hech narsa o'zgarmaydi)

```bash
bash ~/hermes-subs-src/hermes-subscriptions/deploy/install.sh --admin-id SIZNING_ID --step preflight
```

## 3. Nima qilinishini ko'rish (dry-run, hech narsa o'zgarmaydi)

```bash
bash ~/hermes-subs-src/hermes-subscriptions/deploy/install.sh --admin-id SIZNING_ID --dry-run
```

## 4. O'rnatish (bosqichma-bosqich, har birida tasdiq)

```bash
bash ~/hermes-subs-src/hermes-subscriptions/deploy/install.sh --admin-id SIZNING_ID
```

Bosqichlar: tekshiruv → zaxira (`~/hermes-subs-backups/<vaqt>`) → profil va plugin o'rnatish →
`hermes gateway migrate --multiplex` (gateway qayta ishga tushadi) → systemd timer'lar.

## 5. Qabul testlari

`TEST_PLAN.md` dagi 13 ta tekshiruvni bajaring.

## Qaytarish (rollback)

```bash
bash ~/hermes-subs-src/hermes-subscriptions/deploy/rollback.sh
```

config.yaml va .env birinchi o'rnatishdan oldingi holatiga qaytadi, plugin va timer'lar olib tashlanadi,
o'rnatishda yaratilgan `subscribers` profili arxivlanib o'chiriladi, gateway qayta ishga tushadi.
Obuna bazasi saqlanib qoladi.

## Foydali buyruqlar

```bash
cd ~/.hermes/plugins && ~/.hermes/hermes-agent/venv/bin/python -m subscriptions.cli --admin-id SIZNING_ID status
```

```bash
journalctl --user -u hermes-gateway -n 100 --no-pager | grep -i subscriptions
```

```bash
systemctl --user list-timers 'hermes-subscriptions-*'
```
