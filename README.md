# KODAT PCB

نسخة معدّلة من KiCad 10 من KODAT فيها:
- **محرّك رسم سكيماتك موثوق** (`engine/`): بتوصفله الدارة (قطع + توصيلات) وهو بيرسمها، وفاحص مستقل ما بيقبل الرسمة إلا إذا كانت صح.
- **مساعد AI** (Claude API أو Ollama) بيحوّل كلامك لوصف دارة. *(قيد البناء)*
- **واجهة مرتّبة ووضع Eagle**: أدوات بترتيب Eagle، سطر أوامر، Ctrl+K، لوحة AI مزروعة. *(قيد البناء)*

KODAT PCB: a modified KiCad 10 with an AI schematic assistant, a verified auto-drawing
engine and an Eagle-style UI.

## البنية
| الفولدر | شو فيه |
|---|---|
| `engine/` | محرّك الرسم والفاحص (Python). بيشتغل لحاله أو من جوّا KiCad Pro+ |
| فرع `kodat` | كود KiCad 10.0.5 الرسمي + تعديلات KODAT PCB كـ commits |
| `ci/` | رقم نسخة KiCad الأساس والـ launcher |
| `.github/workflows/` | `engine tests` (Linux) و`windows build` (بيطلّع نسخة portable) |

## التنزيل
كل build ناجح بينزل بـ **Releases** كملف `KODAT-PCB-win64.zip`.
فكّه بأي مكان وشغّل `KODAT-PCB.cmd`. إعداداته منفصلة عن KiCad العادي،
وبيستعمل مكتبات KiCad 10 المركّبة عندك.

## الترخيص
KiCad مرخّص GPL-3.0، وهالمشروع كمان. التفاصيل بمستودع KiCad الأصلي.
