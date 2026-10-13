// LANPilot's own script. The Content-Security-Policy allows no inline scripts, so all
// behaviour lives here and is attached through attributes in the markup.
(function () {
  "use strict";

  // Turn <select data-searchable> into a searchable picker (Tom Select).
  function enhance(root) {
    if (typeof TomSelect === "undefined") {
      return;
    }
    root.querySelectorAll("select[data-searchable]").forEach(function (select) {
      if (!select.tomselect) {
        new TomSelect(select, {
          maxOptions: null,
          searchField: ["text", "optgroup"],
          placeholder: select.dataset.placeholder || "",
        });
      }
    });
  }

  function ready() {
    enhance(document);
    document.body.addEventListener("htmx:afterSwap", function (event) {
      enhance(event.detail.target);
    });
    // The session ended while the page was open: a reload leads to the sign-in page.
    document.body.addEventListener("htmx:responseError", function (event) {
      if (event.detail.xhr && event.detail.xhr.status === 401) {
        window.location.reload();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ready);
  } else {
    ready();
  }
})();
