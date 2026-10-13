(function () {
  "use strict";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function inline(value) {
    return value
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  }

  function parse(markdown) {
    var text = escapeHtml(markdown).replace(/\r\n?/g, "\n");
    var codeBlocks = [];
    text = text.replace(/```(?:[a-z0-9_-]+)?\n([\s\S]*?)```/gi, function (_, code) {
      var marker = "@@OFFLINE_CODE_" + codeBlocks.length + "@@";
      codeBlocks.push("<pre><code>" + code.replace(/\n$/, "") + "</code></pre>");
      return marker;
    });

    var html = text.split(/\n{2,}/).map(function (block) {
      var trimmed = block.trim();
      if (!trimmed) return "";
      if (/^@@OFFLINE_CODE_\d+@@$/.test(trimmed)) return trimmed;
      var heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
      if (heading) {
        var level = heading[1].length;
        return "<h" + level + ">" + inline(heading[2]) + "</h" + level + ">";
      }
      var lines = trimmed.split("\n");
      if (lines.every(function (line) { return /^[-*]\s+/.test(line); })) {
        return "<ul>" + lines.map(function (line) {
          return "<li>" + inline(line.replace(/^[-*]\s+/, "")) + "</li>";
        }).join("") + "</ul>";
      }
      if (lines.every(function (line) { return /^\d+\.\s+/.test(line); })) {
        return "<ol>" + lines.map(function (line) {
          return "<li>" + inline(line.replace(/^\d+\.\s+/, "")) + "</li>";
        }).join("") + "</ol>";
      }
      return "<p>" + inline(trimmed).replace(/\n/g, "<br>") + "</p>";
    }).join("\n");

    codeBlocks.forEach(function (block, index) {
      html = html.replace("@@OFFLINE_CODE_" + index + "@@", block);
    });
    return html;
  }

  window.marked = { parse: parse };
}());
