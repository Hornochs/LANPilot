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

  // <form data-busy="text">: a slow action; show that it runs and prevent a second submit.
  function busyForms(root) {
    root.querySelectorAll("form[data-busy]").forEach(function (form) {
      form.addEventListener("submit", function () {
        var button = form.querySelector("button[type=submit]");
        if (button) {
          // Disabled after the submit has been dispatched, so the form is still sent.
          window.setTimeout(function () {
            button.disabled = true;
            button.textContent = form.dataset.busy;
          }, 0);
        }
      });
    });
  }

  function ready() {
    enhance(document);
    busyForms(document);
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
