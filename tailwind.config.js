/** Tailwind is built once and the result is committed as a static asset.
 *  No Node, CDN or build step is needed on the deployment host. */
module.exports = {
  darkMode: ['class', '.theme-dark'],
  content: [
    './app/templates/**/*.html',
    './app/static/js/admin.js',
    './app/static/js/phone.js',
    './app/static/js/app.js',
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#eef2ff', 100: '#e0e7ff', 200: '#c7d2fe', 300: '#a5b4fc',
          400: '#818cf8', 500: '#6366f1', 600: '#4f46e5', 700: '#4338ca',
          800: '#3730a3', 900: '#312e81',
        },
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
      },
      boxShadow: {
        panel: '0 1px 2px rgb(16 24 40 / 6%), 0 8px 24px rgb(16 24 40 / 5%)',
      },
    },
  },
  plugins: [],
};
