"""Interface strings. News text itself is translated by the model; this only covers buttons and labels.
Add a language by adding a dict below — missing keys fall back to English."""

UI: dict[str, dict[str, str]] = {
    "en": {
        "home": "Home", "hot": "Hot", "daily": "Briefings", "search": "Search", "search_ph": "Search news, companies, scholarships…",
        "top_today": "Top stories right now", "latest": "Latest picks", "all_updates": "All updates", "picks": "Picks",
        "closing_soon": "Closing soon", "deadline": "Deadline", "days_left": "days left", "today_left": "closes today",
        "sources": "sources", "source": "source", "also_covered": "Also covered by", "read_original": "Read original",
        "why": "Why it matters", "translate": "Translate", "translating": "Translating…", "show_original": "Original",
        "no_items": "Nothing here yet. The next update runs automatically.", "score": "Score", "heat": "Heat",
        "open_to_pk": "Open to Pakistan", "funding": "Funding", "location": "Location", "eligibility": "Eligibility",
        "apply": "Apply", "save": "Save", "saved": "Saved", "bookmarks": "Saved", "copy": "Copy", "copied": "Copied",
        "weekly": "Weekly", "briefs": "In brief", "more": "Load more", "about": "About", "api": "API & feeds",
        "demo": "Demo data: everything shown is fictional sample content. Run `hotpulse demo --clear` to remove it.",
        "coverage": "Coverage", "timeline": "Timeline", "all_channels": "All channels", "filter": "Filter",
        "everything": "Everything", "only_pk": "Only open to Pakistan", "ai_off": "AI translation is unavailable right now.",
    },
    "ur": {
        "home": "ہوم", "hot": "گرم خبریں", "daily": "بریفنگ", "search": "تلاش", "search_ph": "خبریں، کمپنیاں، اسکالرشپس تلاش کریں…",
        "top_today": "اس وقت کی اہم خبریں", "latest": "تازہ منتخب خبریں", "all_updates": "تمام اپ ڈیٹس", "picks": "منتخب",
        "closing_soon": "جلد ختم ہونے والے", "deadline": "آخری تاریخ", "days_left": "دن باقی", "today_left": "آج آخری دن",
        "sources": "ذرائع", "source": "ذریعہ", "also_covered": "دیگر ذرائع", "read_original": "اصل خبر پڑھیں",
        "why": "یہ کیوں اہم ہے", "translate": "ترجمہ کریں", "translating": "ترجمہ ہو رہا ہے…", "show_original": "اصل",
        "no_items": "ابھی یہاں کچھ نہیں۔ اگلی اپ ڈیٹ خود بخود آئے گی۔", "score": "اسکور", "heat": "مقبولیت",
        "open_to_pk": "پاکستانیوں کے لیے کھلا", "funding": "فنڈنگ", "location": "مقام", "eligibility": "اہلیت",
        "apply": "اپلائی کریں", "save": "محفوظ کریں", "saved": "محفوظ", "bookmarks": "محفوظ شدہ", "copy": "کاپی", "copied": "کاپی ہو گیا",
        "weekly": "ہفتہ وار", "briefs": "مختصر خبریں", "more": "مزید", "about": "تعارف", "api": "API اور فیڈز",
        "coverage": "کوریج", "timeline": "ٹائم لائن", "all_channels": "تمام چینلز", "filter": "فلٹر",
        "everything": "سب", "only_pk": "صرف پاکستان کے لیے کھلے", "ai_off": "اس وقت AI ترجمہ دستیاب نہیں۔",
    },
    "ar": {
        "home": "الرئيسية", "hot": "الأكثر تداولاً", "daily": "النشرات", "search": "بحث", "top_today": "أهم الأخبار الآن",
        "latest": "أحدث المختارات", "closing_soon": "تنتهي قريباً", "deadline": "الموعد النهائي", "sources": "مصادر",
        "read_original": "اقرأ الأصل", "why": "لماذا يهم", "translate": "ترجم", "apply": "قدّم الآن", "save": "حفظ",
        "also_covered": "تناولته أيضاً", "days_left": "أيام متبقية", "search_ph": "ابحث…", "briefs": "باختصار",
    },
    "hi": {
        "home": "होम", "hot": "ट्रेंडिंग", "daily": "ब्रीफिंग", "search": "खोज", "top_today": "अभी की बड़ी ख़बरें",
        "latest": "ताज़ा चुनिंदा", "closing_soon": "जल्द बंद", "deadline": "अंतिम तिथि", "sources": "स्रोत",
        "read_original": "मूल पढ़ें", "why": "यह क्यों मायने रखता है", "translate": "अनुवाद", "apply": "आवेदन करें",
    },
    "zh": {
        "home": "首页", "hot": "热门", "daily": "简报", "search": "搜索", "top_today": "此刻要闻", "latest": "最新精选",
        "closing_soon": "即将截止", "deadline": "截止日期", "sources": "个信源", "read_original": "阅读原文",
        "why": "为什么重要", "translate": "翻译", "apply": "申请",
    },
    "es": {
        "home": "Inicio", "hot": "Tendencias", "daily": "Resúmenes", "search": "Buscar", "top_today": "Lo más importante ahora",
        "latest": "Últimas selecciones", "closing_soon": "Cierran pronto", "deadline": "Fecha límite", "sources": "fuentes",
        "read_original": "Leer original", "why": "Por qué importa", "translate": "Traducir", "apply": "Postular",
    },
}


def t(lang: str, key: str) -> str:
    return UI.get(lang, {}).get(key) or UI["en"].get(key, key)
