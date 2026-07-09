(function () {
  const INVITE_NOTICE = /^Invite created:\s+(.+)$/;

  document.addEventListener('DOMContentLoaded', function () {
    const notice = document.querySelector('.notice');
    if (!notice || !navigator.clipboard?.writeText) return;

    const match = notice.textContent.trim().match(INVITE_NOTICE);
    if (!match) return;

    const url = match[1].trim();
    navigator.clipboard.writeText(url).then(function () {
      notice.textContent += ' (copied to clipboard)';
    }).catch(function () {});
  });
})();
