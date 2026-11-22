Local browser dependencies (no CDN requests at runtime):

- markdown-it 15.0.3: npm `markdown-it`, `dist/browser/markdown-it.umd.min.js` → `markdown-it.min.js`. MIT, see LICENSE-markdown-it.txt.
- Mermaid 12.3.0: npm `mermaid`, `dist/mermaid.min.js`. MIT, see LICENSE-mermaid.txt; bundled dependency notices remain in the JavaScript. Loaded only when Mermaid rendering is enabled and a Mermaid block exists.
- xterm.js and addon-fit: see LICENSE-xterm.txt.
- CodeMirror 6 (state 6.7.6, view 6.43.15, commands 6.11.1, language 6.13.1, search 6.7.2, autocomplete 6.20.4) and @replit/codemirror-vim 6.4.0: one IIFE bundle on `window.CM`, `codemirror.min.js`, built by `make vendor-cm` from `tools/codemirror/` (pinned in its package-lock.json). MIT, see LICENSE-codemirror.txt and LICENSE-codemirror-vim.txt.

markdown-preview.html and markdown-preview.js are workbench source files.
