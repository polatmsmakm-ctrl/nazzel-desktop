// Sends a link to Nazzel on this computer through its nazzel:// address (the browser asks once "Open Nazzel?").
function send(tabId, url, mode) {
  if (!url || !/^https?:/i.test(url)) return;
  const target = 'nazzel://download?url=' + encodeURIComponent(url) + (mode ? '&mode=' + mode : '');
  // an external address opens the app and leaves the page where it is
  chrome.tabs.update(tabId, { url: target });
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: 'nz-video', title: '⬇ نزّل بنزّل (فيديو)', contexts: ['page', 'link', 'video', 'audio', 'image'] });
    chrome.contextMenus.create({ id: 'nz-audio', title: '🎵 نزّل الصوت بس', contexts: ['page', 'link', 'video', 'audio'] });
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.linkUrl || info.srcUrl || info.pageUrl;
  // a blob: video can't be fetched from outside the page: send the page instead
  const usable = url && /^https?:/i.test(url) ? url : info.pageUrl;
  send(tab.id, usable, info.menuItemId === 'nz-audio' ? 'audio' : 'video');
});

chrome.action.onClicked.addListener((tab) => send(tab.id, tab.url, 'video'));
