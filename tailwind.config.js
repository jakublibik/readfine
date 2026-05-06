/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    "./backend/app/templates/**/*.html",
    "./backend/app/static/js/**/*.js",
  ],
  theme: {
    extend: {}
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
}
