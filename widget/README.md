# widget

The embeddable website chat widget. Vanilla JavaScript, no runtime dependencies, rendered in a
Shadow DOM, about 10 KB minified. Architecture and security model: [docs/ai-brain.md](../docs/ai-brain.md).

## Embed

```html
<script src="https://YOUR-APP/widget.js" data-key="pk_..." data-api="https://YOUR-API" async></script>
```

The dashboard page **Website Chat** shows this snippet with your real public key. The key is a public
identifier; the widget never holds API secrets. Message text is only ever inserted with
`textContent`, and requests are sent with `credentials: "omit"`.

## Develop

```bash
cd widget
npm install
npm test          # node:test + jsdom
npm run build     # bundles to dist/widget.js and copies it to ../frontend/public/widget.js
```

`dist/` and `frontend/public/widget.js` are generated and git-ignored; run `npm run build` once
before starting the frontend (a fresh checkout has no `widget.js` until you do).

## Programmatic use

```js
const chat = await window.BizFlowChat.init({ key: "pk_...", api: "https://YOUR-API" });
chat.open();   // chat.close(), chat.destroy()
```

`init` resolves to `null` and renders nothing when the key is unknown or disabled, or the API is
unreachable, so a broken chat never breaks the host page.
