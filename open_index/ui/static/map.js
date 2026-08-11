/* The map.
 *
 * Data arrives as JSON from /<index>/api/graph rather than being inlined, so the
 * doc-type filter and the focus are ordinary links: the page is cheap to load
 * and every view has a URL.
 *
 * Nothing is labelled on the canvas. Entity names are long and arbitrary; drawn
 * beside every dot they overlap each other and their own edges, and truncation
 * does not save a dense graph. Colour carries the type, the legend explains it,
 * and identity is on hover.
 */

(function () {
  var cfg = window.OI_MAP;
  if (!cfg || typeof cytoscape === "undefined") return;

  // Created if absent rather than assumed: when this script loaded above the
  // tooltip element, every hover threw on a null and the map looked inert while
  // drawing perfectly. A missing container should cost nothing.
  var tip = document.getElementById("tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "tip";
    document.body.appendChild(tip);
  }
  var el = document.getElementById("cy");

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  var qs = cfg.params.map(function (t) { return "t=" + encodeURIComponent(t); });
  if (cfg.focus) qs.push("focus=" + encodeURIComponent(cfg.focus));

  fetch(cfg.url + (qs.length ? "?" + qs.join("&") : ""))
    .then(function (r) { return r.json(); })
    .then(render)
    .catch(function (e) {
      el.innerHTML = '<p class="muted" style="padding:20px">Could not load the map: ' +
                     String(e) + "</p>";
    });

  function render(data) {
    document.getElementById("n-nodes").textContent = data.nodes.length;
    document.getElementById("n-edges").textContent = data.edges.length;

    if (data.capped) {
      var cap = document.getElementById("cap");
      cap.style.display = "block";
      cap.textContent = "Showing the " + data.nodes.length + " most-connected of " +
        data.total + " entities. Narrow the doc types above to see the rest.";
    }

    var legend = document.getElementById("legend-rows");
    legend.innerHTML = "";
    data.legend.forEach(function (row) {
      var d = document.createElement("div");
      d.className = "dt-row";
      d.innerHTML = '<span class="dot" style="background:' + row.color + '"></span>' +
                    '<span class="name"></span><span class="count">' + row.count + "</span>";
      d.querySelector(".name").textContent = row.doc_type;
      legend.appendChild(d);
    });
    if (data.edges.length) document.getElementById("edge-note").style.display = "block";

    var tips = {};
    var elements = [];
    data.nodes.forEach(function (n) {
      tips[n.id] = n.tooltip;
      elements.push({ data: { id: n.id, color: n.color, size: n.anchor ? 22 : 13 } });
    });
    // An edge whose endpoint was cut by the node cap would make cytoscape throw,
    // so drop those rather than lose the whole graph.
    var present = {};
    data.nodes.forEach(function (n) { present[n.id] = true; });
    data.edges.forEach(function (e, i) {
      if (!present[e.source] || !present[e.target]) return;
      var id = "e" + i;
      tips[id] = e.tooltip;
      elements.push({ data: { id: id, source: e.source, target: e.target } });
    });

    var cy = cytoscape({
      container: el,
      elements: elements,
      style: [
        { selector: "node", style: {
            "background-color": "data(color)",
            "width": "data(size)", "height": "data(size)",
            "border-width": 0, "label": ""
        }},
        { selector: "edge", style: {
            "width": 1,
            "line-color": css("--border-2"),
            "target-arrow-color": css("--border-2"),
            "target-arrow-shape": "triangle",
            "arrow-scale": .7,
            "curve-style": "straight",
            "opacity": .75
        }},
        { selector: ".hl", style: {
            "line-color": css("--accent"), "target-arrow-color": css("--accent"),
            "opacity": 1, "width": 2
        }},
        { selector: "node.hl", style: {
            "border-width": 3, "border-color": css("--accent")
        }}
      ],
      layout: layoutFor(data.nodes.length),
      wheelSensitivity: .2
    });

    function show(evt, text) {
      tip.textContent = text;
      tip.style.display = "block";
      var pad = 14;
      var x = evt.originalEvent.clientX + pad;
      var y = evt.originalEvent.clientY + pad;
      var box = tip.getBoundingClientRect();
      if (x + box.width > window.innerWidth) x = evt.originalEvent.clientX - box.width - pad;
      if (y + box.height > window.innerHeight) y = evt.originalEvent.clientY - box.height - pad;
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    }

    cy.on("mouseover", "node, edge", function (evt) {
      var t = tips[evt.target.id()];
      if (t) show(evt, t);
      evt.target.addClass("hl");
      if (evt.target.isNode()) evt.target.connectedEdges().addClass("hl");
    });
    cy.on("mousemove", "node, edge", function (evt) {
      if (tip.style.display === "block") show(evt, tip.textContent);
    });
    cy.on("mouseout", "node, edge", function (evt) {
      tip.style.display = "none";
      evt.target.removeClass("hl");
      if (evt.target.isNode()) evt.target.connectedEdges().removeClass("hl");
    });
    cy.on("tap", "node", function (evt) {
      window.location = cfg.base + "/map?focus=" + encodeURIComponent(evt.target.id());
    });
    // Leaving the canvas entirely still has to clear the tooltip: mouseout on an
    // element does not fire if the pointer leaves the window fast enough.
    el.addEventListener("mouseleave", function () { tip.style.display = "none"; });
  }

  function layoutFor(n) {
    // cose settles nicely up to a point; past it, it wanders for a long time and
    // the picture is a hairball anyway, so switch to something deterministic.
    if (n > 220) return { name: "concentric", concentric: function (node) {
      return node.degree();
    }, levelWidth: function () { return 3; }, animate: false, padding: 24 };
    return {
      name: "cose", animate: false, padding: 24,
      nodeRepulsion: 9000, idealEdgeLength: 70, gravity: .25,
      numIter: n > 120 ? 600 : 1200
    };
  }
})();
