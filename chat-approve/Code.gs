/**
 * AI Peer Review — one-tap operator approval from Google Chat.
 *
 * The dispatcher's escalation card shows an "✅ Approve" button that links here
 * with ?repo=<repo>&pr=<number>&action=approve. Tapping it posts the matching
 * OPERATOR command on the PR as you, which the dispatcher then honors.
 *
 * SETUP (one time — see chat-approve/README.md for screenshots):
 *  1. script.google.com → New project → paste this file as Code.gs.
 *  2. Project Settings → Script properties → add:
 *        GITHUB_TOKEN   fine-grained PAT, "Pull requests: Read & write" on your repos
 *        GITHUB_OWNER   NFS-247
 *        APPROVE_TOKEN  (optional) a long random string; if set, the card URL must carry it
 *  3. Deploy → New deployment → type "Web app":
 *        Execute as:      Me
 *        Who has access:  Only myself        <-- this is the security gate
 *     Copy the /exec URL.
 *  4. Put that /exec URL into each repo's APPROVE_WEBAPP_URL secret.
 *
 * Because the deployment is "Only myself", only YOU (signed into your Google
 * account) can ever invoke it — a leaked link is inert for anyone else.
 */

function doGet(e) {
  var props = PropertiesService.getScriptProperties();
  var owner = props.getProperty('GITHUB_OWNER');
  var ghToken = props.getProperty('GITHUB_TOKEN');
  var approveToken = props.getProperty('APPROVE_TOKEN'); // optional

  var p = (e && e.parameter) || {};
  var repo = p.repo || '';
  var pr = p.pr || '';
  var action = p.action || 'approve';
  var token = p.token || '';

  if (approveToken && token !== approveToken) {
    return page('❌ Not authorized', 'Invalid or missing token.');
  }
  if (!owner || !ghToken) {
    return page('❌ Not configured', 'Set GITHUB_OWNER and GITHUB_TOKEN in Script properties.');
  }
  if (!repo || !pr) {
    return page('❌ Bad request', 'Missing repo or pr.');
  }

  var command = {
    approve: 'OPERATOR APPROVE',
    block: 'OPERATOR BLOCK requested from Chat',
    investigate: 'OPERATOR INVESTIGATE requested from Chat'
  }[action];
  if (!command) {
    return page('❌ Bad request', 'Unknown action: ' + escapeHtml(action));
  }

  var prUrl = 'https://github.com/' + owner + '/' + repo + '/pull/' + pr;
  var apiUrl = 'https://api.github.com/repos/' + owner + '/' + repo +
               '/issues/' + pr + '/comments';

  var resp = UrlFetchApp.fetch(apiUrl, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + ghToken,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({ body: command }),
    muteHttpExceptions: true
  });

  var code = resp.getResponseCode();
  if (code >= 200 && code < 300) {
    return page('✅ ' + command,
      'Posted on <b>' + escapeHtml(repo) + ' #' + escapeHtml(pr) + '</b>. ' +
      'The dispatcher will add its approving review.<br><br>' +
      '<a href="' + prUrl + '">Open PR #' + escapeHtml(pr) + ' to merge →</a>');
  }
  return page('⚠ GitHub error ' + code,
    'Could not post the command. <a href="' + prUrl + '">Open PR #' +
    escapeHtml(pr) + '</a> and type it manually.<br><br><pre>' +
    escapeHtml(resp.getContentText().slice(0, 500)) + '</pre>');
}

function page(title, body) {
  var html =
    '<!DOCTYPE html><html><head>' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<meta charset="utf-8"><title>' + escapeHtml(title) + '</title></head>' +
    '<body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;' +
    'max-width:640px;margin:48px auto;padding:0 16px;line-height:1.6;color:#111">' +
    '<h2>' + escapeHtml(title) + '</h2><p>' + body + '</p></body></html>';
  return HtmlService.createHtmlOutput(html);
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
