"""Default patient-facing texts (RU / KZ / UZ-latin).

Keep them short, warm, one question per message — the agent prompt asks for the
same style, and these are what patients see outside a dialogue (reminders,
follow-ups, recalls), where no model is involved at all.
"""
from __future__ import annotations

DEFAULTS: dict[str, dict[str, str]] = {
    # ---- consent ---------------------------------------------------------
    "consent_request": {
        "ru": "Чтобы записать вас, нужно согласие на обработку персональных данных "
        "(имя и телефон — только для записи). Подтверждаете?",
        "kz": "Сізді жазу үшін дербес деректерді өңдеуге келісім қажет "
        "(аты және телефон — тек жазылу үшін). Растайсыз ба?",
        "uz": "Sizni yozib qo'yish uchun shaxsiy ma'lumotlarni qayta ishlashga rozilik kerak "
        "(ism va telefon — faqat navbat uchun). Tasdiqlaysizmi?",
    },
    "consent_thanks": {
        "ru": "Спасибо! Записываю.",
        "kz": "Рақмет! Жазып жатырмын.",
        "uz": "Rahmat! Yozib qo'yaman.",
    },
    # ---- booking ---------------------------------------------------------
    "booking_confirmed": {
        "ru": "Записала вас: {service}, {when}, врач {doctor}.\n"
        "Адрес: {address}\n{map_line}{prep_line}До встречи!",
        "kz": "Сізді жазып қойдым: {service}, {when}, дәрігер {doctor}.\n"
        "Мекенжай: {address}\n{map_line}{prep_line}Кездескенше!",
        "uz": "Navbat olindi: {service}, {when}, shifokor {doctor}.\n"
        "Manzil: {address}\n{map_line}{prep_line}Ko'rishguncha!",
    },
    "booking_cancelled": {
        "ru": "Отменила запись на {when}. Если захотите вернуться — подберу новое время.",
        "kz": "{when} жазылуыңыз тоқтатылды. Қаласаңыз — жаңа уақыт таңдаймын.",
        "uz": "{when} uchun navbat bekor qilindi. Xohlasangiz — yangi vaqt tanlayman.",
    },
    "booking_rescheduled": {
        "ru": "Перенесла: теперь {when}, врач {doctor}. Адрес тот же: {address}",
        "kz": "Ауыстырдым: енді {when}, дәрігер {doctor}. Мекенжай сол: {address}",
        "uz": "Ko'chirdim: endi {when}, shifokor {doctor}. Manzil o'sha: {address}",
    },
    "hold_expired": {
        "ru": "Время {when} я не смогла удержать дольше 10 минут — его могли занять. "
        "Подобрать другое?",
        "kz": "{when} уақытын 10 минуттан артық ұстай алмадым. Басқасын таңдайын ба?",
        "uz": "{when} vaqtini 10 daqiqadan ko'p ushlab turolmadim. Boshqasini tanlaymi?",
    },
    # ---- reminders -------------------------------------------------------
    "reminder_24h": {
        "ru": "Напоминаю: {when} ждём вас на {service}, врач {doctor}.\nПридёте?",
        "kz": "Еске саламын: {when} сізді {service} қабылдауына күтеміз, дәрігер {doctor}.\nКелесіз бе?",
        "uz": "Eslatma: {when} sizni {service} uchun kutamiz, shifokor {doctor}.\nKelasizmi?",
    },
    "reminder_2h": {
        "ru": "Через пару часов ждём вас — {when}. Адрес: {address}\n{map_line}",
        "kz": "Екі сағаттан кейін күтеміз — {when}. Мекенжай: {address}\n{map_line}",
        "uz": "Ikki soatdan keyin kutamiz — {when}. Manzil: {address}\n{map_line}",
    },
    # ---- follow-up -------------------------------------------------------
    "followup_1": {
        "ru": "Оставила за вами интерес к «{service}». Подобрать удобное время? "
        "Есть {slots}.",
        "kz": "«{service}» бойынша сұрауыңыз сақталды. Уақыт таңдайын ба? {slots} бар.",
        "uz": "«{service}» bo'yicha so'rovingiz saqlanib qoldi. Vaqt tanlaymi? {slots} bor.",
    },
    "followup_2": {
        "ru": "Если ещё актуально — на этой неделе есть окна: {slots}. Записать?",
        "kz": "Әлі де қажет болса — осы аптада орын бар: {slots}. Жазайын ба?",
        "uz": "Hali ham kerak bo'lsa — bu hafta joy bor: {slots}. Yozamizmi?",
    },
    "followup_3": {
        "ru": "Последнее напоминание по «{service}»: {reason} "
        "Если неактуально — просто напишите «не надо», больше не потревожу.",
        "kz": "«{service}» бойынша соңғы еске салу: {reason} "
        "Қажет болмаса — «керек емес» деп жазыңыз.",
        "uz": "«{service}» bo'yicha oxirgi eslatma: {reason} "
        "Kerak bo'lmasa — «kerak emas» deb yozing.",
    },
    # ---- recall ----------------------------------------------------------
    "recall_invite": {
        "ru": "{name}, прошло {months} мес. после «{service}» — пора на контроль. "
        "Удобно {slots}?",
        "kz": "{name}, «{service}» кейін {months} ай өтті — тексеру уақыты. {slots} қолайлы ма?",
        "uz": "{name}, «{service}» dan keyin {months} oy o'tdi — ko'rik vaqti. {slots} qulaymi?",
    },
    "dormant_invite": {
        "ru": "{name}, давно вас не видели. Если что-то беспокоит — подберу время у врача. "
        "Написать «стоп», чтобы не получать такие сообщения.",
        "kz": "{name}, көрмегелі көп болды. Қажет болса — дәрігерге уақыт таңдаймын. "
        "Хабарламаларды тоқтату үшін «стоп» деп жазыңыз.",
        "uz": "{name}, ko'rmaganimizga ko'p bo'ldi. Kerak bo'lsa — shifokorga vaqt tanlayman. "
        "To'xtatish uchun «stop» deb yozing.",
    },
    # ---- reviews ---------------------------------------------------------
    "review_request": {
        "ru": "Как всё прошло? Оцените от 1 до 5 — это помогает нам стать лучше.",
        "kz": "Қалай өтті? 1-ден 5-ке дейін бағалаңыз — бізге жақсаруға көмектеседі.",
        "uz": "Qanday o'tdi? 1 dan 5 gacha baho bering — bizga yaxshilanishga yordam beradi.",
    },
    "review_promote": {
        "ru": "Спасибо! Если не сложно, оставьте пару слов здесь — это очень помогает: {link}",
        "kz": "Рақмет! Мүмкін болса, осында пікір жазыңыз: {link}",
        "uz": "Rahmat! Imkon bo'lsa, shu yerda fikr qoldiring: {link}",
    },
    "review_apology": {
        "ru": "Спасибо за честность. Передам руководителю — с вами свяжутся.",
        "kz": "Шынайылығыңыз үшін рақмет. Жетекшіге жеткіземін — сізбен байланысады.",
        "uz": "Rostgo'yligingiz uchun rahmat. Rahbarga yetkazaman — siz bilan bog'lanadilar.",
    },
    # ---- no-show ---------------------------------------------------------
    "noshow_rebook": {
        "ru": "Не получилось прийти? Бывает. Подобрать новое время? Есть {slots}.",
        "kz": "Келе алмадыңыз ба? Болады. Жаңа уақыт таңдайын ба? {slots} бар.",
        "uz": "Kelib ulgurmadingizmi? Bo'ladi. Yangi vaqt tanlaymi? {slots} bor.",
    },
    # ---- handoff ---------------------------------------------------------
    "handoff_notice": {
        "ru": "Подключаю администратора — он ответит здесь же.",
        "kz": "Әкімшіні қосамын — осы жерде жауап береді.",
        "uz": "Administratorni qo'shaman — shu yerda javob beradi.",
    },
    "handoff_night": {
        "ru": "Сейчас нерабочее время. Администратор ответит утром, после 09:00. "
        "Если вопрос срочный и это боль или травма — обратитесь в скорую.",
        "kz": "Қазір жұмыс уақыты емес. Әкімші таңертең 09:00-ден кейін жауап береді. "
        "Жедел жағдайда — жедел жәрдемге хабарласыңыз.",
        "uz": "Hozir ish vaqti emas. Administrator ertalab 09:00 dan keyin javob beradi. "
        "Shoshilinch holatda — tez yordamga murojaat qiling.",
    },
    "red_flag": {
        "ru": "Это может быть срочно. Если есть сильная боль, отёк, температура, кровотечение "
        "или трудно дышать — не ждите. {call_line}\n"
        "Я уже передала администратору, он свяжется с вами.",
        "kz": "Бұл шұғыл болуы мүмкін. Қатты ауру, ісіну, қызба, қан кету немесе тыныс алу қиын болса "
        "— күтпеңіз. {call_line}\nӘкімшіге жеткіздім.",
        "uz": "Bu shoshilinch bo'lishi mumkin. Qattiq og'riq, shish, isitma, qon ketish yoki nafas olish "
        "qiyin bo'lsa — kutmang. {call_line}\nAdministratorga yetkazdim.",
    },
    "photo_received": {
        "ru": "Фото передам врачу — по снимкам я ничего не оцениваю. "
        "Администратор ответит здесь.",
        "kz": "Фотоны дәрігерге жеткіземін — суреттер бойынша баға бермеймін. "
        "Әкімші осы жерде жауап береді.",
        "uz": "Rasmni shifokorga yetkazaman — rasm bo'yicha baho bermayman. "
        "Administrator shu yerda javob beradi.",
    },
    # ---- system ----------------------------------------------------------
    "opt_out_done": {
        "ru": "Больше не буду писать по рассылкам. Если понадобимся — просто напишите.",
        "kz": "Жарнамалық хабарлама жібермеймін. Қажет болсақ — жазыңыз.",
        "uz": "Boshqa reklama xabar yubormayman. Kerak bo'lsak — yozing.",
    },
    "fallback": {
        "ru": "Не до конца поняла. Передаю администратору — он ответит здесь.",
        "kz": "Толық түсінбедім. Әкімшіге жеткіземін — осы жерде жауап береді.",
        "uz": "To'liq tushunmadim. Administratorga yetkazaman — shu yerda javob beradi.",
    },
    "bot_paused": {
        "ru": "Администратор уже в диалоге — он ответит здесь.",
        "kz": "Әкімші диалогта — осы жерде жауап береді.",
        "uz": "Administrator suhbatda — shu yerda javob beradi.",
    },
    "data_deleted": {
        "ru": "Запрос на удаление данных принят. Обработаем в течение 15 рабочих дней "
        "и напишем вам.",
        "kz": "Деректерді жою сұрауы қабылданды. 15 жұмыс күні ішінде өңдеп, жазамыз.",
        "uz": "Ma'lumotlarni o'chirish so'rovi qabul qilindi. 15 ish kuni ichida bajaramiz.",
    },
}

__all__ = ["DEFAULTS"]
