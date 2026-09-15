## results

hard-1:
google/gemma-4-31b-it 6/8 (75%)
qwen/qwen3.7-flash 4/8 (50%)
nex-agi/nex-n2-mini 8/8 (100%) !!!
stepfun/step-3.7-flash 5/8 (62.5%)

hard-2:
google/gemma-4-31b-it 2/16 (12.5%)
qwen/qwen3.7-flash 8/8 (100%) !!!
nex-agi/nex-n2-mini 5/16 (31.25%)
stepfun/step-3.7-flash 5/8 (62.5%)

live-1 (captcha-live-1.png): google/gemma-4-31b-it mis, qwen/qwen3.7-flash ok
live-2 (captcha-live-2.png): google/gemma-4-31b-it mis, qwen/qwen3.7-flash ok
live-3 (captcha-live-3.png): untested

## live samples (2026-09-10)

Real HH standalone-challenge images, current noisy 250×90 style (~15k colors vs ~300
for the old clean fixture). Collected from a live account challenge during the
2026-09-10 incident; overlap visually with the `captcha-hard-*` set.

| file | expected text | verification |
| --- | --- | --- |
| captcha-live-1.png | `разудалого габы` | VERIFIED server-side (correct submit → 302) |
| captcha-live-2.png | `баска непонятое` | vision ×3, not verified server-side |
| captcha-live-3.png | `усилено декабрь` | vision ×3, not verified server-side |

Baseline live-1/live-2 (K=4, consensus): `google/gemma-4-31b-it` 0/2 (mangled
tails: `разудалого габл`, `баска непонятов`, +2/4 timeouts on live-1);
`qwen/qwen3.7-flash` 2/2. See `## results`.

## todo

- batching script
- research best practices
- research recommended models
- models to test:
  - qwen/qwen3.8-27b
- noise preprocessing experiments (2026-09-10): bench denoising / upscaling
  (e.g. contrast stretch, median blur, 2-4× lanczos) before the vision call and
  measure win/loss against raw PNG on `captcha-live-*`. Decision only after
  measurements; nothing to change in `src/` yet.

## prompt

```
what is the text on the image? note: it might be not real text but just  sequence of chars or smth word-like. output `{ "image_text": "str" }`
```
