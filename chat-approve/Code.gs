/**
 * AI Peer Review — one-tap operator approve / approve+merge from Google Chat.
 *
 * The dispatcher's escalation card shows "✅ Approve" and "🚀 Approve & Merge"
 * buttons. Each links here with ?repo=&pr=&action=&sig= where sig is an HMAC
 * the dispatcher computed over "repo:pr:action". This script recomputes the
 * HMAC and refuses anything that doesn't match — so a leaked or hand-edited
 * link (pr=90 → pr=99) is rejected.
 *
 * SETUP (one time — see chat-approve/README.md):
 *  1. script.google.com → New project → paste this as Code.gs.
 *  2. Project Settings → Script properties:
 *        GITHUB_TOKEN          fine-grained PAT: Pull requests R/W  (+ Contents R/W for merge)
 *        GITHUB_OWNER          NFS-247
 *        APPROVE_SIGNING_SECRET  the SAME random string you set as the repo secret
 *        MERGE_METHOD          (optional) merge | squash | rebase   (default: merge)
 *  3. Deploy → Web app: Execute as Me, Who has access: Only myself. Copy /exec URL.
 *  4. Put /exec URL in each repo's APPROVE_WEBAPP_URL secret, and the same
 *     signing string in each repo's APPROVE_SIGNING_SECRET secret.
 *
 * Two locks: "Only myself" deploy (only your Google account can invoke) AND the
 * per-PR signature (a link only works for the exact PR + action it was made for).
 */

function doGet(e) {
  var props = PropertiesService.getScriptProperties();
  var owner = props.getProperty('GITHUB_OWNER');
  var ghToken = props.getProperty('GITHUB_TOKEN');
  var signingSecret = props.getProperty('APPROVE_SIGNING_SECRET');
  var mergeMethod = props.getProperty('MERGE_METHOD') || 'merge';

  var p = (e && e.parameter) || {};
  var repo = p.repo || '';
  var pr = p.pr || '';
  var action = p.action || 'approve';
  var sig = p.sig || '';

  if (!owner || !ghToken || !signingSecret) {
    return page('❌ Not configured',
      'Set GITHUB_OWNER, GITHUB_TOKEN and APPROVE_SIGNING_SECRET in Script properties.');
  }
  if (!repo || !pr) {
    return page('❌ Bad request', 'Missing repo or pr.');
  }

  // Verify the per-PR signature: a link only works for the exact repo+pr+action.
  var expected = hmacHex(signingSecret, repo + ':' + pr + ':' + action);
  if (!sig || !constantTimeEquals(sig, expected)) {
    return page('❌ Signature invalid',
      'This link is not valid for ' + escapeHtml(repo) + ' #' + escapeHtml(pr) +
      ' / ' + escapeHtml(action) + '. It may have been edited or is stale.');
  }

  var command = { approve: 'OPERATOR APPROVE', approve_merge: 'OPERATOR APPROVE' }[action];
  if (!command) {
    return page('❌ Bad request', 'Unknown action: ' + escapeHtml(action));
  }

  var prUrl = 'https://github.com/' + owner + '/' + repo + '/pull/' + pr;

  // 1. Post OPERATOR APPROVE (the dispatcher records it and adds its review).
  var c = gh('post', '/repos/' + owner + '/' + repo + '/issues/' + pr + '/comments',
             ghToken, { body: command });
  if (c.code < 200 || c.code >= 300) {
    return page('⚠ GitHub error ' + c.code,
      'Could not post approval. <a href="' + prUrl + '">Open PR #' + escapeHtml(pr) +
      '</a> and approve manually.<br><br><pre>' + escapeHtml(c.text.slice(0, 500)) + '</pre>');
  }

  // 2. For Approve & Merge, attempt the merge now.
  if (action === 'approve_merge') {
    var m = gh('put', '/repos/' + owner + '/' + repo + '/pulls/' + pr + '/merge',
               ghToken, { merge_method: mergeMethod });
    if (m.code >= 200 && m.code < 300) {
      return page('🚀 Approved & merged',
        '<b>' + escapeHtml(repo) + ' #' + escapeHtml(pr) + '</b> is merged.');
    }
    return page('✅ Approved — merge pending',
      'Approval posted, but the merge could not complete yet (HTTP ' + m.code +
      ' — usually CI still running or branch protection not satisfied).<br><br>' +
      '<a href="' + prUrl + '">Open PR #' + escapeHtml(pr) + ' to merge →</a>');
  }

  return page('✅ Approved',
    'Posted on <b>' + escapeHtml(repo) + ' #' + escapeHtml(pr) + '</b>. ' +
    'The dispatcher will add its approving review.<br><br>' +
    '<a href="' + prUrl + '">Open PR #' + escapeHtml(pr) + ' to merge →</a>');
}

function gh(method, path, token, payload) {
  var resp = UrlFetchApp.fetch('https://api.github.com' + path, {
    method: method,
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });
  return { code: resp.getResponseCode(), text: resp.getContentText() };
}

function hmacHex(secret, message) {
  var raw = Utilities.computeHmacSha256Signature(message, secret);
  return raw.map(function (b) { return ('0' + (b & 0xFF).toString(16)).slice(-2); }).join('');
}

function constantTimeEquals(a, b) {
  if (a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
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
