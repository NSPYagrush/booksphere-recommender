/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        serif: ['"Newsreader"', 'Georgia', 'serif'],
        sans: ['"Inter"', 'system-ui', '-apple-system', 'sans-serif'],
      },
      colors: {
        paper: '#FBF9F5',
        ink: '#1A202C',
        brand: {
          50: '#EFF6F7',
          100: '#D6ECEF',
          500: '#1D6B73',
          600: '#15545B',
          700: '#104147',
          900: '#0B2A2E',
        },
      },
    },
  },
  plugins: [],
};
