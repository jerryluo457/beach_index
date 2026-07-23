import js from '@eslint/js'

export default [
  { ignores: ['dist'] },
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: {
        window: 'readonly',
        document: 'readonly',
        console: 'readonly',
        fetch: 'readonly',
        FormData: 'readonly',
        URL: 'readonly',
        setTimeout: 'readonly',
        clearTimeout: 'readonly',
        structuredClone: 'readonly',
      },
    },
  },
  {
    // Test files run under Node, not the browser.
    files: ['**/*.test.js'],
    languageOptions: {
      globals: { process: 'readonly', console: 'readonly' },
    },
    rules: {
      ...js.configs.recommended.rules,
      'no-unused-vars': ['warn', { varsIgnorePattern: '^[A-Z_]' }],
    },
  },
]
