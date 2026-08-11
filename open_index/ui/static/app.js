/* Shared behaviour: theme toggle and copy-to-clipboard.
 * The map has its own script; this file must stay useful with JS-light pages. */

(function () {
  var root = document.documentElement;

  function current() {
    var explicit = root.getAttribute("data-theme");
    if (explicit) return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("oi-theme", next); } catch (e) {}
    });
  }

  // Copy buttons: <button class="copy" data-copy-target="id">
  document.querySelectorAll("[data-copy-target]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.getElementById(btn.getAttribute("data-copy-target"));
      if (!el) return;
      var text = el.innerText;
      var done = function () {
        var was = btn.textContent;
        btn.textContent = "copied";
        setTimeout(function () { btn.textContent = was; }, 1200);
      };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(done, function () {});
      } else {
        // http:// origins get no navigator.clipboard, and a demo host may well
        // be one, so fall back rather than silently doing nothing.
        var ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); } catch (e) {}
        document.body.removeChild(ta);
      }
    });
  });
})();
