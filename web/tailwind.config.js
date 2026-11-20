/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
    "./node_modules/@yggdrasil-lab/ui/dist/**/*.{js,mjs,cjs}",
    "./node_modules/@yggdrasil-lab/ui/dist/*.{js,mjs,cjs}",
  ],
  theme: {
    extend: {
      colors: {
        background: '#090d16',
        card: '#111827',
        border: '#1f293d',
        accent: '#f59e0b',
        success: '#10b981',
        brand: '#0ea5e9',
      },
    },
  },
  plugins: [],
}
