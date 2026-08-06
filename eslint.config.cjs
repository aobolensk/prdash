const html = require("eslint-plugin-html");
const js = require("@eslint/js");
const globals = require("globals");

module.exports = [
  js.configs.recommended,
  {
    files: ["**/*.html"],
    plugins: { html },
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: "script",
      globals: {
        ...globals.browser,
        htmx: "readonly",
        Chart: "readonly",
      },
    },
    rules: {
      "no-unused-vars": ["error", { args: "none" }],
    },
  },
  {
    files: [
      "**/pr_list.html",
      "**/partials/_pr_content.html",
      "**/partials/_pr_card.html",
    ],
    languageOptions: {
      globals: {
        showToast: "readonly",
        togglePrSearch: "readonly",
        copyBranchName: "readonly",
      },
    },
  },
];
