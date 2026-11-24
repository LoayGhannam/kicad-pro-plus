# kicad_ai: محرّك رسم سكيماتك موثوق (v0.1)

الفكرة: الـ AI (أو إنت) بيوصف **شو** الدارة: القطع والتوصيلات بملف JSON.
المحرّك بيقرّر **وين** كل شي بيروح وبيرسم الأسلاك، وبعدين **فاحص مستقل** بيعيد يقرا الملف
من الصفر وبيتأكد إنو كل شي صح قبل ما يعتبر الرسمة ناجحة.

```
intent.json  ->  engine (placement + routing)  ->  .kicad_sch  ->  verify  ->  PASS / FAIL
```

## شو بيفحص الـ verifier
- كل net بالطلب طالعة بالرسمة بالزبط: ما في توصيلة ناقصة ولا shorts.
- ما في سلك مارق من فوق قطعة، أو فوق pin (غلطة C7 بالزبط).
- ما في أسلاك فوق بعض، ولا طرف سلك لامس نص سلك تاني بدون junction.
- كل pin يا موصول يا عليه no-connect flag.
- كل power input إله مصدر أو PWR_FLAG.
- رأي تاني: بيصدّر netlist بـ `kicad-cli` تبع KiCad نفسه وبيقارنها بالطلب.

## التشغيل على ويندوز (باستعمال Python تبع KiCad)
```
cd %USERPROFILE%\Documents\KiCad\10.0\kicad_ai
"D:\programs\kicad\bin\python.exe" run_examples.py
"D:\programs\kicad\bin\python.exe" -m kicad_ai build examples\ne555_blinker.json out\test.kicad_sch
"D:\programs\kicad\bin\python.exe" -m kicad_ai verify some.kicad_sch examples\ne555_blinker.json
```
إذا ما لقى مكتبات الرموز لحاله، زيد: `--symbols "D:\programs\kicad\share\kicad\symbols"`

## صيغة الـ intent
```json
{
  "title": "...",
  "flow": ["J1", "U1", "LS1"],
  "components": [{"ref": "U1", "symbol": "Timer:NE555P", "value": "NE555P", "footprint": "Package_DIP:DIP-8_W7.62mm"}],
  "nets": [{"name": "GND", "pins": ["U1.1", "C1.2"]}]
}
```
- `symbol` = `Library:Name` من مكتبات KiCad.
- `pins` = `REF.رقم_الـpin`.
- أي net اسمها موجود بمكتبة `power` (GND, +5V, +3V3, +9V...) بتنرسم بـ power symbols.
- `flow` اختياري: ترتيب الإشارة من اليسار لليمين.
- أي pin مش مذكور بأي net بياخد no-connect flag لحاله.

## حدود النسخة الحالية
- صفحة وحدة بس (ما في hierarchical sheets لسا).
- الترتيب مقبول بس مش متل مهندس خبير: أحياناً بيستعمل labels بدل أسلاك، وفي أسلاك بتتقاطع.
- قطع الـ decoupling بتنحط كمجموعة لحالها، مش لازقة بالـ IC.
- بطيء شوي بالدارات الكبيرة (التوزيع بيجرّب كل الأماكن): 15 لـ 60 ثانية للدارات بالأمثلة.
- الملف بينكتب بصيغة KiCad 7، وKiCad 10 بيفتحه وبيحوّله.

## من كلام لسكيماتك (AI)
بيشتغل مع Claude أو GPT أو DeepSeek أو Ollama أو أي API بصيغة OpenAI.
```
python -m kicad_ai ai-config --provider ollama --model qwen2.5:7b
python -m kicad_ai ai-config --provider claude --api-key sk-ant-...
python -m kicad_ai ai-config --provider deepseek --api-key sk-...
python -m kicad_ai ai-config --provider custom --base-url https://openrouter.ai/api/v1 --model ... --api-key ...
python -m kicad_ai ai-test
python -m kicad_ai ai "بدي LED بيرمش بـ NE555 على 9V" out\blinker.kicad_sch
```
الإعدادات بتنحفظ بـ `%APPDATA%\kodat-pcb\ai.json` على جهازك بس.
الـ AI ما بيرسم: بيكتب القطع والتوصيلات، والمحرّك بيرسم، والفاحص بيتأكد.
إذا غلط (قطعة مش موجودة، pin غلط، فحص فشل) الخطأ بيرجعله مع قائمة الـ pins الحقيقية، وبيعيد المحاولة لحد 4 مرات.
