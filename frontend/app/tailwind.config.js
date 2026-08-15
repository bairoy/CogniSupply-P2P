/**
 * Design tokens lifted verbatim from
 * frontend/design-reference/inbound_pay_design_system/DESIGN.md.
 *
 * The reference exports were built against the Tailwind CDN with an inline
 * config; this is the same token set as a real build-time config, so the app
 * and the mockups stay visually identical without importing the mockups.
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: "#f9f9ff",
        "surface-dim": "#d3daea",
        "surface-container-lowest": "#ffffff",
        "surface-container-low": "#f0f3ff",
        "surface-container": "#e7eefe",
        "surface-container-high": "#e2e8f8",
        "surface-container-highest": "#dce2f3",
        "on-surface": "#151c27",
        "on-surface-variant": "#464555",
        "inverse-surface": "#2a313d",
        "inverse-on-surface": "#ebf1ff",
        outline: "#777587",
        "outline-variant": "#c7c4d8",
        primary: "#3525cd",
        "on-primary": "#ffffff",
        "primary-container": "#4f46e5",
        "on-primary-container": "#dad7ff",
        secondary: "#575e70",
        "secondary-container": "#d9dff5",
        error: "#ba1a1a",
        "error-container": "#ffdad6",
        "on-error-container": "#93000a",
        /* functional colours for status badges (DESIGN.md "Functional Colors") */
        success: "#065f46",
        "success-container": "#d1fae5",
        warning: "#92400e",
        "warning-container": "#fef3c7",
        info: "#1e40af",
        "info-container": "#dbeafe",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      fontSize: {
        display: ["30px", { lineHeight: "38px", letterSpacing: "-0.02em", fontWeight: "700" }],
        "headline-lg": ["24px", { lineHeight: "32px", letterSpacing: "-0.015em", fontWeight: "600" }],
        "headline-md": ["20px", { lineHeight: "28px", letterSpacing: "-0.01em", fontWeight: "600" }],
        "body-lg": ["16px", { lineHeight: "24px" }],
        "body-md": ["14px", { lineHeight: "20px" }],
        "body-sm": ["13px", { lineHeight: "18px" }],
        "label-md": ["12px", { lineHeight: "16px", letterSpacing: "0.05em", fontWeight: "600" }],
        "mono-sm": ["12px", { lineHeight: "16px" }],
      },
      borderRadius: { DEFAULT: "0.25rem", md: "0.375rem", lg: "0.5rem", xl: "0.75rem" },
      boxShadow: { overlay: "0px 4px 12px rgba(0,0,0,0.05)" },
    },
  },
  plugins: [],
};
