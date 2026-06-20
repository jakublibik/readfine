/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    "./backend/app/templates/**/*.html",
    "./backend/app/static/js/**/*.js",
  ],
  theme: {
    extend: {
      keyframes: {
        'chat-bounce': {
          '0%, 100%': { transform: 'translateY(0)' },
          '50%':       { transform: 'translateY(-4px)' },
        },
      },
      animation: {
        'chat-bounce': 'chat-bounce 0.8s ease-in-out infinite',
      },
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
}
