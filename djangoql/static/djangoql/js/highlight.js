/*
 * djangoql syntax highlighting — generic primitives, no imposed style.
 *
 * Two things, both optional:
 *
 *   DjangoQLHighlight.tokenize(text)
 *     -> [{ type, value, start, end }]
 *     A pure tokenizer mirroring the DjangoQL grammar. Feed the tokens to
 *     CodeMirror, Prism, your own renderer — whatever you like. This is the
 *     reusable primitive; it knows nothing about the DOM.
 *
 *   DjangoQLHighlight.attachOverlay(textarea)
 *     The lightweight default: paints a colour layer behind a <textarea> using
 *     a transparent-text overlay, kept in sync on input/scroll/resize. Colours
 *     come from CSS classes (.dql-tok-<type>) driven by CSS custom properties,
 *     so they are trivially overridable — or replace this entirely with your
 *     own editor. DjangoQL imposes no colour scheme.
 *
 * Token types: ws, string, number, logical (and/or/not), operator
 * (= != > >= < <= ~ !~ in startswith endswith), bool (True/False), none (None),
 * name, paren, comma, error.
 */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.DjangoQLHighlight = factory();
  }
}(typeof window !== 'undefined' ? window : this, function () {
  'use strict';

  // Whitespace / line terminators recognised by the DjangoQL lexer.
  var WS = ' \\t\\v\\f\\u00A0\\n\\r\\u2028\\u2029';
  var NL = '\\n\\r\\u2028\\u2029';
  var strBody = '(\\\\["\'\\\\/bfnrt]|\\\\u[0-9A-Fa-f]{4}|[^';

  // Order matters: longest / more specific patterns first, keywords before
  // names, two-char operators before one-char ones, float before int.
  var PATTERNS = [
    ['ws', new RegExp('^[' + WS + ']+')],
    ['string', new RegExp('^"' + strBody + '"\\\\' + NL + '])*"')],
    ['string', new RegExp("^'" + strBody + "'\\\\" + NL + "])*'")],
    ['number', /^(-?0|-?[1-9][0-9]*)(\.[0-9]+([eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)/],
    ['number', /^(-?0|-?[1-9][0-9]*)/],
    ['operator', /^(!=|>=|<=|!~)/],
    ['logical', /^(and|or|not)(?![_0-9A-Za-z])/],
    ['operator', /^(in|startswith|endswith)(?![_0-9A-Za-z])/],
    ['bool', /^(True|False)(?![_0-9A-Za-z])/],
    ['none', /^None(?![_0-9A-Za-z])/],
    ['operator', /^(=|>|<|~)/],
    ['paren', /^[()]/],
    ['comma', /^,/],
    ['name', /^[_A-Za-z][_0-9A-Za-z]*(\.[_A-Za-z][_0-9A-Za-z]*)*/],
  ];

  function tokenize(text) {
    var tokens = [];
    var pos = 0;
    text = String(text == null ? '' : text);
    while (pos < text.length) {
      var rest = text.slice(pos);
      var matched = false;
      for (var i = 0; i < PATTERNS.length; i++) {
        var type = PATTERNS[i][0];
        var m = PATTERNS[i][1].exec(rest);
        if (m && m[0].length) {
          tokens.push({
            type: type,
            value: m[0],
            start: pos,
            end: pos + m[0].length,
          });
          pos += m[0].length;
          matched = true;
          break;
        }
      }
      if (!matched) {
        // Unknown character: emit a single-char 'error' token so rendering is
        // total and we never loop forever.
        tokens.push({
          type: 'error', value: text[pos], start: pos, end: pos + 1,
        });
        pos += 1;
      }
    }
    return tokens;
  }

  // ---- Lightweight overlay (optional default renderer) -------------------

  var ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;' };

  function escapeHtml(s) {
    return s.replace(/[&<>]/g, function (c) { return ESCAPES[c]; });
  }

  function renderHtml(text) {
    // XSS-safe: every token value is HTML-escaped, and t.type is always one of
    // the fixed PATTERNS labels (never user input), so the class name and the
    // markup are not attacker-controlled.
    var tokens = tokenize(text);
    var html = '';
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      html += '<span class="dql-tok-' + t.type + '">'
        + escapeHtml(t.value) + '</span>';
    }
    // A trailing newline is not rendered unless followed by content; add a
    // marker so the backdrop height matches the textarea.
    if (text.charAt(text.length - 1) === '\n') {
      html += '\n';
    }
    return html;
  }

  // Metrics that must match for backdrop text to line up with textarea text.
  var SYNC_STYLES = [
    'fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'lineHeight',
    'letterSpacing', 'textTransform', 'wordSpacing', 'textIndent',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
    'borderTopWidth', 'borderRightWidth', 'borderBottomWidth',
    'borderLeftWidth', 'boxSizing',
  ];

  function syncStyles(textarea, backdrop) {
    var cs = window.getComputedStyle(textarea);
    for (var i = 0; i < SYNC_STYLES.length; i++) {
      backdrop.style[SYNC_STYLES[i]] = cs[SYNC_STYLES[i]];
    }
    backdrop.style.borderStyle = 'solid';
    backdrop.style.borderColor = 'transparent';
  }

  function attachOverlay(textarea) {
    if (!textarea || textarea.dataset.dqlHighlight === 'on') {
      return null; // already attached
    }
    textarea.dataset.dqlHighlight = 'on';

    var wrapper = document.createElement('div');
    wrapper.className = 'dql-highlight';
    var backdrop = document.createElement('div');
    backdrop.className = 'dql-highlight-backdrop';
    var code = document.createElement('div');
    code.className = 'dql-highlight-code';
    backdrop.appendChild(code);

    textarea.parentNode.insertBefore(wrapper, textarea);
    wrapper.appendChild(backdrop);
    wrapper.appendChild(textarea);
    textarea.classList.add('dql-highlight-input');

    function paint() {
      code.innerHTML = renderHtml(textarea.value);
    }
    function syncScroll() {
      backdrop.scrollTop = textarea.scrollTop;
      backdrop.scrollLeft = textarea.scrollLeft;
    }

    syncStyles(textarea, backdrop);
    paint();

    textarea.addEventListener('input', paint);
    textarea.addEventListener('scroll', syncScroll);
    window.addEventListener('resize', function () {
      syncStyles(textarea, backdrop);
    });

    return { repaint: paint, backdrop: backdrop };
  }

  // Auto-enable on textareas that opt in explicitly. Conservative by design:
  // the admin does NOT get highlighting unless enabled (see DjangoQLSearchMixin
  // .djangoql_highlight), because an overlay can interfere with a host page's
  // own editor or layout — that is an integrator decision.
  function autoInit() {
    var nodes = document.querySelectorAll('textarea.djangoql-highlight');
    for (var i = 0; i < nodes.length; i++) {
      attachOverlay(nodes[i]);
    }
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', autoInit);
    } else {
      autoInit();
    }
  }

  return {
    tokenize: tokenize,
    renderHtml: renderHtml,
    attachOverlay: attachOverlay,
    enable: attachOverlay,
  };
}));
