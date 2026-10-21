const html = require("eslint-plugin-html");
const js = require("@eslint/js");
const globals = require("globals");

const browserGlobals = {
  ...globals.browser,
  htmx: "readonly",
  Chart: "readonly",
};

module.exports = [
  js.configs.recommended,
  {
    files: ["**/*.js", "**/*.html"],
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: "script",
      globals: browserGlobals,
    },
    rules: {
      "no-unused-vars": ["error", { args: "none" }],
    },
  },
  {
    files: ["**/*.html"],
    plugins: { html },
  },
  {
    files: ["**/pr_list.js"],
    languageOptions: {
      globals: {
        showToast: "readonly",
      },
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
