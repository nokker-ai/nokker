<!-- From Impeccable (Apache-2.0), commit 687bc381, changed for Nokker: reading key on line 2, launcher commands and tool names, render-dependent scores. -->
> **Nokker reading key.** In Nokker, these do not exist: sloplint, the scaffold and base.css, the theme catalog and the photo library (theme.mjs), capture scripts, the stamp, the build log, the render tier, the design hook, the launcher, and the files anatomy.md, imagery.md, macrostructures.md, direction.md, redesign.md, patterns.md, scroll-choreography.md and audit.native.md. design_check replaces the static tier (M); you declare every token yourself at :root. Render (R) checks are asked of the user, never claimed. A waived reflex is stated in one line in your answer, not in a stamp; no numeric self-score. Skip gate 20 and the ledger gates (8, 32). Images and fonts are only what the user supplied, inline as data:, or a system font stack: never a remote image, font or icon library (a remote asset fails the floor; it is never a WARN). A visual the user did not supply is a statement, a table or a diagram of given facts, never drawn or invented; sample data on a page is visibly marked as an example. Nothing here overrides the design-guard floor or Nokker's rules.

Run systematic **technical** quality checks and generate a comprehensive report. Don't fix issues; document them for other commands to address.

This is a code-level audit, not a design critique. Check what's measurable and verifiable in the implementation.

**Web only.** Native platforms (`ios` / `android` / `adaptive`) route to [audit.native.md](audit.native.md) instead; if the project is native, switch to it now.

## Diagnostic Scan

Run comprehensive checks across 5 dimensions. Score each dimension 0-4 using the criteria below. Score only what the code shows. A dimension that needs the render (responsive, touch targets, dark theme, performance) is marked "not verified (render)"; no total /20 unless every dimension was scored.

### 1. Accessibility (A11y)

**Check for**:
- **Contrast issues**: Text contrast ratios < 4.5:1, or < 3:1 for large text (or 7:1 for AAA)
- **Motion sensitivity**: `prefers-reduced-motion` needs an intentional alternative that preserves state change and hierarchy; flag a global `0.01ms` kill that destroys useful feedback, flashing above threshold, and motion that blocks focus, reading, or task completion
- **Missing ARIA**: Interactive elements without proper roles, labels, or states
- **Keyboard navigation**: Missing focus indicators, illogical tab order, keyboard traps
- **Semantic HTML**: Improper heading hierarchy, missing landmarks, divs instead of buttons
- **Alt text**: Missing or poor image descriptions
- **Form issues**: Inputs without labels, poor error messaging, missing required indicators

**Score 0-4**: 0=Inaccessible (fails WCAG A), 1=Major gaps (few ARIA labels, no keyboard nav), 2=Partial (some a11y effort, significant gaps), 3=Good (WCAG AA mostly met, minor gaps), 4=Excellent (WCAG AA fully met, approaches AAA)

### 2. Performance

**Check for**:
- **Layout thrashing**: Reading/writing layout properties in loops
- **Expensive animations**: Casual layout-property animation, unbounded blur/filter/shadow effects, or effects that visibly drop frames
- **Missing optimization**: Images without lazy loading, unoptimized assets
- **will-change overuse**: `will-change` applied broadly or left on at rest (it is a targeted hint for known expensive animations, not a baseline requirement)
- **Bundle size**: Unnecessary imports, unused dependencies
- **Render performance**: Unnecessary re-renders, missing memoization

**Score 0-4**: 0=Severe issues (layout thrash, unoptimized everything), 1=Major problems (no lazy loading, expensive animations), 2=Partial (some optimization, gaps remain), 3=Good (mostly optimized, minor improvements possible), 4=Excellent (fast, lean, well-optimized)

### 3. Theming

**Check for**:
- **Hard-coded colors**: Colors not using design tokens
- **Broken dark mode**: Missing dark mode variants, poor contrast in dark theme
- **Inconsistent tokens**: Using wrong tokens, mixing token types
- **Theme switching issues**: Values that don't update on theme change

**Score 0-4**: 0=No theming (hard-coded everything), 1=Minimal tokens (mostly hard-coded), 2=Partial (tokens exist but inconsistently used), 3=Good (tokens used, minor hard-coded values), 4=Excellent (full token system, dark mode works perfectly)

### 4. Responsive Design

**Check for**:
- **Fixed widths**: Hard-coded widths that break on mobile
- **Touch targets**: Interactive elements < 44x44px. WCAG AA (SC 2.5.8) requires at least 24x24 CSS px unless an exception applies: spacing, an equivalent control, inline text, user-agent styling, or an essential size. Spacing passes when a 24 CSS px circle centered on the target's bounding box overlaps no other target and no other undersized target's circle
- **Broken touch interaction**: Custom sliders, drag surfaces, and scrollable control strips whose primary gesture fails under touch, that swallow page scroll or lose the drag to it, or that stay stuck after an interrupted gesture. Code tells: mouse-only handlers, no `touch-action` on a pointer-event drag surface, drag state that nothing clears on cancel, lost capture, or blur. Exercise the gesture when a browser tool can synthesize touch (a rendered viewport proves layout, not the gesture), then say what produced the evidence (emulated viewport, synthesized touch, which engine, physical device) and what stayed untested
- **Horizontal scroll**: Content overflow on narrow viewports
- **Text scaling**: Layouts that break when text size increases
- **Missing breakpoints**: No mobile/tablet variants

**Score 0-4**: 0=Desktop-only (breaks on mobile), 1=Major issues (some breakpoints, many failures), 2=Partial (works on mobile, rough edges), 3=Good (responsive, minor touch target or overflow issues), 4=Excellent (fluid, all viewports, proper touch targets, gestures work under touch)

### 5. Implementation Integrity (CRITICAL)

Run `design_check` and verify each finding in context. Look for repeated implementation shortcuts, design-system drift, misleading or decorative content, and structure that is interchangeable with an unrelated product. Keep deterministic findings separate from visual judgment and call out false positives.


**Score 0-4**: 0=systemic drift, 1=major repeated failures, 2=several verified issues, 3=minor isolated issues, 4=coherent and intentional

## Generate Report

### Audit Health Score

| # | Dimension | Score | Key Finding |
|---|-----------|-------|-------------|
| 1 | Accessibility | ? | [most critical a11y issue or "--"] |
| 2 | Performance | ? | |
| 3 | Theming | ? | |
| 4 | Responsive Design | ? | |
| 5 | Implementation Integrity | ? | |
| **Total** | | **??/20** | **[Rating band]** |

Score only what the code shows. A dimension that needs the render (responsive, touch targets, dark theme, performance) is marked "not verified (render)"; no total /20 unless every dimension was scored.

**Rating bands**: 18-20 Excellent (minor polish), 14-17 Good (address weak dimensions), 10-13 Acceptable (significant work needed), 6-9 Poor (major overhaul), 0-5 Critical (fundamental issues)

### Implementation Integrity Verdict
**Start here.** Pass/fail: does the implementation express a coherent product-specific system? Cite verified evidence and detector findings.

### Executive Summary
- Audit Health Score: **??/20** ([rating band])
- Total issues found (count by severity: P0/P1/P2/P3)
- Top 3-5 critical issues
- Recommended next steps

### Detailed Findings by Severity

Tag every issue with **P0-P3 severity**:
- **P0 Blocking**: Prevents task completion. Fix immediately
- **P1 Major**: Significant difficulty or WCAG AA violation. Fix before release
- **P2 Minor**: Annoyance, workaround exists. Fix in next pass
- **P3 Polish**: Nice-to-fix, no real user impact. Fix if time permits

For each issue, document:
- **[P?] Issue name**
- **Location**: Component, file, line
- **Category**: Accessibility / Performance / Theming / Responsive / Implementation Integrity
- **Impact**: How it affects users
- **WCAG/Standard**: Which standard it violates (if applicable)
- **Recommendation**: How to fix it
- **Suggested command**: Which command to use (prefer: adapt, clarify, colorize, distill, harden, layout, polish, typeset)

### Patterns & Systemic Issues

Identify recurring problems that indicate systemic gaps rather than one-off mistakes:
- "Hard-coded colors appear in 15+ components, should use design tokens"
- "Touch targets consistently too small (<44px) throughout mobile experience"

### Positive Findings

Note what's working well: good practices to maintain and replicate.

## Recommended Actions

List recommended commands in priority order (P0 first, then P1, then P2):

1. **[P?] `references/<name>.md`**: Brief description (specific context from audit findings)
2. **[P?] `references/<name>.md`**: Brief description (specific context)

**Rules**: Only recommend commands from: adapt, clarify, colorize, distill, harden, layout, polish, typeset. Map findings to the most appropriate command. End with `references/polish.md` as the final step if any fixes were recommended.

After presenting the summary, tell the user:

> You can ask me to run these one at a time, all at once, or in any order you prefer.
>
> Run this audit again after the fixes.

Keep P3s few: a long tail of minor issues buries the ones that matter.

**Avoid**:
- Report issues without explaining impact (why does this matter?)
- Provide generic recommendations (be specific and actionable)
- Skip positive findings (celebrate what works)
- Forget to prioritize (everything can't be P0)
- Report false positives without verification
