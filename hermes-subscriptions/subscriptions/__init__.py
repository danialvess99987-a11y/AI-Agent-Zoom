"""Hermes Agent uchun foydalanuvchilar, ruxsat va obuna boshqaruv plugini.

1-bosqich: faqat Hermes'ga bog'liq bo'lmagan yadro (models, timeutil, db, service).
Hermes bilan integratsiya (`register(ctx)`) 2-bosqichda qo'shiladi; shu sababli bu paket
hozircha Hermes plugin sifatida yoqilmasligi kerak.
"""

__all__ = ["models", "timeutil", "db", "service"]
