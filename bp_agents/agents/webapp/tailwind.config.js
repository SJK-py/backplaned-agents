/**
 * Tailwind config for the webapp's PROD stylesheet build.
 *
 * Build (from this directory), producing the file base.html links when
 * WEBAPP_USE_BUILT_CSS=true:
 *
 *   npx tailwindcss -c tailwind.config.js \
 *     -i static/tailwind.input.css -o static/tailwind.css --minify
 *
 * Dev needs none of this — base.html falls back to the Tailwind Play CDN.
 * The `brand` palette mirrors the inline `tailwind.config` the CDN path uses.
 */
module.exports = {
  content: ["./templates/**/*.html"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#f5f7fb",
          500: "#4f5d75",
          700: "#2d3748",
          900: "#1a202c",
        },
      },
    },
  },
};
