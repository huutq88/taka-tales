# Anti-Slop Design Judgment

Design-level anti-slop rules. These are JUDGMENT calls made before code —
implementation enforcement lives in `ima2-front/references/anti-slop-implementation.md`.

## LLM Default Patterns to Reject

These are the patterns AI agents produce when they lack specific direction.
Reject them on sight:

- **Warm beige/cream backgrounds** with brass/clay accents as "premium" default
- **Centered hero + three equal feature cards** below
- **Generic glassmorphism** without functional justification
- **Inter + slate-900** as the go-to type/color combo
- **Card-based everything** without layout variety
- **Purple gradients on white** (old tell, still common)
- **Gradient mesh backgrounds** as a substitute for real visual content

## 2026 Active Tells

- **Gradient soup** — multiple gradients per viewport, gradients on card grids
- **One-note dark themes** — terminal green, cyber cyan, CRT amber
- **Emoji as UI icons** — the #1 signal that AI wrote this
- **"Elevate your X"** — cliche copy that AI defaults to
- **Version labels in heroes** ("v3.0", "2.0 Release")
- **Numbered eyebrows** ("01 — Features", "02 — Pricing")
- **Fake social proof** ("trusted by teams worldwide", "10,000+ users")
- **Middle-dot overuse** as a section separator
- **Duplicate image reuse** across sections
- **Decorative scroll cues** with no functional purpose

## What to Do Instead

- Use `ima2 gen` to create real, domain-specific visual assets
- Choose asymmetric or content-weighted layouts
- Vary section families (at least 4 different per 8 sections)
- Use domain-appropriate fonts (not Inter unless the project already uses it)
- Write original copy that names the product's actual value
- Generate real product photos/illustrations instead of gradient washes
