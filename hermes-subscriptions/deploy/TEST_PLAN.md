# Qabul testlari (serverda, o'rnatishdan keyin)

Kerak: sizning Telegram akkauntingiz (admin) va kamida bitta boshqa Telegram akkaunt (test foydalanuvchi).
Ikkita bir xil ismli foydalanuvchi testi uchun ikkinchi test akkaunt yoki do'stingiz yordami kerak.

| # | Tekshiruv | Qanday | Kutilgan natija |
|---|---|---|---|
| 1 | Yangi foydalanuvchi — adminga xabar | Test akkauntdan botga `/start` | Sizga "🆕 Yangi foydalanuvchi" + 4 ta tugma |
| 2 | Yangi foydalanuvchiga xabar | Shu `/start` | Test akkauntga imkoniyatlar va obuna tartibi haqida matn |
| 3 | Tasdiqlanmagan foydalanuvchi | Test akkauntdan "salom" | Agent javob bermaydi; "tasdiq kutilmoqda" xabari (10 daqiqada ko'pi bilan 1 marta) |
| 4 | Tabiiy tilda obuna | Siz: "<ism>ga 2 soatlik obuna ber" | Agent foydalanuvchini topadi, sizga tasdiq tugmalari keladi; "✅ Tasdiqlash" dan keyin obuna faol |
| 5 | 2 soatlik hisob | Siz: "<ism> haqida ma'lumot ber" | Tugash vaqti = tasdiqlangan vaqt + 2 soat (Toshkent vaqti) |
| 6 | Muddat tugashi | Siz: "<ism>ga 10 daqiqalik obuna ber", 11–12 daqiqa kuting | Test akkaunt va sizga "obuna tugadi" xabari; test akkaunt yozsa agent javob bermaydi |
| 7 | Uzaytirish | Faol obunaga: "yana 1 kun qo'sh" | Yangi tugash = eski tugash + 1 kun (hozirgi vaqt emas) |
| 8 | Bir xil ismlar | Ikki "Ali" bo'lsa: "Aliga 1 kun ber" | Agent ikkalasini ko'rsatib, tanlashni so'raydi |
| 9 | Oddiy foydalanuvchi admin emas | Test akkaunt: "Menga 30 kun obuna ber" / tugmani forward qilib bosish | Hech narsa o'zgarmaydi; tugma "faqat administrator uchun" |
| 10 | Restart'dan keyin saqlanish | `hermes gateway restart`, so'ng "<ism> haqida ma'lumot" | Obuna ma'lumotlari o'zgarmagan |
| 11 | Bir vaqtda murojaat | 2–3 akkaunt bir vaqtda yozadi | Hammasiga to'g'ri javob/cheklov; `cli status` da `integrity_ok: true` |
| 12 | Chetlab o'tish | Obunasi tugagan akkauntdan rasm, fayl, ovozli xabar, `/model`, `/new` | Hech biri agentga yetmaydi |
| 13 | Mavjud tizim | `systemctl --user status hermes-gateway`, `systemctl status ai-scalping-pro` (yoki `--user`), siz botdan odatdagidek foydalanasiz | Gateway faol, AI_SCALPING_PRO avvalgidek, sizning agentingiz to'liq imkoniyatda |

Qo'shimcha: test akkaunt 51-xabarda "bugungi limitingiz tugadi" olishi (kunlik limit 50).

Muammo bo'lsa:

```bash
journalctl --user -u hermes-gateway -n 200 --no-pager | grep -iE 'subscriptions|plugin|route'
```

va natijani yuboring (token/kalitlarni olib tashlab). Jiddiy muammoda: `bash rollback.sh`.
