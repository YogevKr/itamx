(function () {
  if (window.__itamxElalAwardHookInstalled) {
    return;
  }
  window.__itamxElalAwardHookInstalled = true;

  const SOURCE = "itamx-elal-award-hook";
  const TARGETS = [
    { kind: "fast", path: "/bfm/service/extly/booking/search/points/fast" },
    { kind: "outbound", path: "/bfm/service/extly/booking/search/points/outbound" },
    { kind: "inbound", path: "/bfm/service/extly/booking/search/points/inbound" }
  ];

  function endpointKind(url) {
    try {
      const parsed = new URL(url, window.location.href);
      const match = TARGETS.find((target) => parsed.pathname.endsWith(target.path));
      return match ? match.kind : null;
    } catch (_) {
      return null;
    }
  }

  function parseBody(text) {
    if (!text) {
      return null;
    }
    try {
      return JSON.parse(text);
    } catch (_) {
      return text;
    }
  }

  async function requestBody(input, init) {
    if (init && typeof init.body === "string") {
      return parseBody(init.body);
    }
    if (init && init.body && typeof init.body.text === "function") {
      try {
        return parseBody(await init.body.text());
      } catch (_) {
        return null;
      }
    }
    if (typeof Request !== "undefined" && input instanceof Request) {
      try {
        return parseBody(await input.clone().text());
      } catch (_) {
        return null;
      }
    }
    return null;
  }

  function publish(message) {
    window.postMessage(
      {
        source: SOURCE,
        ...message,
        capturedAt: new Date().toISOString()
      },
      window.location.origin
    );
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async function itamxFetch(input, init) {
      const url = typeof input === "string" ? input : input && input.url;
      const kind = url ? endpointKind(url) : null;
      const request = kind ? await requestBody(input, init) : null;
      const response = await originalFetch.apply(this, arguments);

      if (kind) {
        response
          .clone()
          .text()
          .then((text) => {
            publish({
              transport: "fetch",
              kind,
              url: response.url || url,
              status: response.status,
              request,
              response: parseBody(text)
            });
          })
          .catch((error) => {
            publish({
              transport: "fetch",
              kind,
              url,
              status: response.status,
              request,
              error: String(error)
            });
          });
      }

      return response;
    };
  }

  const OriginalXHR = window.XMLHttpRequest;
  if (typeof OriginalXHR === "function") {
    const originalOpen = OriginalXHR.prototype.open;
    const originalSend = OriginalXHR.prototype.send;

    OriginalXHR.prototype.open = function itamxOpen(method, url) {
      this.__itamxMethod = method;
      this.__itamxUrl = url;
      this.__itamxKind = endpointKind(url);
      return originalOpen.apply(this, arguments);
    };

    OriginalXHR.prototype.send = function itamxSend(body) {
      if (this.__itamxKind) {
        this.__itamxRequest = typeof body === "string" ? parseBody(body) : null;
        this.addEventListener("load", () => {
          publish({
            transport: "xhr",
            kind: this.__itamxKind,
            url: this.responseURL || this.__itamxUrl,
            status: this.status,
            request: this.__itamxRequest,
            response: parseBody(this.responseText)
          });
        });
        this.addEventListener("error", () => {
          publish({
            transport: "xhr",
            kind: this.__itamxKind,
            url: this.__itamxUrl,
            status: this.status,
            request: this.__itamxRequest,
            error: "XMLHttpRequest error"
          });
        });
      }
      return originalSend.apply(this, arguments);
    };
  }

  publish({ kind: "ready", url: window.location.href, status: 0 });
})();
