"use client";

import { useEffect } from "react";

type WidgetGlobal = {
  init: (options: { key: string; api: string }) => Promise<unknown>;
  destroy: () => void;
};

type WindowWithWidget = Window & { BizFlowChat?: WidgetGlobal };

// The bundle is loaded once per page load, however many times the effect below runs (React runs
// effects twice in development, and the page can be left and re-entered). Appending the script on
// every run would execute the bundle repeatedly, and each copy would start its own widget.
let bundle: Promise<WidgetGlobal> | null = null;

function loadWidget(): Promise<WidgetGlobal> {
  const loaded = (window as WindowWithWidget).BizFlowChat;
  if (loaded) return Promise.resolve(loaded);
  bundle ??= new Promise<WidgetGlobal>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/widget.js";
    script.async = true;
    script.onload = () => {
      const widget = (window as WindowWithWidget).BizFlowChat;
      if (widget) resolve(widget);
      else reject(new Error("the widget script did not define BizFlowChat"));
    };
    script.onerror = () => {
      bundle = null; // allow a later visit to try again
      reject(new Error("the widget script could not be loaded"));
    };
    document.body.appendChild(script);
  });
  return bundle;
}

// Loads the real widget bundle (the same file customers embed) on this page so owners can try
// their chat. The public key and API address are public values; nothing secret reaches the browser.
export function WidgetPreview({ publicKey, apiUrl }: { publicKey: string; apiUrl: string }) {
  useEffect(() => {
    let current = true;
    loadWidget()
      .then((widget) => {
        // Leaving the page (or the effect re-running) before the bundle arrived: do not start.
        if (current) void widget.init({ key: publicKey, api: apiUrl });
      })
      .catch(() => {
        // The preview is a convenience; a missing bundle must not break the settings page.
      });
    return () => {
      current = false;
      (window as WindowWithWidget).BizFlowChat?.destroy();
    };
  }, [publicKey, apiUrl]);
  return null;
}
