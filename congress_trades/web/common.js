// Shared by the portal and the static page, so the two cannot drift on these.
// Loaded in <head>, so a saved theme applies before the first paint.

// Theme: follows the OS until the viewer picks; the pick is remembered per browser.
const THEME_KEY='congress-trades-theme';
function applyTheme(t){ const r=document.documentElement;
  if(t==='light'||t==='dark') r.dataset.theme=t; else delete r.dataset.theme; }
try{ applyTheme(localStorage.getItem(THEME_KEY)); }catch(e){}
const cssVar=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const isDark=()=>getComputedStyle(document.documentElement).colorScheme.trim()==='dark';
// Charts read colours from the tokens, so they listen for this and repaint.
function themeChanged(){ dispatchEvent(new Event('themechange')); }
function wireThemeToggle(btn){
  if(!btn) return;
  const label=()=>{ btn.textContent=isDark()?'Light mode':'Dark mode'; };
  label();
  btn.onclick=()=>{ const t=isDark()?'light':'dark'; applyTheme(t);
    try{ localStorage.setItem(THEME_KEY,t); }catch(e){}
    label(); themeChanged(); };
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{ label(); themeChanged(); });
}
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// A member's name in their party's colour: "D" or "Democrat" -> pD; unknown -> ''.
const pc=p=>({D:'pD',R:'pR',I:'pI'})[(p||'')[0]]||'';
