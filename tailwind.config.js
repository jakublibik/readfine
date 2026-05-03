/** @type {import('tailwindcss').Config} */
module.exports = {
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
