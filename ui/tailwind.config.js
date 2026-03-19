/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#eff6ff',
          500: '#3b82f6',
          600: '#2563eb',
          900: '#1e3a8a',
        },
        status: {
          running: '#3b82f6',
          success: '#22c55e',
          failed: '#ef4444',
          pending: '#94a3b8',
          paused: '#f59e0b',
          awaiting: '#a855f7',
        },
        severity: {
          critical: '#dc2626',
          major: '#ea580c',
          warning: '#d97706',
          minor: '#65a30d',
          info: '#0891b2',
        },
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
    },
  },
  plugins: [],
}
