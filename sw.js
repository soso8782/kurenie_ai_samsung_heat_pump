// Service worker pre Web Push. Bez cachovania.
// iOS vyzaduje showNotification pri KAZDOM push (ziadne tiche pushe).
self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
  var d = null;
  try {
    if (event.data) d = event.data.json();
  } catch (e) {
    d = null;
  }
  if (!d || typeof d !== "object") d = {};
  var title = d.title || "Kúrenie";
  var opts = {
    body: d.body || "",
    icon: "/icon-192.png",
    badge: "/favicon.png",
    data: { url: d.url || "/" }
  };
  if (d.tag) opts.tag = d.tag;
  try {
    if (typeof d.badge === "number" && self.navigator && self.navigator.setAppBadge) {
      self.navigator.setAppBadge(d.badge).catch(function () {});
    }
  } catch (e) {}
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if ("focus" in c) return c.focus();
      }
      return self.clients.openWindow(url);
    })
  );
});
