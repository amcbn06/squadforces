/* Markdown editor for <textarea data-md-editor data-preview-url="...">: a formatting toolbar, keyboard shortcuts,
   list continuation on Enter, and a Write / Preview switch. The preview is rendered by the server with the same
   Markdown renderer the page uses, so it shows exactly what readers will see. Without this script the textarea is
   still an ordinary textarea. */
(function () {
  'use strict';

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  // Replace ta.value[start:end] with `text` and select [selStart, selEnd]. execCommand keeps the browser's undo
  // stack; setRangeText is the fallback. An edit that would pass maxlength is dropped (the counter shows why).
  function replaceRange(ta, start, end, text, selStart, selEnd) {
    var max = ta.maxLength;
    if (max > 0 && ta.value.length - (end - start) + text.length > max) return;
    ta.focus();
    ta.setSelectionRange(start, end);
    var done = false;
    try { done = document.execCommand('insertText', false, text); } catch (e) { done = false; }
    if (!done) {
      ta.setRangeText(text, start, end, 'end');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
    ta.setSelectionRange(selStart, selEnd);
  }

  // **bold**, *italic*, `code`: wrap the selection (or a placeholder); applying it again to the same text unwraps it.
  function wrap(ta, before, after, placeholder) {
    var v = ta.value, s = ta.selectionStart, e = ta.selectionEnd, sel = v.slice(s, e);
    var wrapped = sel && v.slice(s - before.length, s) === before && v.slice(e, e + after.length) === after &&
                  !(before === '*' && (v.charAt(s - 2) === '*' || v.charAt(e + 1) === '*'));  // "*" inside "**"
    if (wrapped) {
      var from = s - before.length;
      replaceRange(ta, from, e + after.length, sel, from, from + sel.length);
      return;
    }
    var inner = sel || placeholder;
    replaceRange(ta, s, e, before + inner + after, s + before.length, s + before.length + inner.length);
  }

  function link(ta) {
    var v = ta.value, s = ta.selectionStart, e = ta.selectionEnd, sel = v.slice(s, e);
    if (/^https?:\/\/\S+$/i.test(sel)) {               // a pasted URL becomes the target; type the text
      replaceRange(ta, s, e, '[link text](' + sel + ')', s + 1, s + 10);
      return;
    }
    var label = sel || 'link text';                    // otherwise the selection is the text; type the URL
    var urlStart = s + label.length + 3;
    replaceRange(ta, s, e, '[' + label + '](https://)', urlStart, urlStart + 8);
  }

  var LINES = {
    ul: { re: /^[-*+] /, make: function () { return '- '; } },
    ol: { re: /^\d+\. /, make: function (i) { return (i + 1) + '. '; } },
    quote: { re: /^> ?/, make: function () { return '> '; } },
    heading: { re: /^#{1,6} /, make: function () { return '## '; } }
  };

  // Put (or, if every line already has it, take off) a list / quote / heading prefix on each selected line.
  function prefixLines(ta, kind) {
    var spec = LINES[kind], v = ta.value, s = ta.selectionStart, e = ta.selectionEnd;
    var from = v.lastIndexOf('\n', s - 1) + 1;
    var last = (e > s && v.charAt(e - 1) === '\n') ? e - 1 : e;   // a selection ending at a line start skips that line
    var to = v.indexOf('\n', last);
    if (to === -1) to = v.length;
    var lines = v.slice(from, to).split('\n');
    var targeted = function (l) { return l.trim() !== '' || lines.length === 1; };
    var all = lines.filter(targeted).every(function (l) { return spec.re.test(l); });
    var n = 0;
    var text = lines.map(function (l) {
      if (!targeted(l)) return l;
      if (all) return l.replace(spec.re, '');
      return spec.make(n++) + l.replace(spec.re, '');
    }).join('\n');
    if (s === e) {                                     // a bare caret stays put (shifted by the prefix) so typing goes on
      var caret = Math.max(from, s + text.length - (to - from));
      replaceRange(ta, from, to, text, caret, caret);
    } else {
      replaceRange(ta, from, to, text, from, from + text.length);
    }
  }

  // Enter inside a list or quote starts the next item; Enter on an empty item ends the list.
  function continueList(ta, ev) {
    if (ev.key !== 'Enter' || ev.shiftKey || ev.ctrlKey || ev.metaKey || ev.altKey || ev.isComposing) return;
    if (ta.selectionStart !== ta.selectionEnd) return;
    var v = ta.value, pos = ta.selectionStart;
    var lineStart = v.lastIndexOf('\n', pos - 1) + 1;
    var lineEnd = v.indexOf('\n', pos);
    if (lineEnd === -1) lineEnd = v.length;
    var m = /^(\s*)([-*+]|\d+\.|>) (.*)$/.exec(v.slice(lineStart, pos));
    if (!m) return;
    ev.preventDefault();
    if (m[3] === '' && pos === lineEnd) {
      // Drop the empty marker; after a real item leave a blank line so what comes next isn't read as part of it.
      var previous = lineStart > 0 ? v.slice(v.lastIndexOf('\n', lineStart - 2) + 1, lineStart - 1) : '';
      var blank = previous.trim() === '' ? '' : '\n';
      replaceRange(ta, lineStart, pos, blank, lineStart + blank.length, lineStart + blank.length);
      return;
    }
    var marker = /\d/.test(m[2]) ? (parseInt(m[2], 10) + 1) + '.' : m[2];
    var insert = '\n' + m[1] + marker + ' ';
    replaceRange(ta, pos, pos, insert, pos + insert.length, pos + insert.length);
  }

  var TOOLS = [
    { label: 'B', title: 'Bold (Ctrl+B)', cls: 'mde-b', run: function (ta) { wrap(ta, '**', '**', 'bold text'); } },
    { label: 'I', title: 'Italic (Ctrl+I)', cls: 'mde-i', run: function (ta) { wrap(ta, '*', '*', 'italic text'); } },
    { label: 'H', title: 'Heading', run: function (ta) { prefixLines(ta, 'heading'); } },
    { label: '</>', title: 'Code', run: function (ta) { wrap(ta, '`', '`', 'code'); } },
    { label: 'Link', title: 'Link (Ctrl+K)', run: link },
    { label: '• List', title: 'Bulleted list', run: function (ta) { prefixLines(ta, 'ul'); } },
    { label: '1. List', title: 'Numbered list', run: function (ta) { prefixLines(ta, 'ol'); } },
    { label: '“ Quote', title: 'Quote', run: function (ta) { prefixLines(ta, 'quote'); } }
  ];

  function enhance(ta) {
    if (ta.dataset.mdReady) return;
    ta.dataset.mdReady = '1';

    var box = el('div', 'mde');
    var bar = el('div', 'mde-bar');
    var tabs = el('div', 'mde-tabs');
    var writeTab = el('button', 'mde-tab', 'Write');
    var previewTab = el('button', 'mde-tab', 'Preview');
    var tools = el('div', 'mde-tools');
    var count = el('span', 'mde-count');
    var preview = el('div', 'md mde-preview');
    preview.hidden = true;
    [writeTab, previewTab].forEach(function (b) { b.type = 'button'; tabs.appendChild(b); });

    var toolButtons = TOOLS.map(function (t) {
      var b = el('button', 'mde-tool' + (t.cls ? ' ' + t.cls : ''), t.label);
      b.type = 'button';
      b.title = t.title;
      b.setAttribute('aria-label', t.title);
      b.addEventListener('mousedown', function (ev) { ev.preventDefault(); });  // keep the selection in the textarea
      b.addEventListener('click', function () { t.run(ta); });
      tools.appendChild(b);
      return b;
    });

    bar.appendChild(tabs);
    bar.appendChild(tools);
    bar.appendChild(count);
    ta.parentNode.insertBefore(box, ta);
    box.appendChild(bar);
    box.appendChild(ta);
    box.appendChild(preview);

    function updateCount() {
      var max = ta.maxLength;
      if (max > 0) {
        count.textContent = ta.value.length + ' / ' + max;
        count.classList.toggle('mde-near-limit', ta.value.length >= max * 0.9);
      }
    }
    ta.addEventListener('input', updateCount);
    updateCount();

    var token = 0;
    function renderPreview() {
      var mine = ++token;
      if (!ta.value.trim()) {
        preview.textContent = 'Nothing to preview.';
        preview.classList.add('mde-empty');
        return;
      }
      preview.classList.remove('mde-empty');
      preview.textContent = 'Loading…';
      var body = new URLSearchParams();
      body.set('text', ta.value);
      fetch(ta.dataset.previewUrl, { method: 'POST', body: body, credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
        // The server renders it with the page's own Markdown filter (raw HTML off, unsafe link targets refused).
        .then(function (html) { if (mine === token) preview.innerHTML = html; })
        .catch(function () {
          if (mine !== token) return;
          preview.textContent = 'Preview unavailable. Try again.';
          preview.classList.add('mde-empty');
        });
    }

    function setMode(mode, quiet) {
      var previewing = mode === 'preview';
      if (previewing) preview.style.minHeight = ta.offsetHeight + 'px';
      ta.hidden = previewing;
      preview.hidden = !previewing;
      writeTab.classList.toggle('active', !previewing);
      previewTab.classList.toggle('active', previewing);
      writeTab.setAttribute('aria-pressed', String(!previewing));
      previewTab.setAttribute('aria-pressed', String(previewing));
      toolButtons.forEach(function (b) { b.disabled = previewing; });
      if (previewing) renderPreview(); else if (!quiet) ta.focus();
    }
    writeTab.addEventListener('click', function () { setMode('write'); });
    previewTab.addEventListener('click', function () { setMode('preview'); });
    // A hidden textarea can't show "please fill in this field", so go back to it when the form is refused.
    ta.addEventListener('invalid', function () { if (ta.hidden) setMode('write'); });
    setMode('write', true);

    ta.addEventListener('keydown', function (ev) {
      var mod = ev.ctrlKey || ev.metaKey;
      if (mod && !ev.shiftKey && !ev.altKey) {
        var k = ev.key.toLowerCase();
        if (k === 'b') { ev.preventDefault(); wrap(ta, '**', '**', 'bold text'); return; }
        if (k === 'i') { ev.preventDefault(); wrap(ta, '*', '*', 'italic text'); return; }
        if (k === 'k') { ev.preventDefault(); link(ta); return; }
        if (k === 'enter' && ta.form) { ev.preventDefault(); if (ta.form.requestSubmit) ta.form.requestSubmit(); return; }
      }
      continueList(ta, ev);
    });
  }

  function init() {
    document.querySelectorAll('textarea[data-md-editor]').forEach(enhance);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
