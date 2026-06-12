/**
 * AI Peer Review — one-tap operator approve / approve+merge from Google Chat.
 *
 * The dispatcher's cards show "✅ Approve", "🚀 Approve & Merge", "🔄 Send back"
 * (INVESTIGATE) and "✋ Block" buttons. Each links here with ?repo=&pr=&action=&sig=
 * where sig is an HMAC
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

  var command = {
    approve: 'OPERATOR APPROVE',
    approve_merge: 'OPERATOR APPROVE',
    investigate: 'OPERATOR INVESTIGATE operator requested another round from chat',
    block: 'OPERATOR BLOCK operator blocked from chat'
  }[action];
  if (!command) {
    return page('❌ Bad request', 'Unknown action: ' + escapeHtml(action));
  }

  var prUrl = 'https://github.com/' + owner + '/' + repo + '/pull/' + pr;

  // 1. Post OPERATOR APPROVE (the dispatcher records it and adds its review).
  //    ghRetry transparently retries once on a GitHub rate limit.
  var c = ghRetry('post', '/repos/' + owner + '/' + repo + '/issues/' + pr + '/comments',
                  ghToken, { body: command });
  if (c.code < 200 || c.code >= 300) {
    if (isRateLimited(c)) {
      // Still limited after one retry: degrade to a manual link, NOT an error
      // page. Nothing was lost — the operator just approves with one more tap.
      return page('⏳ GitHub is busy — tap to approve',
        'GitHub is rate-limiting right now, so your approval could not be posted ' +
        'automatically (we already retried). Nothing was lost — tap below to open ' +
        'the PR and approve manually.<br><br>' +
        '<a href="' + prUrl + '">Open PR #' + escapeHtml(pr) + ' to approve →</a>');
    }
    return page('⚠ Could not post approval',
      'Tap to open the PR and approve manually.<br><br>' +
      '<a href="' + prUrl + '">Open PR #' + escapeHtml(pr) + '</a>' +
      '<br><br><pre>' + escapeHtml(c.text.slice(0, 500)) + '</pre>');
  }

  // 2. For Approve & Merge, attempt the merge now (also retried on rate limit).
  if (action === 'approve_merge') {
    var m = ghRetry('put', '/repos/' + owner + '/' + repo + '/pulls/' + pr + '/merge',
                    ghToken, { merge_method: mergeMethod });
    if (m.code >= 200 && m.code < 300) {
      return page('🚀 Approved & merged',
        '<b>' + escapeHtml(repo) + ' #' + escapeHtml(pr) + '</b> is merged.');
    }
    if (isRateLimited(m)) {
      return page('✅ Approved — merge is rate-limited',
        'Your approval is posted. GitHub is rate-limiting the merge right now ' +
        '(we retried once). Tap to open the PR and merge manually.<br><br>' +
        '<a href="' + prUrl + '">Open PR #' + escapeHtml(pr) + ' to merge →</a>');
    }
    return page('✅ Approved — merge pending',
      'Approval posted, but the merge could not complete yet (HTTP ' + m.code +
      ' — usually CI still running or branch protection not satisfied).<br><br>' +
      '<a href="' + prUrl + '">Open PR #' + escapeHtml(pr) + ' to merge →</a>');
  }

  if (action === 'investigate') {
    return page('🔄 Sent back',
      'Posted <b>OPERATOR INVESTIGATE</b> on <b>' + escapeHtml(repo) + ' #' + escapeHtml(pr) +
      '</b>. The reviewers will run another round.<br><br>' +
      '<a href="' + prUrl + '">Open PR #' + escapeHtml(pr) + '</a>');
  }
  if (action === 'block') {
    return page('✋ Blocked',
      'Posted <b>OPERATOR BLOCK</b> on <b>' + escapeHtml(repo) + ' #' + escapeHtml(pr) +
      '</b>. The dispatcher will pause this PR until you resume it.<br><br>' +
      '<a href="' + prUrl + '">Open PR #' + escapeHtml(pr) + '</a>');
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
  return {
    code: resp.getResponseCode(),
    text: resp.getContentText(),
    headers: resp.getAllHeaders()
  };
}

// GitHub rate limits surface as HTTP 403 (primary or secondary) or 429, with
// either X-RateLimit-Remaining: 0 or a "rate limit"/"secondary rate
// limit"/"abuse" message. A single retry after a short backoff usually clears
// a brief secondary limit; if it doesn't, the caller degrades to a manual link
// instead of showing a scary error.
function ghRetry(method, path, token, payload) {
  var r = gh(method, path, token, payload);
  if (isRateLimited(r)) {
    Utilities.sleep(retryAfterMs(r));
    r = gh(method, path, token, payload);
  }
  return r;
}

function isRateLimited(r) {
  if (r.code !== 403 && r.code !== 429) return false;
  if (String(headerValue(r.headers, 'X-RateLimit-Remaining')) === '0') return true;
  return /rate limit|secondary rate|abuse/i.test(r.text || '');
}

function retryAfterMs(r) {
  var ra = headerValue(r.headers, 'Retry-After');
  var secs = ra ? parseInt(ra, 10) : NaN;
  if (!isNaN(secs) && secs > 0) return Math.min(secs, 8) * 1000;  // honor, cap 8s
  return 2000;  // default backoff (the operator is waiting on this tap)
}

function headerValue(headers, name) {
  if (!headers) return '';
  var lower = name.toLowerCase();
  for (var k in headers) {
    if (k.toLowerCase() === lower) {
      return Array.isArray(headers[k]) ? headers[k][0] : headers[k];
    }
  }
  return '';
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
