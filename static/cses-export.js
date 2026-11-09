/* Runs on cses.fi, in the member's own logged-in browser, from the bookmarklet that /cses builds out of this very
   function (csesExport.toString()), so it has to be one self-contained function with no outside names.

   It reads the member's solved tasks off the problem list, then opens the Squadforces import page with them in the
   URL fragment (which is never sent to any server), where the member reviews and confirms. No CSES password or
   cookie leaves the browser, and nothing is sent anywhere until the member presses "Import" on that page.

   Layout it relies on (cses.fi/problemset/): every task is an <li class="task"> holding the task link and a
   <span class="task-score icon ..."> whose classes include "full" when the task is solved. If CSES changes this the
   import page shows 0 tasks found rather than guessing. */
function csesExport(origin) {
  var page = document.documentElement.outerHTML;
  var task = /<li class="task">\s*<a href="\/problemset\/task\/(\d+)"(?:(?!<li)[\s\S])*?<span class="([^"]*\btask-score\b[^"]*)"/g;
  var solved = [], total = 0, match;
  while ((match = task.exec(page)) !== null) {
    total++;
    if (/(^|\s)full(\s|$)/.test(match[2])) solved.push(match[1]);
  }
  var me = /href="\/user\/(\d+)"/.exec(page);  // a link to the member's own page, if the header has one
  var data = { solved: solved, total: total, cses_id: me ? me[1] : null };
  window.open(origin + '/cses#' + encodeURIComponent(JSON.stringify(data)), '_blank');
}
