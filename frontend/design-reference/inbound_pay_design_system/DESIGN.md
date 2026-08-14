---
name: Inbound → Pay Design System
colors:
  surface: '#f9f9ff'
  surface-dim: '#d3daea'
  surface-bright: '#f9f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f0f3ff'
  surface-container: '#e7eefe'
  surface-container-high: '#e2e8f8'
  surface-container-highest: '#dce2f3'
  on-surface: '#151c27'
  on-surface-variant: '#464555'
  inverse-surface: '#2a313d'
  inverse-on-surface: '#ebf1ff'
  outline: '#777587'
  outline-variant: '#c7c4d8'
  surface-tint: '#4d44e3'
  primary: '#3525cd'
  on-primary: '#ffffff'
  primary-container: '#4f46e5'
  on-primary-container: '#dad7ff'
  inverse-primary: '#c3c0ff'
  secondary: '#575e70'
  on-secondary: '#ffffff'
  secondary-container: '#d9dff5'
  on-secondary-container: '#5c6274'
  tertiary: '#46494a'
  on-tertiary: '#ffffff'
  tertiary-container: '#5e6061'
  on-tertiary-container: '#dadbdc'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#e2dfff'
  primary-fixed-dim: '#c3c0ff'
  on-primary-fixed: '#0f0069'
  on-primary-fixed-variant: '#3323cc'
  secondary-fixed: '#dce2f7'
  secondary-fixed-dim: '#c0c6db'
  on-secondary-fixed: '#141b2b'
  on-secondary-fixed-variant: '#404758'
  tertiary-fixed: '#e1e3e4'
  tertiary-fixed-dim: '#c5c7c8'
  on-tertiary-fixed: '#191c1d'
  on-tertiary-fixed-variant: '#454748'
  background: '#f9f9ff'
  on-background: '#151c27'
  surface-variant: '#dce2f3'
typography:
  display:
    fontFamily: Inter
    fontSize: 30px
    fontWeight: '700'
    lineHeight: 38px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.015em
  headline-md:
    fontFamily: Inter
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: -0.01em
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  body-sm:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
  label-md:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.05em
  mono-sm:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  unit: 4px
  container-margin: 24px
  gutter: 16px
  density-compact: 8px
  density-default: 12px
  density-spacious: 20px
---

## Brand & Style
The design system is engineered for high-stakes enterprise supply chain management. The brand personality is authoritative, precise, and transparent, mirroring the "End-to-End Traceability" core value. 

The style is **Modern Corporate**, drawing heavily from high-performance productivity tools. It prioritizes information density without sacrificing visual breathing room. The aesthetic relies on a "Layered Surface" philosophy where depth is communicated through subtle borders and tonal shifts rather than heavy shadows. The emotional response should be one of absolute control and reliability.

## Colors
The palette is rooted in a "Paper & Ink" foundation. The background architecture uses `#FFFFFF` for primary content areas and `#F9FAFB` for sidebars and secondary containers to create clear structural separation.

- **Primary (Indigo):** Reserved strictly for primary actions, active states, and progress indicators.
- **Typography (Navy/Charcoal):** `#111827` is used for headings and high-emphasis text; `#374151` for body content.
- **Functional Colors:** Success, Warning, and Danger colors are calibrated for high legibility on light backgrounds, used primarily in status badges and exception alerts.
- **Borders:** A consistent `#E5E7EB` is the primary tool for element separation, replacing the need for drop shadows in most interface regions.

## Typography
This design system utilizes **Inter** for all UI elements to ensure maximum legibility at small sizes. For technical data—such as tracking numbers, SKUs, or hash values—**JetBrains Mono** is introduced to provide clear character differentiation.

- **Scale:** The type scale is tight. Use `body-md` (14px) as the default for most data entry and grid content.
- **Hierarchy:** Use font weight rather than size to establish hierarchy in dense data views.
- **Mobile:** For mobile views, `display` and `headline-lg` should be reduced to 24px and 20px respectively to maintain context.

## Layout & Spacing
The layout follows a **Fluid Grid** model with fixed-width sidebars (240px). The primary content area utilizes a 12-column grid with 16px gutters.

- **Information Density:** The system supports three density modes. Data-heavy tables should default to `density-compact` (8px internal padding), while configuration forms use `density-default`.
- **Breakpoints:**
  - Desktop: 1280px+
  - Tablet: 768px - 1279px (Sidebar collapses to icons)
  - Mobile: <767px (Sidebar becomes a bottom sheet/drawer; grid becomes 4-column)

## Elevation & Depth
Elevation is primarily communicated through **Tonal Layering** and **Low-Contrast Outlines**. 

- **Level 0 (Base):** `#F9FAFB` (Application background).
- **Level 1 (Card/Surface):** `#FFFFFF` with a 1px border of `#E5E7EB`. No shadow.
- **Level 2 (Overlays/Dropdowns):** `#FFFFFF` with a 1px border and a very soft ambient shadow: `0px 4px 12px rgba(0, 0, 0, 0.05)`.
- **Active State:** Elements being dragged or interacted with should use a subtle Indigo tint (`#EEF2FF`) on their background to indicate focus.

## Shapes
The design system uses a **Soft (0.25rem)** rounding strategy. This maintains a professional, "engineered" feel while avoiding the clinical harshness of sharp corners.

- **Small Components:** Checkboxes and small tags use `rounded` (4px).
- **Medium Components:** Buttons, Input fields, and Cards use `rounded-lg` (8px).
- **Large Components:** Modals and large containers use `rounded-xl` (12px).
- **Full Rounding:** Only used for "Status Dots" or "User Avatars".

## Components
- **Buttons:** Primary buttons use `#4F46E5` with white text. Secondary buttons use a white background with a `#D1D5DB` border and `#374151` text. No gradients.
- **Data Tables:** The cornerstone of the system. Use a "Zebra" striping or subtle hover highlight (`#F9FAFB`). Headers must be sticky, using `label-md` typography.
- **Status Badges:** Use a "Pill" shape with a light background tint and dark text of the same hue (e.g., Success Badge: Background `#D1FAE5`, Text `#065F46`).
- **Input Fields:** Use a 1px border `#D1D5DB`. On focus, transition border to `#4F46E5` and add a 2px indigo "halo" with 20% opacity.
- **Traceability Timeline:** A vertical line component with nodes representing supply chain milestones (Inbound, Warehouse, Customs, Paid). Use color coding on nodes to indicate real-time status.
- **Breadcrumbs:** Essential for deep navigation. Use `body-sm` with `/` separators.