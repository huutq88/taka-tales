# Motion Choreography

Use motion to clarify state and hierarchy. Prefer one signature moment plus a
small number of supporting reveals over scattered effects.

## Motion Bucket Map (FE-MOTION-BUCKET-01)

Classify the surface before choosing scroll choreography:

| Surface | Motion guidance |
| --- | --- |
| Landing, campaign, editorial, portfolio | LANDING: one signature scroll moment plus at least one supporting reveal; keep the total near 2-4 |
| Experiential microsite, award entry, interactive story | EXPERIENCE: continuous authored choreography only when every scene advances narrative or state, reduced-motion fallback exists, and core information remains reachable without precision scrolling |
| Consumer apps, education, community | Feedback and state-transition motion only |
| Dashboards, admin, ops, finance, government, B2B tools | No scroll-driven motion; preserve restrained feedback and state transitions |
| Games and interactive art | Follow the domain |

The LANDING floor applies only to the base experience. EXPERIENCE is carved out
of LANDING and has no ~4 ceiling, but every scene must advance narrative or state.
Reduced-motion users and unsupported browsers may receive a static final state.

## Implementation Rules

- Animate `transform` and `opacity` when possible.
- Enumerate transition properties; do not default to `transition-all`.
- Use one shared pointer listener per interactive cluster, scheduled through
  `requestAnimationFrame`, rather than one listener per child.
- Gate pointer-proximity effects behind `(hover: hover) and (pointer: fine)`.
- Disable non-essential movement under `prefers-reduced-motion: reduce`.
- Prefer CSS scroll-driven animations as progressive enhancement. Keep content
  visible and usable when the feature is unsupported.
- Never attach raw scroll listeners for effects when CSS timelines or a
  framework motion primitive can express the behavior.

## Motion Assets (FE-MOTION-VIDEO-01)

Use video only when motion itself communicates product meaning. Generate or
source a poster frame, reserve stable dimensions with `aspect-ratio`, and avoid
autoplay with sound. Respect reduced-motion preferences by showing the poster
or a static frame. Compress assets, lazy-load below-the-fold media, and verify
that playback does not cause layout shift.

## Verification

- Confirm the surface matches its motion bucket.
- Count distinct scroll-driven moments against FE-MOTION-BUCKET-01.
- Test keyboard use and focus while animation is active.
- Test reduced motion and the static fallback.
- Check mobile touch behavior and desktop pointer behavior separately.
- Confirm animation does not obscure text, controls, or state changes.

---

## Motion Honesty (FE-MOTION-HONESTY-01, DEFAULT)

Source: taste-skill v2 (62k stars), adapted for ima2.

The declared MOTION_INTENSITY dial must match the shipped page's actual motion.
A dial value above 4 that ships a static page is a lie — the motion was claimed
but never delivered.

| Dial | Required motion evidence |
|------|------------------------|
| 1-3 | Hover and active state transitions only. No scroll-driven motion required. |
| 4-5 | At least one entrance animation or staggered load-in visible on first scroll. |
| 6-7 | Scroll-driven reveals on multiple sections + at least one signature moment. |
| 8-10 | Choreographed scroll timeline or parallax + signature moment + supporting reveals. |

Verification: scroll the built page top-to-bottom and count distinct motion events.
If the count does not match the dial band, either lower the dial or add the motion.

Honesty has a second dimension: motion must carry a semantic verb, not merely
raise the event count. A repeated verb is communicative when the same action
explains brand or product state across loader, navigation, and content—for
example Cobloc assembling identity, SSTR translating telemetry into loader
grammar, or Interfere demonstrating issue resolution. Generic decorative
entrances remain disallowed as a governing system. This does not remove the
level 4-5 requirement for at least one entrance animation or staggered load-in;
that required entrance must be restrained, while the repeated system earns its
place by communicating state.

Any MOTION_INTENSITY > 3 MUST honor `prefers-reduced-motion`: reduce to hover/active
only. This is not optional at any dial level.
