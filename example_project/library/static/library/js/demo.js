/* Showcase glue for the overlay demo page. Wires the library primitives
 * (multiline.js, highlight.js) to the demo API (format / explain / search).
 * Nothing here is part of DjangoQL — it is how *this* app chooses to use it. */
(function () {
  'use strict';

  var textarea = document.getElementById('q');
  var form = document.getElementById('search-form');
  var errorEl = document.getElementById('error');
  var resultsBody = document.querySelector('#results tbody');
  var resultCount = document.getElementById('result-count');
  var explainPanel = document.getElementById('explain-panel');
  var explainTree = document.getElementById('explain-tree');

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = !msg;
  }

  function post(url) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ q: textarea.value }),
    }).then(function (r) {
      return r.json().then(function (data) {
        return { ok: r.ok, data: data };
      });
    });
  }

  function repaintOverlay() {
    // highlight.js listens for 'input' to repaint its backdrop.
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function run() {
    showError('');
    post('/api/search/').then(function (res) {
      if (!res.ok) { showError(res.data.error || 'Query error'); return; }
      resultCount.textContent =
        'showing ' + res.data.shown + ' of ' + res.data.total;
      resultsBody.innerHTML = '';
      res.data.rows.forEach(function (row) {
        var tr = document.createElement('tr');
        [row.title, row.author, row.publisher, row.year,
          row.rating, row.price, row.in_stock ? '✓' : '—'
        ].forEach(function (v) {
          var td = document.createElement('td');
          td.textContent = v;
          tr.appendChild(td);
        });
        resultsBody.appendChild(tr);
      });
    });
  }

  function format() {
    showError('');
    post('/api/format/').then(function (res) {
      if (!res.ok) { showError(res.data.error || 'Cannot format'); return; }
      if (res.data.formatted) {
        textarea.value = res.data.formatted;
        textarea.rows = Math.max(3, res.data.formatted.split('\n').length);
        repaintOverlay();
      }
    });
  }

  function renderNode(node) {
    var li = document.createElement('li');
    li.className = 'node role-' + node.role;
    li.title = node.count + ' row(s) match this sub-expression';
    var label = document.createElement('span');
    label.className = 'node-label';
    label.textContent = node.text;
    var count = document.createElement('span');
    count.className = 'node-count';
    count.textContent = node.count;
    li.appendChild(count);
    li.appendChild(label);
    if (node.children && node.children.length) {
      var ul = document.createElement('ul');
      node.children.forEach(function (child) {
        ul.appendChild(renderNode(child));
      });
      li.appendChild(ul);
    }
    return li;
  }

  function explain() {
    showError('');
    post('/api/explain/').then(function (res) {
      if (!res.ok) { showError(res.data.error || 'Cannot explain'); return; }
      explainTree.innerHTML = '';
      if (!res.data.tree) { explainPanel.hidden = true; return; }
      var ul = document.createElement('ul');
      ul.className = 'tree';
      ul.appendChild(renderNode(res.data.tree));
      explainTree.appendChild(ul);
      explainPanel.hidden = false;
    });
  }

  form.addEventListener('submit', function (e) { e.preventDefault(); run(); });
  document.getElementById('btn-format').addEventListener('click', format);
  document.getElementById('btn-explain').addEventListener('click', explain);

  Array.prototype.forEach.call(
    document.querySelectorAll('.example'),
    function (btn) {
      btn.addEventListener('click', function () {
        textarea.value = btn.dataset.q;
        repaintOverlay();
        run();
      });
    }
  );
}());
