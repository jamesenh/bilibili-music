/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bili: {
          pink: '#00AEEC', // 哔哩哔哩蓝/粉
          hot: '#FB7299',  // B站标志粉
          accent: '#00AEEC',
        },
        surface: {
          DEFAULT: '#121214',
          subtle: '#18181c',
          panel: '#202026',
          border: '#2c2c34',
          hover: '#26262e',
        }
      },
      fontFamily: {
        sans: [
          'PingFang SC',
          'Hiragino Sans GB',
          'Microsoft YaHei',
          'Segoe UI',
          'system-ui',
          '-apple-system',
          'sans-serif',
        ],
      },
      boxShadow: {
        'glow': '0 0 24px -4px rgba(251, 114, 153, 0.3)',
        'glow-blue': '0 0 24px -4px rgba(0, 174, 236, 0.3)',
        'modal': '0 20px 40px -15px rgba(0, 0, 0, 0.7)',
      }
    },
  },
  plugins: [],
}
