/* Workbench Markdown preview. Libraries are local; document HTML stays escaped. */
const md = markdownit({ html: false, linkify: true });
let revision = 0, latest, busy = false, mermaidLoad, serial = 0;
const cache = new Map();
function localPath(href, documentPath) {
  if (/^[a-z][a-z\d+.-]*:|^\/\//i.test(href)) return null;
  try {
    const base = new URL('/' + documentPath, location.origin);
    const url = new URL(href, base);
    return decodeURIComponent(url.pathname.slice(1));
  } catch { return null; }
}
function loadMermaid() {
  if (!mermaidLoad) mermaidLoad = new Promise((resolve, reject) => {
    const script = document.createElement('script'); script.src = '/vendor/mermaid.min.js';
    script.onload = resolve; script.onerror = () => reject(new Error('Could not load Mermaid.'));
    document.head.append(script);
  });
  return mermaidLoad;
}
async function render() {
  if (busy) return;
  busy = true;
  try {
    while (latest) {
      const data = latest; latest = null; const gen = revision;
      for (const [key, value] of Object.entries(data.colours || {})) document.documentElement.style.setProperty('--' + key, value);
      document.documentElement.style.colorScheme = data.dark ? 'dark' : 'light';
      const article = document.querySelector('article'), top = scrollY;
      article.innerHTML = md.render(data.text);
      // Local images are fetched through the workdir's existing raw-file route.
      for (const img of article.querySelectorAll('img')) {
        const path = localPath(img.getAttribute('src'), data.path);
        if (path) img.src = '/api/raw?path=' + encodeURIComponent(path);
        else img.removeAttribute('src');
      }
      for (const a of article.querySelectorAll('a')) {
        const href = a.getAttribute('href');
        if (href.startsWith('#')) continue;
        const path = localPath(href, data.path);
        if (path !== null) a.onclick = e => { e.preventDefault(); parent.postMessage({type:'markdown-open', path}, '*'); };
        else { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
      }
      // Stable heading anchors, including duplicate headings.
      const ids = new Set();
      for (const h of article.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
        const base = h.textContent.toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu, '').trim().replace(/\s+/g, '-') || 'section';
        let id = base, n = 1; while (ids.has(id)) id = base + '-' + n++;
        ids.add(id); h.id = id;
      }
      scrollTo(0, top);
      if (!data.diagrams) continue;
      const blocks = [...article.querySelectorAll('pre > code.language-mermaid')];
      if (!blocks.length) continue;
      try { await loadMermaid(); } catch (error) { for (const block of blocks) block.parentNode.title = error.message; continue; }
      if (gen !== revision) continue;
      mermaid.initialize({startOnLoad:false, securityLevel:'strict', theme:data.dark ? 'dark' : 'default',
        suppressErrorRendering:true, flowchart:{htmlLabels:false}});
      for (const block of blocks) {
        if (gen !== revision) break;
        const key = (data.dark ? 'dark:' : 'light:') + block.textContent;
        try {
          let svg = cache.get(key);
          if (!svg) {
            svg = (await mermaid.render('md-diagram-' + serial++, block.textContent)).svg;
            if (cache.size >= 50) cache.delete(cache.keys().next().value);
            cache.set(key, svg);
          }
          if (gen !== revision) break;
          const diagram = document.createElement('div'); diagram.className = 'diagram'; diagram.innerHTML = svg;
          block.parentNode.replaceWith(diagram);
        } catch (error) {
          if (gen !== revision) break;
          const message = document.createElement('p'); message.className = 'error'; message.textContent = 'Mermaid: ' + error.message;
          block.parentNode.after(message);
        }
      }
    }
  } finally { busy = false; }
}
window.addEventListener('message', e => {
  if (e.source !== parent || e.data?.type !== 'markdown-render' || typeof e.data.text !== 'string') return;
  latest = e.data; revision++; render();
});
parent.postMessage({type:'markdown-ready'}, '*');
