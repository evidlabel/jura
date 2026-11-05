Local browser dependencies (no CDN requests at runtime):

- markdown-it 15.0.3: npm `markdown-it`, `dist/browser/markdown-it.umd.min.js` → `markdown-it.min.js`. MIT, see LICENSE-markdown-it.txt.
- Mermaid 12.3.0: npm `mermaid`, `dist/mermaid.min.js`. MIT, see LICENSE-mermaid.txt; bundled dependency notices remain in the JavaScript. Loaded only when Mermaid rendering is enabled and a Mermaid block exists.
- xterm.js and addon-fit: see LICENSE-xterm.txt.

markdown-preview.html and markdown-preview.js are workbench source files.
