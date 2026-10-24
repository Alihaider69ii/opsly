// Scoped to the widget's shadow root, so the host page's CSS can't break it and it can't leak out.
export const STYLES = `
:host { all: initial; }
.bf-root { position: fixed; right: 20px; bottom: 20px; z-index: 2147483000; font: 14px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #18181b; }
.bf-launcher { width: 56px; height: 56px; border-radius: 50%; border: 0; background: #18181b; color: #fff; font-size: 24px; cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,.25); }
.bf-launcher:focus-visible, .bf-send:focus-visible, .bf-close:focus-visible, .bf-retry:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }
.bf-panel { position: absolute; right: 0; bottom: 68px; width: 360px; max-width: calc(100vw - 24px); height: 480px; max-height: calc(100vh - 100px); display: flex; flex-direction: column; background: #fff; border: 1px solid #e4e4e7; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,.2); overflow: hidden; }
.bf-panel[hidden] { display: none; }
.bf-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; background: #18181b; color: #fff; font-weight: 600; }
.bf-close { background: transparent; border: 0; color: #fff; font-size: 22px; line-height: 1; cursor: pointer; }
.bf-log { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; background: #fafafa; }
.bf-msg { max-width: 85%; padding: 8px 12px; border-radius: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.bf-user { align-self: flex-end; background: #18181b; color: #fff; border-bottom-right-radius: 4px; }
.bf-assistant { align-self: flex-start; background: #e4e4e7; border-bottom-left-radius: 4px; }
.bf-typing { opacity: .7; letter-spacing: 2px; }
.bf-error { align-self: stretch; max-width: 100%; background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; display: flex; gap: 8px; align-items: center; justify-content: space-between; }
.bf-retry { border: 1px solid #991b1b; background: #fff; color: #991b1b; border-radius: 6px; padding: 4px 8px; cursor: pointer; white-space: nowrap; }
.bf-status { min-height: 18px; padding: 0 14px; font-size: 12px; color: #52525b; }
.bf-form { display: flex; gap: 8px; padding: 10px; border-top: 1px solid #e4e4e7; background: #fff; }
.bf-lead-toggle { align-self: flex-start; margin: 4px 10px; border: 0; background: transparent; color: #52525b; text-decoration: underline; font: inherit; font-size: 12px; cursor: pointer; }
.bf-lead-form { display: grid; gap: 6px; padding: 8px 10px; border-top: 1px solid #e4e4e7; background: #fff; }
.bf-lead-send { border: 0; border-radius: 8px; background: #18181b; color: #fff; padding: 7px 12px; cursor: pointer; }
.bf-lead-input { width: 100%; box-sizing: border-box; border: 1px solid #d4d4d8; border-radius: 6px; padding: 7px; font: inherit; }
.bf-consent { display: flex; align-items: center; gap: 6px; color: #52525b; font-size: 11px; }
.bf-input { flex: 1; resize: none; border: 1px solid #d4d4d8; border-radius: 8px; padding: 8px; font: inherit; max-height: 96px; }
.bf-input:disabled { background: #f4f4f5; }
.bf-send { border: 0; border-radius: 8px; background: #18181b; color: #fff; padding: 0 14px; cursor: pointer; }
.bf-send:disabled { opacity: .5; cursor: not-allowed; }
@media (max-width: 480px) { .bf-root { right: 12px; bottom: 12px; } .bf-panel { position: fixed; inset: 0; width: auto; height: auto; max-width: none; max-height: none; border-radius: 0; } }
`;
