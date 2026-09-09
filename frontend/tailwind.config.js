/** @type {import('tailwindcss').Config} */
export default {
  // darkMode по умолчанию 'media' — тема следует prefers-color-scheme, как и требуется.
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          50: '#f6f7f9',
          100: '#eceef2',
          200: '#d5dae3',
          300: '#b0b9c9',
          400: '#8592aa',
          500: '#65738e',
          600: '#505c75',
          700: '#424b5f',
          800: '#3a4051',
          900: '#181c26',
          950: '#0e1118',
        },
        accent: {
          400: '#4ade9a',
          500: '#16c47f',
          600: '#0ea86a',
        },
      },
      fontFamily: {
        sans: [
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
      },
      keyframes: {
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        pulseUrgent: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.45' },
        },
        riseIn: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        shimmer: 'shimmer 1.6s linear infinite',
        'pulse-urgent': 'pulseUrgent 1s ease-in-out infinite',
        'rise-in': 'riseIn 0.22s ease-out',
      },
    },
  },
  plugins: [],
};
