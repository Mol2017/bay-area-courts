// Frontend deployment config — read by app.js at startup.
//
// Most fields are auto-detected from window.location when the site is hosted
// on GitHub Pages at <user>.github.io/<repo>/web/. Set them explicitly here
// only when you need to override the auto-detection (e.g. when serving from
// a custom domain or a non-default repo path).
//
// REPO_OWNER / REPO_NAME identify which repository hosts the
// `.github/workflows/refresh.yml` workflow that the in-page Refresh button
// triggers via the GitHub REST API.

window.SITE_CONFIG = {
  // Override these only if auto-detection fails.
  // repoOwner: "wentao",
  // repoName:  "bay-area-courts",

  // Workflow filename to dispatch via the API. Must match the file in
  // .github/workflows/.
  workflowFile: "refresh.yml",

  // Branch the workflow runs on (must match the workflow's default).
  workflowRef: "main",
};
